"""Reconcile position reports received through two repeaters.

Two repeaters can receive the same positioning sentence from one base
station.  Their clocks are recorded with one-second resolution and their
forwarding quality flags are independent of the position itself, so a
quality flag cannot be used as an identity key.

This module keeps every reception as a separate object.  A
:class:`Reception` wraps the parsed NMEA sentence and the name of the
repeater that supplied it.  :func:`match_retransmissions` then builds a
bipartite graph of *possible* retransmissions:

* the receptions must come from different repeaters;
* their truncated timestamps must be within ``reception_delay`` seconds;
* their positions must be within ``position_error`` metres.

The graph is deliberately not collapsed transitively.  Three records that
are pairwise close can still describe two or three distinct fixes.  A fix
is therefore an explicit pairing of one reception from each repeater, and
one reception can belong to at most one pair.

For every connected group the module enumerates all maximal one-to-one
correspondences.  A group with a single correspondence has confirmed
retransmissions.  A group with several correspondences is returned as an
:class:`AmbiguityGroup`; each :class:`Correspondence` lists its pairs and
the number of fixes implied by that correspondence.  No arbitrary choice
is made between ambiguous alternatives.
"""

from __future__ import unicode_literals

import datetime
import math

from .nmea import NMEASentence

__all__ = [
    'Reception',
    'Fix',
    'Correspondence',
    'AmbiguityGroup',
    'Reconciliation',
    'match_retransmissions',
]

_EARTH_RADIUS_M = 6371008.8


def _is_finite(value):
    return not (math.isnan(value) or math.isinf(value))


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two signed decimal-degree positions."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) *
         math.sin(dlambda / 2.0) ** 2)
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _as_sentence(value):
    if isinstance(value, NMEASentence):
        return value
    return NMEASentence.parse(value)


def _sentence_quality(sentence):
    """Return the forwarding-quality-like flag exposed by a sentence."""
    for name in ('gps_qual', 'quality', 'mode_indicator', 'status'):
        if hasattr(sentence, name):
            value = getattr(sentence, name)
            if value is not None:
                return value
    return None


def _sentence_date(sentence):
    value = getattr(sentence, 'datestamp', None)
    if value is not None:
        return value
    value = getattr(sentence, 'date', None)
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def _time_of_day_seconds(value):
    """Whole seconds since midnight, ignoring fractions of a second."""
    return value.hour * 3600 + value.minute * 60 + value.second


def _timestamp_delta_seconds(left, right):
    """Difference between two reception times, in whole seconds.

    When both records have a date the difference is anchored to that
    date, including a midnight crossover.  Otherwise the time of day is
    compared circularly so a pair spanning midnight is still considered.
    Fractions of a second are discarded because the reception timestamps
    are truncated to whole seconds.
    """
    left_seconds = _time_of_day_seconds(left.timestamp)
    right_seconds = _time_of_day_seconds(right.timestamp)

    if left.date is not None and right.date is not None:
        days = abs((left.date - right.date).days)
        return float(abs(days * 86400 + left_seconds - right_seconds))

    delta = abs(left_seconds - right_seconds)
    return float(min(delta, 86400 - delta))


class Reception(object):
    """One positioning sentence received from one repeater.

    ``sentence`` may be a parsed :class:`pynmea2.NMEASentence` or a raw
    NMEA string.  ``forwarder`` identifies the repeater.  ``quality`` may
    be supplied explicitly when the import pipeline carries a forwarding
    quality flag outside the NMEA sentence; otherwise a suitable flag is
    read from the sentence (for example ``gps_qual`` on GGA).

    ``station`` is an optional base-station identifier.  Records with
    different non-``None`` station values are never paired.

    ``received_at`` is optional local reception time.  It is used as a
    fallback when the sentence has no timestamp and, when it carries a
    date, to anchor time-of-day comparisons.  The NMEA timestamp remains
    the primary time used for matching.
    """

    def __init__(self, sentence, forwarder, quality=None, station=None,
                 received_at=None, date=None):
        self.sentence = _as_sentence(sentence)
        self.forwarder = forwarder
        self.station = station
        self.received_at = received_at

        timestamp = getattr(self.sentence, 'timestamp', None)
        if timestamp is None and isinstance(received_at, datetime.datetime):
            timestamp = received_at.time()
        if timestamp is None:
            raise ValueError('reception has no timestamp')
        self.timestamp = timestamp

        record_date = date or _sentence_date(self.sentence)
        if record_date is None and isinstance(received_at, datetime.datetime):
            record_date = received_at.date()
        self.date = record_date

        self.quality = (_sentence_quality(self.sentence)
                        if quality is None else quality)

        latitude = getattr(self.sentence, 'latitude', None)
        longitude = getattr(self.sentence, 'longitude', None)
        raw_latitude = getattr(self.sentence, 'lat', None)
        raw_longitude = getattr(self.sentence, 'lon', None)
        if raw_latitude == '' or raw_longitude == '':
            raise ValueError('sentence does not contain a position')
        if latitude is None or longitude is None:
            raise ValueError('sentence does not contain a position')
        if not _is_finite(latitude) or not _is_finite(longitude):
            raise ValueError('sentence contains a non-finite position')
        self.latitude = latitude
        self.longitude = longitude

    @property
    def datetime(self):
        """Date and time as a :class:`datetime.datetime`, if a date exists."""
        if self.date is None:
            return None
        return datetime.datetime.combine(self.date, self.timestamp)

    @property
    def seconds_of_day(self):
        """Whole seconds since midnight."""
        return _time_of_day_seconds(self.timestamp)

    @property
    def raw(self):
        """The original NMEA sentence as text."""
        return str(self.sentence)

    def __repr__(self):
        return ('<Reception(%s, %s, %s, %s, quality=%r)>'
                % (self.forwarder, self.timestamp, self.latitude,
                   self.longitude, self.quality))


class Fix(object):
    """A physical fix represented by one or two reception records."""

    def __init__(self, receptions):
        self.receptions = tuple(receptions)
        if not 1 <= len(self.receptions) <= 2:
            raise ValueError('a fix contains one or two receptions')

    @property
    def is_retransmission(self):
        return len(self.receptions) == 2

    @property
    def forwarders(self):
        return tuple(reception.forwarder for reception in self.receptions)

    @property
    def qualities(self):
        return tuple(reception.quality for reception in self.receptions)

    def __len__(self):
        return len(self.receptions)

    def __iter__(self):
        return iter(self.receptions)

    def __repr__(self):
        return '<Fix(%s)>' % ', '.join(
            repr(reception) for reception in self.receptions)


class Correspondence(object):
    """One feasible way to pair the receptions in a group.

    ``pairs`` contains reception pairs.  ``fixes`` is the complete
    partition of the group into those pairs and the remaining singleton
    receptions.  ``fix_count`` is ``len(fixes)``, i.e. the number of
    distinct fixes implied by this correspondence.
    """

    def __init__(self, pairs, records):
        self.pairs = tuple(pairs)
        self.records = tuple(records)
        self.fixes = self._build_fixes()

    def _build_fixes(self):
        partner = {}
        for left, right in self.pairs:
            partner[id(left)] = right
            partner[id(right)] = left

        emitted = set()
        fixes = []
        for record in self.records:
            if id(record) in emitted:
                continue
            other = partner.get(id(record))
            if other is None:
                fixes.append(Fix([record]))
            else:
                fixes.append(Fix([record, other]))
                emitted.add(id(record))
                emitted.add(id(other))
        return tuple(fixes)

    @property
    def matched_count(self):
        return len(self.pairs)

    @property
    def fix_count(self):
        return len(self.fixes)

    def __repr__(self):
        return '<Correspondence(pairs=%d, fixes=%d)>' % (
            self.matched_count, self.fix_count)


class AmbiguityGroup(object):
    """A connected group with more than one feasible correspondence."""

    def __init__(self, records, correspondences, truncated=False):
        self.records = tuple(records)
        self.correspondences = tuple(correspondences)
        self.truncated = bool(truncated)

    @property
    def options(self):
        """Alias of ``correspondences``."""
        return self.correspondences

    @property
    def fix_counts(self):
        """Fix count for each correspondence, in the same order."""
        return tuple(option.fix_count for option in self.correspondences)

    @property
    def distinct_fix_counts(self):
        """Sorted distinct fix counts among the listed correspondences."""
        return sorted(set(self.fix_counts))

    @property
    def shared_pairs(self):
        """Pairs present in every listed correspondence.

        When enumeration was truncated this is ``None`` because the
        listed correspondences are not the complete set.
        """
        if self.truncated or not self.correspondences:
            return None
        shared = set()
        for pair in self.correspondences[0].pairs:
            key = frozenset((id(pair[0]), id(pair[1])))
            if all(any(frozenset((id(a), id(b))) == key
                       for a, b in option.pairs)
                   for option in self.correspondences[1:]):
                shared.add(key)

        result = []
        for left, right in self.correspondences[0].pairs:
            if frozenset((id(left), id(right))) in shared:
                result.append((left, right))
        return tuple(result)

    def __repr__(self):
        return '<AmbiguityGroup(records=%d, correspondences=%d)>' % (
            len(self.records), len(self.correspondences))


class Reconciliation(object):
    """Result of matching receptions from two repeaters.

    ``fixes`` contains the confirmed fixes: every pair from a group with
    one correspondence, plus every singleton reception that is not part
    of an ambiguity group.  Receptions in ambiguous groups appear only
    in ``ambiguous_groups`` so that callers do not accidentally treat one
    arbitrary alternative as confirmed.
    """

    def __init__(self, receptions, fixes, ambiguous_groups,
                 position_error, reception_delay):
        self.receptions = tuple(receptions)
        self.fixes = tuple(fixes)
        self.ambiguous_groups = tuple(ambiguous_groups)
        self.position_error = position_error
        self.reception_delay = reception_delay

    @property
    def retransmissions(self):
        """Confirmed fixes that contain two reception records."""
        return tuple(fix for fix in self.fixes if fix.is_retransmission)

    @property
    def singletons(self):
        """Confirmed fixes that contain one reception record."""
        return tuple(fix for fix in self.fixes if not fix.is_retransmission)

    @property
    def is_ambiguous(self):
        return bool(self.ambiguous_groups)

    @property
    def fix_count(self):
        """Number of confirmed fixes, or ``None`` when ambiguity remains."""
        if self.ambiguous_groups:
            return None
        return len(self.fixes)

    def __repr__(self):
        return ('<Reconciliation(receptions=%d, fixes=%d, '
                'ambiguous_groups=%d)>'
                % (len(self.receptions), len(self.fixes),
                   len(self.ambiguous_groups)))


def _maximal_matchings(left_records, right_records, adjacency, limit, order):
    """Enumerate maximal one-to-one matchings in a bipartite graph.

    A matching is maximal when no further pair could be added to it: every
    reception that is still a singleton has no compatible, still-unpaired
    reception on the other side.  Different maximal matchings can pair a
    different number of records, so they can imply different fix counts.

    Returns ``(matchings, truncated)``.  Each matching is a sorted tuple
    of ``(left, right)`` pairs.  ``order`` maps ``id(record)`` to the
    record's input position, which keeps the output deterministic.
    """
    matchings = []
    state = {'truncated': False}

    def is_maximal(pairs):
        used_right = set(id(right) for _, right in pairs)
        matched_left = set(id(left) for left, _ in pairs)
        for left in left_records:
            if id(left) in matched_left:
                continue
            if any(id(right) not in used_right
                   for right in adjacency.get(id(left), ())):
                return False
        return True

    def visit(index, pairs, used_right):
        if state['truncated']:
            return
        if index == len(left_records):
            if is_maximal(pairs):
                canonical = tuple(sorted(
                    pairs, key=lambda pair: (order[id(pair[0])],
                                             order[id(pair[1])])))
                matchings.append(canonical)
                if limit is not None and len(matchings) > limit:
                    state['truncated'] = True
            return

        left = left_records[index]
        visit(index + 1, pairs, used_right)

        for right in adjacency.get(id(left), ()):
            if id(right) in used_right:
                continue
            used_right.add(id(right))
            pairs.append((left, right))
            visit(index + 1, pairs, used_right)
            pairs.pop()
            used_right.remove(id(right))

    visit(0, [], set())

    truncated = state['truncated']
    if truncated:
        matchings = matchings[:limit]
    matchings.sort(key=lambda matching: tuple(
        (order[id(left)], order[id(right)]) for left, right in matching))
    return matchings, truncated


def _components(vertices, adjacency):
    """Connected components containing at least one edge."""
    seen = set()
    components = []
    for vertex in vertices:
        if id(vertex) in seen or not adjacency.get(id(vertex)):
            continue
        stack = [vertex]
        seen.add(id(vertex))
        component = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in adjacency.get(id(current), ()):
                if id(neighbour) not in seen:
                    seen.add(id(neighbour))
                    stack.append(neighbour)
        components.append(component)
    return components


def _validate_number(name, value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError('%s must be a number' % name)
    if not _is_finite(number) or number < 0:
        raise ValueError('%s must be a finite non-negative number' % name)
    return number


def match_retransmissions(receptions, position_error, reception_delay=1.0,
                          max_ambiguity_options=1000):
    """Match confirmable retransmissions between two repeaters.

    ``position_error`` is the maximum great-circle distance in metres
    between two reports that may describe the same fix.  ``reception_delay``
    is the maximum difference in seconds between their truncated
    timestamps.  Both tolerances are inclusive.

    Records are never merged transitively.  Each physical fix is either a
    confirmed one-to-one pair or a singleton reception; groups with more
    than one feasible correspondence are returned as
    :class:`AmbiguityGroup` objects.

    ``max_ambiguity_options`` bounds enumeration of a single group.  If a
    group has more maximal correspondences than the limit, its
    ``truncated`` flag is set and only the first ``limit`` options are
    retained.  Pass ``None`` for no limit.
    """
    position_error = _validate_number('position_error', position_error)
    reception_delay = _validate_number('reception_delay', reception_delay)
    if max_ambiguity_options is not None:
        if (not isinstance(max_ambiguity_options, int) or
                max_ambiguity_options < 1):
            raise ValueError(
                'max_ambiguity_options must be a positive integer')

    records = list(receptions)
    if len(set(id(record) for record in records)) != len(records):
        raise ValueError('each Reception must be passed only once')
    order = dict((id(record), index) for index, record in enumerate(records))

    forwarders = []
    for record in records:
        if record.forwarder not in forwarders:
            forwarders.append(record.forwarder)
    if len(forwarders) > 2:
        raise ValueError('expected receptions from at most two forwarders')

    if len(forwarders) < 2:
        fixes = tuple(Fix([record]) for record in records)
        return Reconciliation(records, fixes, (), position_error,
                              reception_delay)

    left_records = [record for record in records
                    if record.forwarder == forwarders[0]]
    right_records = [record for record in records
                     if record.forwarder == forwarders[1]]

    adjacency = {}
    for left in left_records:
        for right in right_records:
            if (left.station is not None and right.station is not None and
                    left.station != right.station):
                continue
            if (_timestamp_delta_seconds(left, right) >
                    reception_delay):
                continue
            distance = _haversine_m(left.latitude, left.longitude,
                                    right.latitude, right.longitude)
            if distance > position_error:
                continue
            adjacency.setdefault(id(left), []).append(right)
            adjacency.setdefault(id(right), []).append(left)

    for neighbours in adjacency.values():
        neighbours.sort(key=lambda record: order[id(record)])

    fixes = []
    groups = []
    in_group = set()
    for component in _components(records, adjacency):
        left = sorted((record for record in component
                       if record.forwarder == forwarders[0]),
                      key=lambda record: order[id(record)])
        right = sorted((record for record in component
                        if record.forwarder == forwarders[1]),
                       key=lambda record: order[id(record)])
        matchings, truncated = _maximal_matchings(
            left, right, adjacency, max_ambiguity_options, order)

        if not matchings:
            continue

        if len(matchings) == 1 and not truncated:
            pairs = matchings[0]
            partner = {}
            for a, b in pairs:
                partner[id(a)] = b
                partner[id(b)] = a
            emitted = set()
            for record in sorted(component,
                                 key=lambda item: order[id(item)]):
                if id(record) in emitted:
                    continue
                other = partner.get(id(record))
                if other is None:
                    fixes.append(Fix([record]))
                else:
                    fixes.append(Fix([record, other]))
                    emitted.add(id(record))
                    emitted.add(id(other))
        else:
            ordered = sorted(component, key=lambda item: order[id(item)])
            correspondences = [Correspondence(pairs, ordered)
                               for pairs in matchings]
            groups.append(AmbiguityGroup(ordered, correspondences, truncated))
            in_group.update(id(record) for record in component)

    for record in records:
        if id(record) in in_group:
            continue
        if any(record in fix.receptions for fix in fixes):
            continue
        fixes.append(Fix([record]))

    fixes.sort(key=lambda fix: min(order[id(reception)]
                                   for reception in fix.receptions))
    groups.sort(key=lambda group: min(order[id(record)]
                                      for record in group.records))

    return Reconciliation(records, fixes, groups, position_error,
                          reception_delay)
