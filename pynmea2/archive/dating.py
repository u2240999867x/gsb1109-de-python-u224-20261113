"""
UTC date resolution for archived NMEA 0183 sentences.

Most shipboard sentences (GGA, GLL, ...) only carry an UTC time-of-day
(``hhmmss.ss``); full dates arrive occasionally in RMC (``DDMMYY``) or ZDA
sentences.  Archiving tolerates messages arriving out of order within a
bounded *delay*, so the date of a time-only sentence has to be inferred from
the nearby date anchors rather than assumed to be the arrival day.

Ordering model
--------------
The sentences of one voyage are walked in arrival order.  For two consecutive
sentences whose event times are ``a`` and ``b`` the archive accepts

    -max_delay <= b - a < 24h

The clock may fall back by at most ``max_delay`` (the out-of-order window)
and two adjacent sentences are always less than a day apart, so at most one
midnight can sit between them.  The rule gives the required behaviour:

* ``23:59`` arriving *after* ``00:01`` keeps the same date -- putting it on
  the next day would be a ~48h jump and is rejected.
* ``00:01`` arriving after ``23:59`` moves to the next day, because keeping
  the same date would roll the clock back almost 24h, beyond ``max_delay``.

RMC/ZDA sentences are *anchors* that pin a full UTC date.  The voyage is
split at the anchors into independent regions (the head before the first
anchor, each anchor-to-anchor segment, and the tail after the last one).
Inside every region each time-only sentence is offered every date the region
can span, then forward/backward reachability under the ordering window keeps
only the dates that participate in a valid assignment.

When two adjacent anchors cannot be joined by any valid assignment (for
example the second anchor dates backwards), the raw sentences between them
are reported as a :class:`DateConflict`.  The anchors themselves are factual
records and keep their stated date; only the time-only sentences inside the
broken join lose their date.  The conflict is confined to that segment: the
rest of the voyage and every other voyage can still be archived.
"""
import datetime

from ..nmea import NMEASentence
from ..types.talker import RMC, ZDA

__all__ = [
    'DateConflict',
    'DateResolution',
    'DateResolver',
    'VoyageResult',
]

# Adjacent sentences are assumed less than one day apart in event time.
_ONE_DAY = datetime.timedelta(days=1)


def _anchor_date(sentence):
    """Return the UTC date pinned by an RMC/ZDA sentence, else ``None``."""
    if isinstance(sentence, (RMC, ZDA)):
        try:
            date = sentence.datestamp
        except (ValueError, TypeError):
            return None
        if isinstance(date, datetime.date):
            return date
    return None


class DateConflict(Exception):
    """Two adjacent date anchors contradict each other.

    ``start_index``/``end_index`` bracket the affected segment, both anchors
    included; ``sentences`` holds the raw NMEA strings of that segment so an
    archive can park exactly the broken records.  The two anchor sentences
    remain fully dated (their RMC/ZDA date is factual); the time-only
    sentences between them carry no date.
    """

    def __init__(self, message, start_index, end_index, sentences):
        super(DateConflict, self).__init__(message)
        self.start_index = start_index
        self.end_index = end_index
        self.sentences = sentences

    def __repr__(self):
        return 'DateConflict(%r, indices=%d..%d, %d sentences)' % (
            self.args[0], self.start_index, self.end_index,
            len(self.sentences))


class DateResolution(object):
    """Possible UTC dates resolved for a single sentence.

    ``sentence``       the parsed NMEA sentence
    ``index``          its position in the voyage input
    ``possible_dates`` sorted list of feasible ``datetime.date`` values
    ``is_anchor``      whether the sentence itself carries a full date
    ``raw``            original NMEA string (for conflict reporting)
    """

    __slots__ = ('sentence', 'index', 'possible_dates', 'is_anchor', 'raw')

    def __init__(self, sentence, index, possible_dates, is_anchor, raw):
        self.sentence = sentence
        self.index = index
        self.possible_dates = possible_dates
        self.is_anchor = is_anchor
        self.raw = raw

    @property
    def date(self):
        """The unique resolved date, or ``None`` when ambiguous/unknown."""
        return self.possible_dates[0] if len(self.possible_dates) == 1 else None

    @property
    def datetime(self):
        """Fully qualified UTC ``datetime`` when the date is unique."""
        if self.date is None or getattr(self.sentence, 'timestamp', None) is None:
            return None
        return datetime.datetime.combine(self.date, self.sentence.timestamp)

    def __repr__(self):
        return '<DateResolution(index=%d, dates=%r, anchor=%r)>' % (
            self.index, self.possible_dates, self.is_anchor)


class VoyageResult(object):
    """Outcome of resolving one voyage.

    ``resolutions`` is always populated.  Time-only sentences inside a
    conflicting segment carry an empty date list (the anchors themselves
    stay dated), while every other sentence is still resolved and can be
    archived.
    """

    __slots__ = ('voyage_id', 'resolutions', 'conflicts')

    def __init__(self, voyage_id, resolutions, conflicts):
        self.voyage_id = voyage_id
        self.resolutions = resolutions
        self.conflicts = conflicts

    @property
    def ok(self):
        return not self.conflicts

    def __repr__(self):
        return '<VoyageResult(%r, %d resolutions, %d conflicts)>' % (
            self.voyage_id, len(self.resolutions), len(self.conflicts))


class DateResolver(object):
    """Assign possible UTC dates to time-only NMEA sentences.

    ``max_delay`` is the out-of-order window tolerated by the archive, given
    as a :class:`~datetime.timedelta` or a number of seconds.  It must be
    shorter than 24 hours.
    """

    def __init__(self, max_delay=datetime.timedelta(0)):
        if isinstance(max_delay, datetime.timedelta):
            delay = max_delay
        else:
            delay = datetime.timedelta(seconds=float(max_delay))
        if delay < datetime.timedelta(0) or delay >= _ONE_DAY:
            raise ValueError('max_delay must be in [0, 24h)')
        self.max_delay = delay

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def resolve(self, sentences):
        """Resolve the dates of every sentence of one voyage.

        ``sentences`` is an iterable of parsed :class:`NMEASentence`
        objects or raw NMEA strings.  Returns a list of
        :class:`DateResolution`.

        Raises the first :class:`DateConflict` if adjacent anchors
        contradict each other; use :meth:`inspect` or
        :meth:`resolve_voyages` to collect conflicts without aborting.
        """
        parsed = [self._coerce(sentence) for sentence in sentences]
        resolutions, conflicts = self._resolve(parsed)
        if conflicts:
            raise conflicts[0]
        return resolutions

    def inspect(self, sentences):
        """Like :meth:`resolve` but returns ``(resolutions, conflicts)``.

        The time-only records inside a conflicting segment carry an empty
        date list (the anchors themselves stay dated) instead of aborting,
        so the unaffected sentences remain available for archiving.
        """
        parsed = [self._coerce(sentence) for sentence in sentences]
        return self._resolve(parsed)

    def resolve_voyages(self, voyages):
        """Resolve several voyages independently.

        ``voyages`` is an iterable of ``(voyage_id, sentences)`` pairs; a
        contradiction in one voyage never affects the others.  Returns a
        list of :class:`VoyageResult`.
        """
        results = []
        for voyage_id, sentences in voyages:
            parsed = [self._coerce(sentence) for sentence in sentences]
            resolutions, conflicts = self._resolve(parsed)
            results.append(VoyageResult(voyage_id, resolutions, conflicts))
        return results

    # ------------------------------------------------------------------ #
    # core algorithm
    # ------------------------------------------------------------------ #
    def _resolve(self, parsed):
        length = len(parsed)
        dates_per_index = [set() for _ in range(length)]
        anchors = self._collect_anchors(parsed)

        if not anchors:
            # Without any full-date anchor the absolute UTC date is
            # unknowable; every sentence stays undated rather than guessed.
            return self._build_resolutions(parsed, dates_per_index), []

        conflicts = []

        for anchor_index, anchor_date in anchors:
            dates_per_index[anchor_index] = {anchor_date}

        head_nodes = [index for index in range(anchors[0][0])
                      if self._has_time(parsed[index])]
        if head_nodes:
            # A chain of m edges before the anchor can cross at most m
            # midnights, so the furthest head date is D - len(head_nodes).
            anchor_index, anchor_date = anchors[0]
            earliest = anchor_date - _ONE_DAY * len(head_nodes)
            bounds = {index: (earliest, anchor_date) for index in head_nodes}
            if self._has_time(parsed[anchor_index]):
                head_nodes.append(anchor_index)
                bounds[anchor_index] = (anchor_date, anchor_date)
            self._propagate(parsed, head_nodes, dates_per_index,
                            candidate_dates=bounds)

        for pos in range(len(anchors) - 1):
            left_index, left_date = anchors[pos]
            right_index, right_date = anchors[pos + 1]
            segment = [index for index in range(left_index, right_index + 1)
                       if self._has_time(parsed[index])]
            conflict = self._resolve_anchor_segment(
                parsed, segment, left_index, left_date,
                right_index, right_date, dates_per_index)
            if conflict is not None:
                conflicts.append(conflict)

        tail_nodes = [index for index in range(anchors[-1][0] + 1, length)
                      if self._has_time(parsed[index])]
        if tail_nodes:
            # Each edge away from the anchor can cross at most one midnight.
            anchor_index, anchor_date = anchors[-1]
            latest = anchor_date + _ONE_DAY * len(tail_nodes)
            bounds = {index: (anchor_date, latest) for index in tail_nodes}
            if self._has_time(parsed[anchor_index]):
                tail_nodes.insert(0, anchor_index)
                bounds[anchor_index] = (anchor_date, anchor_date)
            self._propagate(parsed, tail_nodes, dates_per_index,
                            candidate_dates=bounds)

        return self._build_resolutions(parsed, dates_per_index), conflicts

    def _resolve_anchor_segment(self, parsed, nodes, left_index, left_date,
                                right_index, right_date, dates_per_index):
        if right_date < left_date:
            return self._mark_conflict(
                parsed, left_index, right_index, left_date, right_date,
                dates_per_index)

        candidate_dates = {}
        for index in nodes:
            if index == left_index:
                candidate_dates[index] = (left_date, left_date)
            elif index == right_index:
                candidate_dates[index] = (right_date, right_date)
            else:
                candidate_dates[index] = (left_date, right_date)

        survivors = self._propagate(parsed, nodes, dates_per_index,
                                    candidate_dates)
        if any(not survivors[index] for index in nodes):
            return self._mark_conflict(
                parsed, left_index, right_index, left_date, right_date,
                dates_per_index)
        return None

    def _propagate(self, parsed, nodes, dates_per_index, candidate_dates):
        """Keep only candidate datetimes participating in a valid assignment.

        Forward reachability from the first node and backward reachability
        from the last node under the ordering window; a candidate survives
        only when it is reachable from both ends.
        """
        if not nodes:
            return {}
        candidates = {
            index: self._candidate_datetimes(parsed[index], candidate_dates[index])
            for index in nodes
        }
        forward = self._reach(nodes, candidates, forward=True)
        backward = self._reach(nodes, candidates, forward=False)

        survivors = {}
        for index in nodes:
            surviving = {
                candidate for candidate in candidates[index]
                if candidate in forward[index] and candidate in backward[index]
            }
            survivors[index] = surviving
            dates_per_index[index] = {candidate.date() for candidate in surviving}
        return survivors

    def _reach(self, nodes, candidates, forward):
        ordered = nodes if forward else list(reversed(nodes))
        reach = {index: set() for index in nodes}
        reach[ordered[0]] = set(candidates[ordered[0]])
        for pos in range(1, len(ordered)):
            prev_index = ordered[pos - 1]
            index = ordered[pos]
            for candidate in candidates[index]:
                if any(self._edge_ok(previous, candidate) if forward
                       else self._edge_ok(candidate, previous)
                       for previous in reach[prev_index]):
                    reach[index].add(candidate)
        return reach

    def _edge_ok(self, earlier, later):
        """Ordering window: -max_delay <= later - earlier < 24h."""
        difference = later - earlier
        return -self.max_delay <= difference < _ONE_DAY

    def _candidate_datetimes(self, sentence, date_bounds):
        first_date, last_date = date_bounds
        days = (last_date - first_date).days
        return [
            datetime.datetime.combine(
                first_date + datetime.timedelta(days=day), sentence.timestamp)
            for day in range(days + 1)
        ]

    def _mark_conflict(self, parsed, left_index, right_index,
                       left_date, right_date, dates_per_index):
        for index in range(left_index, right_index + 1):
            anchor = _anchor_date(parsed[index])
            if anchor is not None:
                # An anchor's own RMC/ZDA record is still a factual, fully
                # dated sentence; only the join between the two anchors is
                # broken, so anchors keep their stated date.
                dates_per_index[index] = {anchor}
            else:
                # Interior, time-only sentences have no consistent date.
                dates_per_index[index] = set()
        return DateConflict(
            'date anchors at index %d (%s) and %d (%s) cannot be reconciled '
            'within a %s out-of-order delay' % (
                left_index, left_date.isoformat(),
                right_index, right_date.isoformat(), self.max_delay),
            left_index, right_index,
            [self._raw(parsed[index])
             for index in range(left_index, right_index + 1)])

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _has_time(sentence):
        return isinstance(
            getattr(sentence, 'timestamp', None), datetime.time)

    @staticmethod
    def _collect_anchors(parsed):
        return [(index, _anchor_date(sentence))
                for index, sentence in enumerate(parsed)
                if _anchor_date(sentence) is not None]

    @staticmethod
    def _build_resolutions(parsed, dates_per_index):
        return [
            DateResolution(
                sentence, index, sorted(dates_per_index[index]),
                _anchor_date(sentence) is not None,
                DateResolver._raw(sentence))
            for index, sentence in enumerate(parsed)
        ]

    def _coerce(self, sentence):
        if isinstance(sentence, NMEASentence):
            return sentence
        if isinstance(sentence, bytes):
            sentence = sentence.decode('ascii', errors='replace')
        from .. import parse as parse_sentence
        return parse_sentence(sentence)

    @staticmethod
    def _raw(sentence):
        raw = sentence.render()
        if isinstance(raw, bytes):
            raw = raw.decode('ascii')
        return raw
