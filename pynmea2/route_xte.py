"""Route-fixed cross-track error (XTE) interpretation.

An XTE (and the cross-track part of an RMB) reports the distance off the
active leg as a non-negative magnitude plus a correction direction,
``L`` or ``R``.  That direction is defined relative to the *active leg's
direction of travel*.  A route whose base lines are flown both ways - the
same line serves the outbound and the return trip - therefore flips the
reported L/R even though the geometry never changes.

This module keeps the geometry in one fixed, route-wide frame so that
deviations are comparable no matter which leg or travel direction
produced the report.

Sign convention (one fixed frame for the whole route)::

    signed deviation > 0  -> position to the RIGHT of the route reference
    signed deviation < 0  -> position to the LEFT of the route reference

The route reference is established by the ordered waypoint list, leg
``W[i] -> W[i+1]``.  A leg flown ``W[i] -> W[i+1]`` is *forward*; flown
the other way it is *reverse* and every deviation is negated when mapped
into the route frame.

Correction-direction semantics (NMEA 0183): the correction points back
toward the track, so a craft off to the right is told to steer ``L`` and
a craft off to the left is told to steer ``R``.  Reversing the route
definition only negates the route-fixed signed deviation; the magnitude,
and therefore any threshold-exceedance duration, is unchanged.
"""

from collections import namedtuple


# Sign of a position relative to a reference direction, right-hand side
# positive.
RIGHT = 1
LEFT = -1

# Correction letter -> sign of the side the craft is actually on, measured
# relative to its own direction of travel (right positive).  Steer L means
# the craft is on the right; steer R means it is on the left.
_OFFSET_SIGN = {
    'L': RIGHT,
    'R': LEFT,
}


Leg = namedtuple('Leg', ['origin', 'dest'])
LegDeviation = namedtuple(
    'LegDeviation', ['leg', 'forward', 'signed', 'side'])


def _to_float(value):
    if value is None or value == '':
        return None
    return float(value)


def travel_signed(magnitude, correction_dir):
    """Signed offset relative to the craft's actual direction of travel.

    Right of the travel direction is positive.  ``magnitude`` must be
    non-negative; ``correction_dir`` is the NMEA correction letter.
    """
    if magnitude is None:
        return None
    sign = _OFFSET_SIGN.get((correction_dir or '').upper())
    if sign is None:
        return None
    return sign * abs(magnitude)


def route_signed(travel_value, forward):
    """Map a travel-relative offset into the fixed route frame.

    ``forward`` is ``True`` when travel follows the route reference
    direction and ``False`` for a return (reverse) leg.
    """
    if travel_value is None:
        return None
    return travel_value if forward else -travel_value


def side_of(signed_value):
    """Return ``'R'``/``'L'`` for a route-frame offset, ``None`` at zero."""
    if signed_value is None or signed_value == 0:
        return None
    return 'R' if signed_value > 0 else 'L'


def extract_cross_track(message):
    """Return ``(magnitude, correction_dir, units)`` from XTE or RMB.

    The magnitude is a non-negative float or ``None`` when absent.
    """
    sentence = getattr(message, 'sentence_type', None)
    if sentence == 'XTE':
        magnitude = _to_float(message.cross_track_err_dist)
        correction_dir = message.correction_dir or None
        units = message.dist_units or None
    elif sentence == 'RMB':
        magnitude = _to_float(message.cross_track_error)
        correction_dir = message.cte_correction_dir or None
        units = 'N'
    else:
        raise ValueError(
            'expected an XTE or RMB sentence, got %r' % sentence)
    if correction_dir is not None:
        correction_dir = correction_dir.upper()
    return magnitude, correction_dir, units


class CrossTrackObservation(object):
    """One cross-track report and every route leg it could belong to.

    ``candidates`` holds one :class:`LegDeviation` per leg the report may
    be attributed to.  Around a waypoint handoff a receiver can emit a
    report against the leg it is leaving or the leg it is joining; when
    those cannot be disambiguated both are retained and ``ambiguous`` is
    ``True``.
    """

    def __init__(self, message, time, magnitude, units, correction_dir,
                 travel_offset, candidates):
        self.message = message
        self.time = time
        self.magnitude = magnitude
        self.units = units
        self.correction_dir = correction_dir
        self.travel_offset = travel_offset
        self.candidates = list(candidates)

    @property
    def ambiguous(self):
        return len(self.candidates) != 1

    @property
    def signed(self):
        """Route-frame deviation, or ``None`` when not uniquely attributed."""
        if len(self.candidates) == 1:
            return self.candidates[0].signed
        return None

    @property
    def side(self):
        """Route-frame ``'L'``/``'R'``, or ``None`` when not unique/zero."""
        if len(self.candidates) == 1:
            return self.candidates[0].side
        return None

    def __repr__(self):
        return '<CrossTrackObservation(time=%r, magnitude=%r, signed=%r, ' \
               'candidates=%d)>' % (
                   self.time, self.magnitude, self.signed,
                   len(self.candidates))


class RouteTrack(object):
    """A fixed route definition against which reports are interpreted.

    ``waypoints`` is the ordered waypoint id list defining the reference
    direction of every leg (``waypoints[i] -> waypoints[i+1]``).
    """

    def __init__(self, waypoints):
        waypoints = list(waypoints)
        if len(waypoints) < 2:
            raise ValueError('a route needs at least two waypoints')
        self.waypoints = waypoints
        self.legs = [Leg(waypoints[i], waypoints[i + 1])
                     for i in range(len(waypoints) - 1)]
        # A geometric base line can occur more than once (shared/parallel
        # legs); keep every matching leg so attribution stays a candidate set.
        self._pair_map = {}
        for leg in self.legs:
            key = frozenset((leg.origin, leg.dest))
            self._pair_map.setdefault(key, []).append(leg)
        self.observations = []

    def reversed(self):
        """Return the same base lines with the reference direction flipped."""
        return RouteTrack(list(reversed(self.waypoints)))

    def attribute(self, active_origin, active_dest):
        """Candidate legs for an active ``origin -> dest`` travel leg.

        Returns a list of ``(leg, forward)`` tuples.  More than one is
        returned when the same base line occurs more than once, which lets
        a caller keep candidates instead of guessing.
        """
        matches = self._pair_map.get(
            frozenset((active_origin, active_dest)), [])
        result = []
        for leg in matches:
            forward = leg.origin == active_origin and leg.dest == active_dest
            result.append((leg, forward))
        return result

    @staticmethod
    def _active_pairs(message, active):
        sentence = getattr(message, 'sentence_type', None)
        if active is not None:
            pairs = active
        elif sentence == 'RMB':
            pairs = [(message.origin_waypoint_id, message.dest_waypoint_id)]
        else:
            pairs = []
        normalized = []
        for pair in pairs:
            origin, dest = pair
            if origin is None or dest is None:
                continue
            normalized.append((origin, dest))
        return normalized

    def observe(self, message, time=None, active=None):
        """Interpret one parsed XTE/RMB against this route.

        ``active`` is an optional list of ``(origin, dest)`` travel legs the
        report may belong to (required for a bare XTE, which carries no
        waypoint ids; RMB derives it from its own fields).  Multiple entries
        model a waypoint handoff and produce multiple candidates.
        """
        magnitude, correction_dir, units = extract_cross_track(message)
        travel_value = travel_signed(magnitude, correction_dir)

        candidates = []
        seen = set()
        for origin, dest in self._active_pairs(message, active):
            for leg, forward in self.attribute(origin, dest):
                if leg in seen:
                    continue
                seen.add(leg)
                signed_value = route_signed(travel_value, forward)
                candidates.append(
                    LegDeviation(leg, forward, signed_value,
                                 side_of(signed_value)))

        observation = CrossTrackObservation(
            message, time, magnitude, units, correction_dir,
            travel_value, candidates)
        self.observations.append(observation)
        return observation

    def annotate(self, message, time=None, active=None):
        """Attach route-fixed deviations to an already parsed sentence.

        The sentence itself is returned unchanged in its NMEA rendering; the
        route data is added as ordinary python attributes.
        """
        observation = self.observe(message, time=time, active=active)
        message.route_magnitude = observation.magnitude
        message.route_signed = observation.signed
        message.route_side = observation.side
        message.route_ambiguous = observation.ambiguous
        message.route_candidates = observation.candidates
        return observation

    def _timed_observations(self):
        timed = [obs for obs in self.observations if obs.time is not None]
        if len(timed) != len(self.observations):
            raise ValueError(
                'every observation needs a numeric time for duration math')
        return sorted(timed, key=lambda obs: obs.time)

    def exceedance_intervals(self, threshold):
        """Merged ``[start, end)`` intervals where magnitude exceeds threshold.

        A sample at time ``t`` holds until the next sample, so the last
        sample contributes no forward interval.  The comparison is strict
        (``magnitude > threshold``); magnitude is orientation-independent,
        so reversing the route never changes these intervals.
        """
        timed = self._timed_observations()
        intervals = []
        start = None
        for index, obs in enumerate(timed):
            over = obs.magnitude is not None and obs.magnitude > threshold
            if over and start is None:
                start = obs.time
            if start is not None and (not over or index == len(timed) - 1):
                end = obs.time
                if end > start:
                    intervals.append((start, end))
                start = None
        return intervals

    def exceedance_duration(self, threshold):
        """Total time, in the units of the supplied timestamps, over limit."""
        return sum(end - start
                   for start, end in self.exceedance_intervals(threshold))
