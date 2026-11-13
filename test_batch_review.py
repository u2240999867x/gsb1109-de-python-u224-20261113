import random
from fractions import Fraction

import pytest

from batch_review import (
    BatchReview,
    Decision,
    Row,
    UnknownChunkError,
    Version,
)


def test_duplicate_delivery_counts_once():
    review = BatchReview({"a": 4})
    rows = [
        Row("id-1", "payload-1", {"R1"}),
        Row("id-1", "payload-1", {"R1"}),
        Row("id-2", "payload-2"),
        Row("id-2", "payload-2"),
    ]
    receipt = review.submit_chunk("a", rows)
    assert receipt.delivered_rows == 4
    assert review.unique_rows == 2
    assert review.bad_rows == 1
    # Re-submitting the very same chunk changes nothing.
    review.submit_chunk("a", rows)
    assert review.unique_rows == 2
    assert review.bad_rows == 1


def test_multi_rule_violation_counts_as_one_bad_row():
    review = BatchReview({"a": 2})
    review.submit_chunk(
        "a",
        [
            Row("id-1", "payload-1", {"R1", "R2", "R3"}),
            Row("id-2", "payload-2"),
        ],
    )
    assert review.unique_rows == 2
    assert review.bad_rows == 1


def test_conflict_counts_one_bad_record_and_versions_kept_apart():
    review = BatchReview({"a": 2, "b": 2})
    review.submit_chunk("a", [Row("id-1", "payload-v1"), Row("id-2", "ok")])
    review.submit_chunk(
        "b",
        [
            Row("id-1", "payload-v2"),
            Row("id-1", "payload-v3", {"R1"}),
        ],
    )
    assert review.unique_rows == 2
    assert review.bad_rows == 1
    conflicts = review.conflicts
    assert set(conflicts) == {"id-1"}
    assert set(conflicts["id-1"]) == {
        Version("payload-v1", frozenset()),
        Version("payload-v2", frozenset()),
        Version("payload-v3", frozenset({"R1"})),
    }
    # The conflicting identity is a single bad record even with 3 versions.
    decision = review.decide()
    assert decision.complete and decision.bad_rows == 1


def test_decision_stays_pending_until_scope_complete():
    review = BatchReview({"a": 1, "b": 1, "c": 1})
    review.submit_chunk("a", [Row("id-1", "p1")])
    review.submit_chunk("c", [Row("id-3", "p3")])
    decision = review.decide()
    assert not decision.complete
    assert decision.approved is None
    assert decision.bad_ratio is None
    assert decision.missing_chunks == ("b",)
    review.submit_chunk("b", [Row("id-2", "p2")])
    final = review.decide()
    assert final.complete and final.approved is True
    assert final.bad_ratio == 0


def test_pending_reports_only_certain_breaches():
    # 2000 declared rows allow a budget of 2 bad rows.
    review = BatchReview({"a": 1000, "b": 1000})
    review.submit_chunk(
        "a",
        [Row("bad-1", "x", {"R1"}), Row("bad-2", "y", {"R2"})]
        + [Row("ok-%d" % i, "p") for i in range(998)],
    )
    decision = review.decide()
    assert not decision.complete
    # 2 bad rows could still fit the budget once all 2000 rows arrive.
    assert decision.definitely_over_limit is False

    review.submit_chunk("b", [Row("bad-3", "z", {"R3"})] + [Row("ok-b-%d" % i, "p") for i in range(999)])
    final = review.decide()
    assert final.complete
    assert final.bad_ratio == Fraction(3, 2000)
    assert final.approved is False


def test_pending_definitely_over_limit_with_missing_chunk():
    review = BatchReview({"a": 1000, "b": 1000})
    review.submit_chunk(
        "a",
        [Row("bad-%d" % i, "x", {"R1"}) for i in range(3)]
        + [Row("ok-%d" % i, "p") for i in range(997)],
    )
    decision = review.decide()
    assert not decision.complete
    assert decision.missing_chunks == ("b",)
    # 3 bad rows can never fit a budget of 2000 * 1/1000 = 2.
    assert decision.definitely_over_limit is True


def test_threshold_boundary_is_exact():
    # Exactly 1/1000 is allowed ("must not exceed").
    review = BatchReview({"a": 1000})
    review.submit_chunk(
        "a",
        [Row("bad", "x", {"R1"})] + [Row("ok-%d" % i, "p") for i in range(999)],
    )
    decision = review.decide()
    assert decision.bad_ratio == Fraction(1, 1000)
    assert decision.approved is True

    # 2/1999 is just above 1/1000 and must be rejected.
    review = BatchReview({"a": 1999})
    review.submit_chunk(
        "a",
        [Row("bad-1", "x", {"R1"}), Row("bad-2", "y", {"R2"})]
        + [Row("ok-%d" % i, "p") for i in range(1997)],
    )
    decision = review.decide()
    assert decision.bad_ratio == Fraction(2, 1999)
    assert decision.approved is False


def test_empty_batch_is_approved():
    decision = BatchReview({}).decide()
    assert decision.complete and decision.approved is True
    assert decision.unique_rows == 0 and decision.bad_rows == 0


def test_unknown_chunk_is_rejected():
    review = BatchReview({"a": 1})
    with pytest.raises(UnknownChunkError):
        review.submit_chunk("nope", [Row("id-1", "p")])


def _sample_rows():
    rows = [Row("ok-%d" % i, "payload-%d" % i) for i in range(60)]
    rows += [
        Row("bad-1", "x", {"R1", "R2", "R3"}),
        Row("bad-2", "y", {"R2"}),
        Row("dup", "same"),
        Row("dup", "same"),
        Row("conflict", "v1"),
        Row("conflict", "v2"),
    ]
    return rows


def _run(chunk_sizes, rows):
    manifest = {}
    offset = 0
    for index, size in enumerate(chunk_sizes):
        manifest["chunk-%d" % index] = size
        offset += size
    assert offset == len(rows)
    review = BatchReview(manifest)
    offset = 0
    for index, size in enumerate(chunk_sizes):
        review.submit_chunk("chunk-%d" % index, rows[offset : offset + size])
        offset += size
    return review.decide(), review.conflicts


def test_chunking_and_arrival_order_do_not_change_outcome():
    rows = _sample_rows()
    decision_a, conflicts_a = _run([len(rows)], rows)

    shuffled = rows[:]
    random.Random(20261113).shuffle(shuffled)
    decision_b, conflicts_b = _run([7, 1, 33, 10, 15], shuffled)

    assert decision_a == decision_b
    assert conflicts_a == conflicts_b
    assert decision_a.unique_rows == 64  # 60 ok + 2 bad + dup + conflict
    assert decision_a.bad_rows == 3  # bad-1, bad-2, conflict
    assert decision_a.approved is False
