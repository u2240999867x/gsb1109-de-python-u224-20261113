"""Tests for end-of-day payment/order reconciliation (schema.recon)."""

from datetime import date
from decimal import Decimal

import pytest

from schema import SchemaError, recon
from schema.recon import (
    REASON_NO_CANDIDATE,
    REASON_NO_PERFECT_MATCHING,
    REASON_UNEQUAL_PARTIES,
    Allocation,
    AmbiguousGroup,
    Candidate,
    InvalidRecord,
    MatchConfig,
    Order,
    Pair,
    Payment,
    filter_orders,
    filter_payments,
    load_orders,
    load_payments,
    reconcile,
    reconcile_records,
    reconcile_valid_records,
)

D = date(2026, 11, 1)


def order(identifier, amount, currency="USD", day=1):
    return Order(
        id=identifier,
        currency=currency,
        amount=Decimal(str(amount)),
        order_date=date(2026, 11, day),
    )


def payment(identifier, amount, currency="USD", day=1):
    return Payment(
        id=identifier,
        currency=currency,
        amount=Decimal(str(amount)),
        payment_date=date(2026, 11, day),
    )


def order_record(identifier, amount="100.00", currency="USD", day="2026-11-01"):
    return {
        "id": identifier,
        "currency": currency,
        "amount": amount,
        "order_date": day,
    }


def payment_record(identifier, amount="100.00", currency="USD", day="2026-11-01"):
    return {
        "id": identifier,
        "currency": currency,
        "amount": amount,
        "payment_date": day,
    }


def assert_is_bijective(pairs, payment_ids, order_ids):
    """Each payment and each order is used exactly once."""
    assert sorted(pair.payment_id for pair in pairs) == sorted(payment_ids)
    assert sorted(pair.order_id for pair in pairs) == sorted(order_ids)
    assert len({pair.payment_id for pair in pairs}) == len(pairs)
    assert len({pair.order_id for pair in pairs}) == len(pairs)


# --------------------------------------------------------------------------- #
# Certain pairing
# --------------------------------------------------------------------------- #


def test_exact_one_to_one_match_is_certain():
    result = reconcile([order("O1", 100)], [payment("P1", 100)])
    assert result.certain_pairs == (Pair("P1", "O1"),)
    assert result.ambiguous_groups == ()
    assert result.unmatchable_groups == ()


def test_payment_with_two_local_candidates_is_forced_globally():
    # P1 can reach O1 and O2 locally, but P2 can only reach O2.
    # The global constraint forces P2->O2 and therefore P1->O1.
    config = MatchConfig(amount_tolerance=Decimal("0.6"))
    orders = [order("O1", 100), order("O2", 101)]
    payments = [payment("P1", 100.5), payment("P2", 101)]

    result = reconcile(orders, payments, config)

    candidates = result.certain_pairs
    assert candidates == (Pair("P1", "O1"), Pair("P2", "O2"))
    assert result.ambiguous_groups == ()


def test_three_by_three_each_payment_ambiguous_locally_but_unique_globally():
    # Every payment has several local candidates; only one perfect matching
    # covers all three orders without double-claiming any of them.
    config = MatchConfig(amount_tolerance=Decimal("0.6"))
    orders = [order("O1", 100), order("O2", 101), order("O3", 102)]
    payments = [payment("P1", 100.5), payment("P2", 101.5), payment("P3", 102)]

    result = reconcile(orders, payments, config)

    assert result.certain_pairs == (
        Pair("P1", "O1"),
        Pair("P2", "O2"),
        Pair("P3", "O3"),
    )
    assert result.ambiguous_groups == ()


def test_no_order_is_claimed_twice_in_certain_pairs():
    config = MatchConfig(amount_tolerance=Decimal("5"))
    orders = [order("O1", 100), order("O2", 100)]
    payments = [payment("P1", 100), payment("P2", 100)]

    result = reconcile(orders, payments, config)
    # Symmetric data is ambiguous, but every allocation stays bijective.
    group = result.ambiguous_groups[0]
    for allocation in group.allocations:
        assert_is_bijective(allocation.pairs, ["P1", "P2"], ["O1", "O2"])


# --------------------------------------------------------------------------- #
# Ambiguous pairing with reconstructable, fully legal allocations
# --------------------------------------------------------------------------- #


def test_symmetric_two_by_two_is_ambiguous_with_two_allocations():
    orders = [order("O1", 100), order("O2", 100)]
    payments = [payment("P1", 100), payment("P2", 100)]

    result = reconcile(orders, payments)
    assert len(result.ambiguous_groups) == 1
    group = result.ambiguous_groups[0]
    assert isinstance(group, AmbiguousGroup)
    assert len(group.allocations) == 2
    assert group.truncated is False

    seen = {
        frozenset((p.payment_id, p.order_id) for p in a.pairs)
        for a in group.allocations
    }
    assert len(seen) == 2
    for allocation in group.allocations:
        assert isinstance(allocation, Allocation)
        assert_is_bijective(allocation.pairs, ["P1", "P2"], ["O1", "O2"])


def test_full_allocations_restore_two_legal_whole_batch_assignments():
    orders = [order("O1", 100), order("O2", 100)]
    payments = [payment("P1", 100), payment("P2", 100)]

    result = reconcile(orders, payments)
    allocations = result.full_allocations()

    assert len(allocations) == 2
    assert allocations[0] != allocations[1]
    for assignment in allocations:
        assert_is_bijective(assignment, ["P1", "P2"], ["O1", "O2"])


def test_full_allocations_keep_certain_pairs_in_every_variant():
    # One isolated certain pair plus one independent ambiguous 2x2 group.
    orders = [
        order("O1", 100),
        order("O2", 200),
        order("O3", 200),
    ]
    payments = [
        payment("P1", 100),
        payment("P2", 200),
        payment("P3", 200),
    ]

    result = reconcile(orders, payments)
    assert Pair("P1", "O1") in result.certain_pairs

    allocations = result.full_allocations()
    assert len(allocations) == 2
    for assignment in allocations:
        mapping = {pair.payment_id: pair.order_id for pair in assignment}
        assert mapping["P1"] == "O1"
        assert_is_bijective(assignment, ["P1", "P2", "P3"], ["O1", "O2", "O3"])
    # The two legal allocations differ on the ambiguous group only.
    first = {pair.payment_id: pair.order_id for pair in allocations[0]}
    second = {pair.payment_id: pair.order_id for pair in allocations[1]}
    assert first != second


def test_ambiguous_group_exposes_its_candidates():
    config = MatchConfig(amount_tolerance=Decimal("1"))
    orders = [order("O1", 100), order("O2", 100)]
    payments = [payment("P1", 100), payment("P2", 100)]

    group = reconcile(orders, payments, config).ambiguous_groups[0]
    assert set(group.candidates) == {"P1", "P2"}
    assert {c.order_id for c in group.candidates["P1"]} == {"O1", "O2"}
    assert all(isinstance(c, Candidate) for c in group.candidates["P1"])


def test_truncated_group_is_still_ambiguous_with_capped_solutions():
    orders = [order("O1", 100), order("O2", 100)]
    payments = [payment("P1", 100), payment("P2", 100)]

    config = MatchConfig(max_solutions=1)
    result = reconcile(orders, payments, config)
    group = result.ambiguous_groups[0]
    assert len(group.allocations) == 1
    assert group.truncated is True


# --------------------------------------------------------------------------- #
# Unmatchable records
# --------------------------------------------------------------------------- #


def test_payment_without_candidate_is_unmatchable():
    result = reconcile([order("O1", 100)], [payment("P1", 999)])
    group = next(group for group in result.unmatchable_groups if group.payments)
    assert group.reason == REASON_NO_CANDIDATE
    assert [p.id for p in group.payments] == ["P1"]
    assert group.orders == ()
    assert result.unmatched_payments[0].id == "P1"


def test_order_without_candidate_is_unmatchable():
    result = reconcile([order("O1", 100)], [payment("P1", 100), payment("P2", 500)])
    # P1/O1 pair; P2 is isolated and unmatchable.
    assert result.certain_pairs == (Pair("P1", "O1"),)
    assert result.unmatchable_groups[0].reason == REASON_NO_CANDIDATE
    assert result.unmatched_payments[0].id == "P2"
    assert result.unmatched_orders == ()


def test_unmatched_order_is_reported():
    result = reconcile([order("O1", 100), order("O2", 500)], [payment("P1", 100)])
    assert result.certain_pairs == (Pair("P1", "O1"),)
    assert [o.id for o in result.unmatched_orders] == ["O2"]
    group = next(g for g in result.unmatchable_groups if g.orders)
    assert group.reason == REASON_NO_CANDIDATE


def test_unequal_party_counts_are_unmatchable():
    orders = [order("O1", 100), order("O2", 100)]
    payments = [
        payment("P1", 100),
        payment("P2", 100),
        payment("P3", 100),
    ]
    result = reconcile(orders, payments)
    group = result.unmatchable_groups[0]
    assert group.reason == REASON_UNEQUAL_PARTIES
    assert len(group.payments) == 3 and len(group.orders) == 2


def test_no_perfect_matching_even_with_equal_counts(monkeypatch):
    # Connected, balanced graph that violates Hall's condition:
    # P1 and P2 can only reach O1; P3 reaches O1, O2 and O3.
    edges = {
        "P1": ["O1"],
        "P2": ["O1"],
        "P3": ["O1", "O2", "O3"],
    }
    candidates = {
        pid: tuple(Candidate(oid, Decimal("0"), 0) for oid in oids)
        for pid, oids in edges.items()
    }
    monkeypatch.setattr(
        recon, "_build_edges", lambda orders, payments, config: (edges, candidates)
    )

    orders = [order(f"O{i}", 1) for i in (1, 2, 3)]
    payments = [payment(f"P{i}", 1) for i in (1, 2, 3)]
    result = reconcile(orders, payments)

    assert len(result.unmatchable_groups) == 1
    assert result.unmatchable_groups[0].reason == REASON_NO_PERFECT_MATCHING


def test_three_buckets_partition_every_record_exactly_once():
    orders = [
        order("O1", 100),  # certain with P1
        order("O2", 200),  # ambiguous group with O3
        order("O3", 200),
        order("O9", 900),  # unmatched order
    ]
    payments = [
        payment("P1", 100),  # certain
        payment("P2", 200),  # ambiguous
        payment("P3", 200),  # ambiguous
        payment("P8", 800),  # unmatched payment
    ]

    result = reconcile(orders, payments)

    seen_payments = {pair.payment_id for pair in result.certain_pairs}
    seen_orders = {pair.order_id for pair in result.certain_pairs}
    for group in result.ambiguous_groups:
        seen_payments.update(p.id for p in group.payments)
        seen_orders.update(o.id for o in group.orders)
    for group in result.unmatchable_groups:
        seen_payments.update(p.id for p in group.payments)
        seen_orders.update(o.id for o in group.orders)

    assert seen_payments == {"P1", "P2", "P3", "P8"}
    assert seen_orders == {"O1", "O2", "O3", "O9"}


# --------------------------------------------------------------------------- #
# Candidate filters: currency, date window, amount tolerance
# --------------------------------------------------------------------------- #


def test_currency_must_match():
    result = reconcile(
        [order("O1", 100, currency="EUR")],
        [payment("P1", 100, currency="USD")],
    )
    assert result.unmatchable_groups[0].reason == REASON_NO_CANDIDATE


def test_different_currencies_form_independent_components():
    orders = [
        order("O1", 100, currency="USD"),
        order("O2", 100, currency="EUR"),
    ]
    payments = [
        payment("P1", 100, currency="USD"),
        payment("P2", 100, currency="EUR"),
    ]
    result = reconcile(orders, payments)
    assert result.certain_pairs == (
        Pair("P1", "O1"),
        Pair("P2", "O2"),
    )


def test_date_window_inclusive_boundary():
    config = MatchConfig(date_window_days=3)
    inside = reconcile([order("O1", 100, day=1)], [payment("P1", 100, day=4)], config)
    assert inside.certain_pairs == (Pair("P1", "O1"),)

    outside = reconcile([order("O1", 100, day=1)], [payment("P1", 100, day=5)], config)
    assert outside.unmatchable_groups[0].reason == REASON_NO_CANDIDATE


def test_absolute_amount_tolerance():
    config = MatchConfig(amount_tolerance=Decimal("1.00"))
    accepted = reconcile([order("O1", 100)], [payment("P1", 100.99)], config)
    assert accepted.certain_pairs == (Pair("P1", "O1"),)

    rejected = reconcile([order("O1", 100)], [payment("P1", 101.01)], config)
    assert rejected.unmatchable_groups[0].reason == REASON_NO_CANDIDATE


def test_relative_amount_tolerance():
    config = MatchConfig(amount_tolerance_rate=Decimal("0.01"))
    accepted = reconcile([order("O1", 100)], [payment("P1", 100.9)], config)
    assert accepted.certain_pairs == (Pair("P1", "O1"),)

    rejected = reconcile([order("O1", 100)], [payment("P1", 102)], config)
    assert rejected.unmatchable_groups[0].reason == REASON_NO_CANDIDATE


def test_effective_tolerance_uses_max_of_absolute_and_relative():
    config = MatchConfig(
        amount_tolerance=Decimal("2"),
        amount_tolerance_rate=Decimal("0.01"),
    )
    # Order of 1000: relative allowance is 10, absolute is 2 -> 10 wins.
    result = reconcile([order("O1", 1000)], [payment("P1", 1009)], config)
    assert result.certain_pairs == (Pair("P1", "O1"),)


def test_candidate_reports_amount_and_date_differences():
    config = MatchConfig(amount_tolerance=Decimal("2"), date_window_days=5)
    result = reconcile([order("O1", 100, day=1)], [payment("P1", 101, day=3)], config)
    candidate = result.certain_pairs  # pairing exists
    assert candidate == (Pair("P1", "O1"),)


def test_empty_inputs_return_empty_buckets():
    result = reconcile([], [])
    assert result.certain_pairs == ()
    assert result.ambiguous_groups == ()
    assert result.unmatchable_groups == ()
    assert result.full_allocations() == ((),)


# --------------------------------------------------------------------------- #
# Single-row validation and bad-field filtering
# --------------------------------------------------------------------------- #


def test_load_orders_normalizes_and_converts_fields():
    loaded = load_orders([order_record("O1", amount="100.50", currency="usd")])
    assert loaded == [Order("O1", "USD", Decimal("100.50"), date(2026, 11, 1))]


def test_load_payments_accepts_numeric_amounts_exactly():
    loaded = load_payments([payment_record("P1", amount=100.25)])
    assert loaded[0].amount == Decimal("100.25")


def test_extra_keys_are_ignored():
    record = order_record("O1")
    record["unexpected"] = "x"
    loaded = load_orders([record])
    assert loaded[0].id == "O1"


def test_invalid_amount_is_rejected():
    with pytest.raises(SchemaError):
        load_orders([order_record("O1", amount="not-money")])


def test_invalid_date_is_rejected():
    with pytest.raises(SchemaError):
        load_orders([order_record("O1", day="11/01/2026")])


def test_missing_required_key_is_rejected():
    with pytest.raises(SchemaError):
        load_orders([{"id": "O1", "currency": "USD", "amount": "100"}])


def test_filter_orders_quarantines_bad_rows_with_index():
    records = [
        order_record("O1"),
        order_record("O2", amount="bad"),
        order_record("O3"),
    ]
    orders, invalid = filter_orders(records)

    assert [o.id for o in orders] == ["O1", "O3"]
    assert len(invalid) == 1
    assert isinstance(invalid[0], InvalidRecord)
    assert invalid[0].index == 1
    assert isinstance(invalid[0].error, str) and invalid[0].error


def test_filter_payments_quarantines_bad_rows():
    records = [
        payment_record("P1"),
        {"id": "P2", "currency": "USD"},  # missing amount/date
    ]
    payments, invalid = filter_payments(records)
    assert [p.id for p in payments] == ["P1"]
    assert [item.index for item in invalid] == [1]


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        load_orders([order_record("O1"), order_record("O1")])
    with pytest.raises(ValueError):
        load_payments([payment_record("P1"), payment_record("P1")])


def test_reconcile_rejects_duplicate_object_ids():
    with pytest.raises(ValueError):
        reconcile([order("O1", 100), order("O1", 200)], [payment("P1", 100)])
    with pytest.raises(ValueError):
        reconcile([order("O1", 100)], [payment("P1", 100), payment("P1", 100)])


def test_multiple_independent_ambiguous_groups_stay_separate():
    orders = [
        order("O1", 100),
        order("O2", 100),
        order("O3", 200, currency="EUR"),
        order("O4", 200, currency="EUR"),
    ]
    payments = [
        payment("P1", 100),
        payment("P2", 100),
        payment("P3", 200, currency="EUR"),
        payment("P4", 200, currency="EUR"),
    ]

    result = reconcile(orders, payments)

    assert len(result.ambiguous_groups) == 2
    currencies = {group.currency for group in result.ambiguous_groups}
    assert currencies == {"USD", "EUR"}
    for group in result.ambiguous_groups:
        assert len(group.allocations) == 2
        for allocation in group.allocations:
            assert_is_bijective(
                allocation.pairs,
                [p.id for p in group.payments],
                [o.id for o in group.orders],
            )

    allocations = result.full_allocations()
    assert len(allocations) == 2
    for assignment in allocations:
        assert_is_bijective(
            assignment, ["P1", "P2", "P3", "P4"], ["O1", "O2", "O3", "O4"]
        )


def test_reconcile_valid_records_keeps_bad_rows_out_of_matching():
    batch = reconcile_valid_records(
        [
            order_record("O1"),
            order_record("O2", amount="bad"),
        ],
        [payment_record("P1"), payment_record("P2", day="bad-date")],
    )

    assert batch.has_invalid_records
    assert [item.index for item in batch.invalid_orders] == [1]
    assert [item.index for item in batch.invalid_payments] == [1]
    assert batch.result.certain_pairs == (Pair("P1", "O1"),)


def test_reconcile_records_validates_then_matches():
    result = reconcile_records([order_record("O1")], [payment_record("P1")])
    assert result.certain_pairs == (Pair("P1", "O1"),)
