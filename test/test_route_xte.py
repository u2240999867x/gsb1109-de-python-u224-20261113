"""Reproducible cross-checks for route-fixed cross-track error (XTE).

The NMEA XTE/RMB correction flag (L/R) is relative to the *active leg's
direction of travel*, while the route's base lines are shared by the
outbound and the return trip.  These tests pin down:

* the L/R -> signed-offset convention with hand-computed values;
* that outbound and return on one shared base line map to the same
  route-fixed side;
* that an unresolvable waypoint-handoff report keeps every leg as a
  candidate instead of guessing;
* the reversal invariant: reading the identical message stream against a
  route whose definition is turned around only negates the directed
  deviation, never the magnitude or the over-limit duration.

All expectations below are derived by hand from the documented sign
convention, so the numbers can be re-computed without running the code.
"""

import pytest

import pynmea2
from pynmea2.route_xte import (
    RIGHT,
    LEFT,
    travel_signed,
    route_signed,
    side_of,
    extract_cross_track,
    RouteTrack,
    Leg,
)


def make_xte(magnitude, correction_dir, units='N'):
    return pynmea2.parse(str(pynmea2.XTE(
        'GP', 'XTE', ('V', 'N', '%.2f' % magnitude, correction_dir, units))))


def make_rmb(magnitude, correction_dir, origin, dest):
    return pynmea2.parse(str(pynmea2.RMB(
        'GP', 'RMB',
        ('A', '%.2f' % magnitude, correction_dir, origin, dest,
         '0000.00', 'N', '00000.00', 'E', '1.0', '90.0', '5.0', 'V'))))


# ---------------------------------------------------------------------------
# Sign convention: correction points back toward the track.
# ---------------------------------------------------------------------------
def test_correction_direction_signs_travel_frame():
    # Steer L -> the craft is on the right of its travel direction.
    assert travel_signed(0.30, 'L') == RIGHT * 0.30
    # Steer R -> the craft is on the left of its travel direction.
    assert travel_signed(0.30, 'R') == LEFT * 0.30
    # Magnitude is always non-negative; a stray sign is normalised away.
    assert travel_signed(0.30, 'L') == travel_signed(-0.30, 'L')
    assert travel_signed(None, 'L') is None
    assert travel_signed(0.30, 'X') is None
    # Lowercase flags are normalised to the same convention.
    assert travel_signed(0.30, 'l') == RIGHT * 0.30


def test_route_frame_flips_only_reverse_travel():
    # Forward leg keeps the sign; a return (reverse) leg negates it.
    assert route_signed(0.30, forward=True) == 0.30
    assert route_signed(0.30, forward=False) == -0.30
    assert route_signed(None, forward=False) is None


def test_side_labels():
    assert side_of(0.30) == 'R'
    assert side_of(-0.30) == 'L'
    assert side_of(0.0) is None
    assert side_of(None) is None


# ---------------------------------------------------------------------------
# One shared base line, flown outbound then return: the route-fixed side is
# identical even though the L/R flag flips with the travel direction.
# ---------------------------------------------------------------------------
def test_outbound_and_return_share_baseline_same_physical_side():
    route = RouteTrack(['A', 'B'])  # reference direction A -> B

    # Outbound A -> B, craft on the right of A -> B: correction L.
    outbound = make_xte(0.30, 'L')
    obs_out = route.observe(outbound, time=0, active=[('A', 'B')])
    assert obs_out.signed == 0.30
    assert obs_out.side == 'R'
    assert obs_out.candidates == [
        (Leg('A', 'B'), True, 0.30, 'R')]

    # Return B -> A.  The same physical side is now the craft's LEFT, so the
    # receiver emits R instead of L.
    returning = make_xte(0.30, 'R')
    obs_back = route.observe(returning, time=10, active=[('B', 'A')])
    assert obs_back.candidates == [
        (Leg('A', 'B'), False, 0.30, 'R')]
    assert obs_back.signed == 0.30
    assert obs_back.side == 'R'

    # Magnitude and route-fixed deviation agree across the two directions.
    assert obs_out.magnitude == obs_back.magnitude == 0.30
    assert obs_out.signed == obs_back.signed


# ---------------------------------------------------------------------------
# Waypoint handoff: a report that could belong to either leg keeps both.
# ---------------------------------------------------------------------------
def test_handoff_keeps_both_legs_as_candidates():
    route = RouteTrack(['A', 'B', 'C'])
    msg = make_xte(0.30, 'L')

    # Receiver is between B and may still report against A->B or already B->C.
    obs = route.observe(msg, time=20, active=[('A', 'B'), ('B', 'C')])
    assert obs.ambiguous is True
    assert obs.signed is None          # not uniquely attributable
    assert obs.side is None
    assert len(obs.candidates) == 2
    assert Leg('A', 'B') in [candidate.leg for candidate in obs.candidates]
    assert Leg('B', 'C') in [candidate.leg for candidate in obs.candidates]
    # Every candidate is still fully computed and direction-consistent.
    assert all(candidate.signed == 0.30 for candidate in obs.candidates)

    # A single active leg resolves the ambiguity.
    resolved = route.observe(msg, time=30, active=[('B', 'C')])
    assert resolved.ambiguous is False
    assert resolved.signed == 0.30


def test_unknown_leg_yields_no_candidate():
    route = RouteTrack(['A', 'B'])
    obs = route.observe(make_xte(0.10, 'L'), active=[('X', 'Y')])
    assert obs.candidates == []
    assert obs.ambiguous is True
    assert obs.signed is None


# ---------------------------------------------------------------------------
# RMB carries its own waypoint ids, so no active leg list is required.
# ---------------------------------------------------------------------------
def test_rmb_attributes_from_its_own_waypoints():
    route = RouteTrack(['A', 'B', 'C'])
    msg = make_rmb(0.25, 'R', 'B', 'C')  # craft left of B -> C
    magnitude, correction_dir, units = extract_cross_track(msg)
    assert (magnitude, correction_dir, units) == (0.25, 'R', 'N')

    obs = route.observe(msg)
    assert obs.candidates == [(Leg('B', 'C'), True, -0.25, 'L')]
    assert obs.signed == -0.25
    assert obs.side == 'L'


# ---------------------------------------------------------------------------
# Annotating already-parsed results without changing their NMEA rendering.
# ---------------------------------------------------------------------------
def test_annotate_attaches_route_deviation_and_preserves_sentence():
    route = RouteTrack(['A', 'B'])
    msg = make_xte(0.30, 'L')
    original = str(msg)

    observation = route.annotate(msg, time=0, active=[('A', 'B')])
    assert msg.route_magnitude == 0.30
    assert msg.route_signed == 0.30
    assert msg.route_side == 'R'
    assert msg.route_ambiguous is False
    assert msg.route_candidates == observation.candidates
    # The route annotation is metadata; it must not alter the NMEA sentence.
    assert str(msg) == original


# ---------------------------------------------------------------------------
# Reversal invariant over one shared message stream.
#
# The identical XTE strings and identical (physical) active legs are fed to
# the forward route and to a route whose waypoint definition is reversed.
# Only the directed deviation may change sign; magnitude and over-limit
# duration are invariant.
# ---------------------------------------------------------------------------
# (time, magnitude, correction L/R, physical active leg)
STREAM = [
    (0,  0.10, 'L', [('A', 'B')]),            # under threshold
    (10, 0.30, 'L', [('A', 'B')]),            # over threshold starts
    (20, 0.30, 'L', [('A', 'B'), ('B', 'C')]),  # handoff, ambiguous
    (30, 0.30, 'R', [('B', 'C')]),            # left of B -> C
    (40, 0.05, 'R', [('B', 'C')]),            # back under threshold
]
THRESHOLD = 0.20


def _ingest(route):
    for time, magnitude, direction, active in STREAM:
        route.observe(make_xte(magnitude, direction), time=time,
                      active=active)


def test_reversal_negates_directed_deviation_only():
    forward = RouteTrack(['A', 'B', 'C'])
    reversed_route = forward.reversed()
    assert reversed_route.waypoints == ['C', 'B', 'A']

    _ingest(forward)
    _ingest(reversed_route)

    assert len(forward.observations) == len(reversed_route.observations)
    for fwd_obs, rev_obs in zip(forward.observations,
                                reversed_route.observations):
        # Magnitudes come from the same sentences and never change.
        assert fwd_obs.magnitude == rev_obs.magnitude
        assert len(fwd_obs.candidates) == len(rev_obs.candidates)
        for fwd_candidate, rev_candidate in zip(fwd_obs.candidates,
                                                rev_obs.candidates):
            # Same base line, opposite reference direction -> opposite sign.
            assert fwd_candidate.leg.origin == rev_candidate.leg.dest
            assert fwd_candidate.leg.dest == rev_candidate.leg.origin
            assert fwd_candidate.forward != rev_candidate.forward
            assert fwd_candidate.signed == -rev_candidate.signed
            # The geometric side letter flips with the reference direction.
            assert fwd_candidate.side != rev_candidate.side

    # Spot-check the hand-computed forward values.
    by_time = {obs.time: obs for obs in forward.observations}
    assert by_time[0].signed == 0.10
    assert by_time[10].signed == 0.30
    assert by_time[20].ambiguous is True        # two legs at the handoff
    assert by_time[30].signed == -0.30          # steer R -> left side
    assert by_time[40].signed == -0.05

    # And their exact negations under the reversed definition.
    rev_by_time = {obs.time: obs for obs in reversed_route.observations}
    assert rev_by_time[0].signed == -0.10
    assert rev_by_time[10].signed == -0.30
    assert rev_by_time[20].ambiguous is True
    assert rev_by_time[30].signed == 0.30
    assert rev_by_time[40].signed == 0.05


def test_reversal_keeps_overlimit_duration_unchanged():
    forward = RouteTrack(['A', 'B', 'C'])
    reversed_route = forward.reversed()
    _ingest(forward)
    _ingest(reversed_route)

    # Over threshold from t=10 (first 0.30) until t=40 (the 0.05 sample):
    # one merged interval [10, 40), duration 30, in timestamp units.
    assert forward.exceedance_intervals(THRESHOLD) == [(10, 40)]
    assert forward.exceedance_duration(THRESHOLD) == 30
    assert reversed_route.exceedance_intervals(THRESHOLD) == [(10, 40)]
    assert reversed_route.exceedance_duration(THRESHOLD) == 30


def test_exceedance_merges_adjacent_overlimit_samples():
    route = RouteTrack(['A', 'B'])
    # Two separate over-limit bursts separated by an in-limit sample.
    samples = [
        (0, 0.30, 'L'),   # over -> burst starts
        (1, 0.30, 'L'),   # over
        (2, 0.10, 'L'),   # under -> burst closes [0, 2)
        (3, 0.40, 'L'),   # over -> second burst
        (4, 0.05, 'L'),   # under -> closes [3, 4)
    ]
    for time, magnitude, direction in samples:
        route.observe(make_xte(magnitude, direction), time=time,
                      active=[('A', 'B')])
    assert route.exceedance_intervals(THRESHOLD) == [(0, 2), (3, 4)]
    assert route.exceedance_duration(THRESHOLD) == 3


def test_duration_requires_timestamps():
    route = RouteTrack(['A', 'B'])
    route.observe(make_xte(0.30, 'L'), active=[('A', 'B')])
    with pytest.raises(ValueError):
        route.exceedance_duration(THRESHOLD)


def test_route_needs_two_waypoints():
    with pytest.raises(ValueError):
        RouteTrack(['A'])
