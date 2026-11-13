# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import pytest

from mido.ordering import (
    EVIDENCE_SAME_PORT,
    EVIDENCE_SYNC_CALIBRATION,
    RELATION_AFTER,
    RELATION_BEFORE,
    RELATION_DISCONNECTED,
    RELATION_INDETERMINATE,
    CalibrationError,
    EventOrderer,
    KeyEvent,
    SyncPoint,
)


def make_sync(uncertainty=0.0):
    """Two ports sharing opening and intermission sync signals."""
    return [
        SyncPoint('opening', 'p1', 'show', 0.0, uncertainty, 0.0),
        SyncPoint('intermission', 'p1', 'show', 1000.0, uncertainty, 100.0),
        SyncPoint('opening', 'p2', 'show', 500.0, uncertainty, 0.0),
        SyncPoint('intermission', 'p2', 'show', 1500.0, uncertainty, 100.0),
    ]


def make_events():
    return [
        KeyEvent('a', 'p1', 'show', 100.0),
        KeyEvent('c', 'p1', 'show', 200.0),
        KeyEvent('b', 'p2', 'show', 800.0),
        KeyEvent('d', 'p2', 'show', 700.0),
    ]


def test_cross_port_order_when_intervals_separated():
    orderer = EventOrderer(make_events(), make_sync())

    # a ~ global 10s, b ~ global 30s on the shared axis.
    assert orderer.relation('a', 'b') == RELATION_BEFORE
    assert orderer.relation('b', 'a') == RELATION_AFTER
    assert orderer.definitely_before('a', 'b')
    assert not orderer.definitely_before('b', 'a')


def test_claimed_order_has_sync_evidence():
    orderer = EventOrderer(make_events(), make_sync())

    evidence = orderer.evidence('a', 'b')
    assert evidence
    assert evidence[0].kind == EVIDENCE_SYNC_CALIBRATION
    assert evidence[0].earlier_id == 'a'
    assert evidence[0].later_id == 'b'
    assert set(evidence[0].sync_labels) == {'opening', 'intermission'}

    # A claim that the guarantee does not support yields no evidence.
    assert orderer.evidence('b', 'a') == []


def test_same_port_keeps_send_order():
    orderer = EventOrderer(make_events(), make_sync())

    assert orderer.relation('a', 'c') == RELATION_BEFORE
    evidence = orderer.evidence('a', 'c')
    assert len(evidence) == 1
    assert evidence[0].kind == EVIDENCE_SAME_PORT


def test_transitive_order_chains_send_order_evidence():
    events = [
        KeyEvent('a', 'p1', 'show', 100.0),
        KeyEvent('c', 'p1', 'show', 200.0),
        KeyEvent('late', 'p1', 'show', 900.0),
    ]
    orderer = EventOrderer(events, make_sync())

    # Only consecutive same-port events share an edge, so reaching 'late'
    # from 'a' goes through the transitive closure.
    assert orderer.definitely_before('a', 'late')
    evidence = orderer.evidence('a', 'late')
    assert [item.kind for item in evidence] == [
        EVIDENCE_SAME_PORT,
        EVIDENCE_SAME_PORT,
    ]
    assert [item.later_id for item in evidence] == ['c', 'late']

    # Cross-port reachability carries calibration evidence instead.
    cross = EventOrderer(make_events(), make_sync()).evidence('a', 'b')
    assert cross[0].kind == EVIDENCE_SYNC_CALIBRATION


def test_close_events_are_indeterminate_under_uncertainty():
    orderer = EventOrderer(make_events(), make_sync(uncertainty=5.0))

    # c (~20s) and d (~19s) sit inside each other's error intervals.
    assert orderer.relation('c', 'd') == RELATION_INDETERMINATE
    assert not orderer.definitely_before('c', 'd')
    assert not orderer.definitely_before('d', 'c')
    assert ('c', 'd') in orderer.ambiguous_pairs()

    orderings = orderer.example_orderings('c', 'd')
    assert orderings is not None
    c_first, d_first = orderings
    assert c_first.index('c') < c_first.index('d')
    assert d_first.index('d') < d_first.index('c')
    assert orderer.is_consistent(c_first)
    assert orderer.is_consistent(d_first)


def test_widely_separated_events_still_ordered_under_uncertainty():
    orderer = EventOrderer(make_events(), make_sync(uncertainty=5.0))
    assert orderer.relation('a', 'b') == RELATION_BEFORE
    assert orderer.example_orderings('a', 'b') is None


def test_example_orderings_when_preferred_event_is_blocked():
    sync_points = [
        SyncPoint('opening', 'p1', 'show', 0.0, 3.0, 0.0),
        SyncPoint('intermission', 'p1', 'show', 1000.0, 3.0, 100.0),
        SyncPoint('opening', 'p2', 'show', 0.0, 3.0, 0.0),
        SyncPoint('intermission', 'p2', 'show', 1000.0, 3.0, 100.0),
    ]
    events = [
        # 'p' is guaranteed before 'y' via calibration, while 'a' on an
        # unsynced third port is an unconstrained initial source.
        KeyEvent('p', 'p1', 'show', 100.0),
        KeyEvent('y', 'p2', 'show', 200.0),
        KeyEvent('a', 'p3', 'show', 42.0),
    ]
    orderer = EventOrderer(events, sync_points)
    assert orderer.definitely_before('p', 'y')
    assert orderer.relation('y', 'a') == RELATION_INDETERMINATE

    y_first, a_first = orderer.example_orderings('y', 'a')
    assert y_first.index('y') < y_first.index('a')
    assert a_first.index('a') < a_first.index('y')
    assert orderer.is_consistent(y_first)
    assert orderer.is_consistent(a_first)


def test_reconnect_starts_independent_segment():
    events = make_events() + [
        KeyEvent('e', 'p3', 'after_reconnect', 10.0),
    ]
    sync_points = make_sync() + [
        SyncPoint('reopen', 'p3', 'after_reconnect', 0.0, 0.0, 200.0),
        SyncPoint('finale', 'p3', 'after_reconnect', 100.0, 0.0, 210.0),
    ]
    orderer = EventOrderer(events, sync_points)

    assert orderer.relation('a', 'e') == RELATION_DISCONNECTED
    assert not orderer.definitely_before('a', 'e')
    assert not orderer.definitely_before('e', 'a')
    assert orderer.evidence('a', 'e') == []

    # Both directions must remain realizable.
    orderings = orderer.example_orderings('a', 'e')
    assert orderings is not None


def test_events_outside_sync_range_have_no_interval():
    events = make_events() + [
        KeyEvent('stray', 'p1', 'show', 5000.0),
        KeyEvent('stray2', 'p2', 'show', 5000.0),
    ]
    orderer = EventOrderer(events, make_sync())

    assert orderer.interval('stray') is None
    assert not orderer.definitely_before('stray', 'stray2')
    assert not orderer.definitely_before('stray2', 'stray')

    # Same-port send order is still guaranteed without calibration.
    assert orderer.definitely_before('c', 'stray')
    assert orderer.evidence('c', 'stray')[0].kind == EVIDENCE_SAME_PORT


def test_linearizations_respect_all_guarantees():
    orderer = EventOrderer(make_events(), make_sync())

    all_orderings = list(orderer.linearizations())
    assert len(all_orderings) >= 2
    for ordering in all_orderings:
        assert orderer.is_consistent(ordering)
        assert set(ordering) == {'a', 'b', 'c', 'd'}

    # Same-port a before c must hold in every realizable ordering.
    assert all(ordering.index('a') < ordering.index('c') for ordering in all_orderings)

    limited = list(orderer.linearizations(limit=1))
    assert len(limited) == 1


def test_unique_linearization_for_total_order():
    events = [
        KeyEvent('a', 'p1', 'show', 100.0),
        KeyEvent('c', 'p1', 'show', 200.0),
        KeyEvent('b', 'p2', 'show', 800.0),
    ]
    orderer = EventOrderer(events, make_sync())
    assert list(orderer.linearizations()) == [('a', 'c', 'b')]
    assert orderer.guaranteed_pairs() == [
        ('a', 'b'),
        ('a', 'c'),
        ('c', 'b'),
    ]


def test_inconsistent_ordering_rejected():
    orderer = EventOrderer(make_events(), make_sync())
    assert not orderer.is_consistent(['b', 'a', 'c', 'd'])
    assert not orderer.is_consistent(['a', 'b'])


def test_duplicate_event_id_rejected():
    events = make_events() + [KeyEvent('a', 'p2', 'show', 900.0)]
    with pytest.raises(CalibrationError):
        EventOrderer(events, make_sync())


def test_inseparable_sync_points_rejected():
    sync_points = [
        SyncPoint('opening', 'p1', 'show', 0.0, 5.0, 0.0),
        SyncPoint('intermission', 'p1', 'show', 8.0, 5.0, 100.0),
        SyncPoint('opening', 'p2', 'show', 0.0, 5.0, 0.0),
        SyncPoint('intermission', 'p2', 'show', 1000.0, 5.0, 100.0),
    ]
    with pytest.raises(CalibrationError):
        EventOrderer(make_events(), sync_points)


def test_non_increasing_sync_times_rejected():
    sync_points = [
        SyncPoint('opening', 'p1', 'show', 1000.0, 0.0, 0.0),
        SyncPoint('intermission', 'p1', 'show', 1000.0, 0.0, 100.0),
        SyncPoint('opening', 'p2', 'show', 0.0, 0.0, 0.0),
        SyncPoint('intermission', 'p2', 'show', 1000.0, 0.0, 100.0),
    ]
    with pytest.raises(CalibrationError):
        EventOrderer(make_events(), sync_points)


def test_negative_uncertainty_rejected():
    with pytest.raises(CalibrationError):
        EventOrderer(
            make_events(),
            [SyncPoint('opening', 'p1', 'show', 0.0, -1.0)],
        )


def test_segment_without_shared_sync_is_not_calibrated():
    events = [
        KeyEvent('a', 'p1', 'show', 100.0),
        KeyEvent('b', 'p2', 'show', 100.0),
    ]
    # Only one port observed the opening signal: no common bracket exists.
    sync_points = [
        SyncPoint('opening', 'p1', 'show', 0.0, 0.0, 0.0),
        SyncPoint('intermission', 'p1', 'show', 1000.0, 0.0, 100.0),
        SyncPoint('intermission', 'p2', 'show', 1000.0, 0.0, 100.0),
    ]
    orderer = EventOrderer(events, sync_points)
    assert orderer.interval('a') is None
    assert orderer.interval('b') is None
    assert orderer.relation('a', 'b') == RELATION_INDETERMINATE
