"""Find complete, non-overlapping performances in a practice recording.

A student who repeats an exercise produces a sequence of note onsets in
which the same rhythm template occurs several times, at slightly
different tempos.  Choosing too many of those occurrences makes the
student look more fluent than they really are, so the useful unit is a
maximum-size set of *complete* and pairwise non-overlapping
performances.

The matching model used here is deliberately small and predictable:

* A template is a sequence of onset offsets.  ``[0, 1, 2, 4]`` and the
  equivalent inter-onset form ``[1, 1, 2]`` are both accepted.
* Every note of a performance must be present.  A prefix which matches
  the beginning of the template but stops early is not a performance.
* One rate multiplier is used for the whole performance.  It must lie in
  the supplied rate range.  Timing tolerance is optional and defaults to
  exact matching.
* Performances do not overlap when their keystroke ranges are disjoint.
* A performance is rejected when one of its keystrokes lies in a
  forbidden region or its span intersects one.

The result reports every performance which occurs in *some* maximum-size
solution, and for every keystroke the groups it can belong to.  This is
important for sampling: a note which can belong to two different groups
in two equally good solutions is ambiguous and should be avoided when
drawing independent samples.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Optional

__all__ = [
    "AmbiguityReport",
    "KeystrokeAmbiguity",
    "Performance",
    "PerformanceSelection",
    "RhythmTemplate",
    "extract_performances",
    "find_complete_performances",
    "find_maximal_performances",
    "find_optimal_performances",
    "find_performances",
    "segment_performances",
    "segment_practice",
    "select_performances",
]

_EPSILON = 1e-9


def _is_number(value):
    return isinstance(value, Real) and not isinstance(value, bool)


def _as_float(value, name):
    if not _is_number(value):
        raise TypeError(f'{name} must be a number, not {type(value).__name__}')
    return float(value)


def _close(first, second):
    return math.isclose(float(first), float(second),
                        rel_tol=1e-9, abs_tol=1e-9)


@dataclass(frozen=True)
class RhythmTemplate:
    """A normalized rhythm template.

    ``onsets`` always starts at zero and is strictly increasing.
    ``durations`` may contain ``None`` when note lengths are unknown.
    """

    onsets: tuple
    durations: tuple

    def __post_init__(self):
        if not self.onsets:
            raise ValueError('rhythm template must contain at least one note')
        if not _close(self.onsets[0], 0):
            raise ValueError('template onsets must start at zero')
        if len(self.durations) != len(self.onsets):
            raise ValueError('template durations must match number of notes')
        for first, second in zip(self.onsets, self.onsets[1:]):
            if second <= first:
                raise ValueError('template onsets must be strictly increasing')
        for duration in self.durations:
            if duration is not None and duration < 0:
                raise ValueError('template durations must be non-negative')

    @property
    def note_count(self):
        """Number of notes in one complete performance."""
        return len(self.onsets)

    @property
    def length(self):
        """Offset of the last onset in the template."""
        return self.onsets[-1]


@dataclass(frozen=True, order=True)
class Performance:
    """One complete occurrence of the practice template.

    ``start_index`` and ``end_index`` are inclusive keystroke indices.
    ``start`` and ``end`` are the corresponding times.  ``rate`` is the
    multiplier applied to the template (``1.0`` means the template is
    played at its written speed).
    """

    start_index: int
    end_index: int
    start: float
    end: float
    rate: float
    group_id: int = -1

    @property
    def group_number(self):
        """One-based group number."""
        return self.group_id + 1 if self.group_id >= 0 else None

    @property
    def note_count(self):
        """Number of keystrokes in this performance."""
        return self.end_index - self.start_index + 1

    @property
    def note_indices(self):
        """Inclusive range of keystroke indices in this performance."""
        return range(self.start_index, self.end_index + 1)

    @property
    def onset(self):
        """Absolute start time."""
        return self.start

    @property
    def speed(self):
        """Alias for the rate multiplier."""
        return self.rate

    @property
    def tempo(self):
        """Alias for the rate multiplier."""
        return self.rate

    def __len__(self):
        return self.note_count


class KeystrokeAmbiguity(frozenset):
    """Possible group ids for one keystroke.

    The value behaves like a :class:`frozenset` of group ids, so it can be
    compared directly with a set.  ``possible_positions`` gives the
    zero-based position(s) of the keystroke within those groups, and
    ``possible_group_ordinals`` gives its one-based group ordinal(s) in
    optimal selections.
    """

    def __new__(cls, index, onset, group_ids, positions, ordinals,
                can_be_unused):
        obj = super().__new__(cls, group_ids)
        obj.index = index
        obj.onset = onset
        obj.group_ids = frozenset(group_ids)
        obj.possible_groups = obj.group_ids
        obj.possible_positions = tuple(sorted(set(positions)))
        obj.possible_group_ordinals = tuple(sorted(set(ordinals)))
        obj.possible_group_numbers = tuple(group_id + 1
                                           for group_id in sorted(obj))
        obj.can_be_unused = bool(can_be_unused)
        return obj

    @property
    def ambiguous(self):
        """True when sampling this keystroke could select the wrong group."""
        return self.can_be_unused or len(self) > 1

    @property
    def is_ambiguous(self):
        """Alias for :attr:`ambiguous`."""
        return self.ambiguous

    def __repr__(self):
        return (f'KeystrokeAmbiguity(index={self.index!r}, '
                f'onset={self.onset!r}, groups={sorted(self)!r}, '
                f'positions={list(self.possible_positions)!r}, '
                f'ordinals={list(self.possible_group_ordinals)!r}, '
                f'can_be_unused={self.can_be_unused!r})')


class AmbiguityReport(Sequence):
    """Per-keystroke ambiguity information.

    Entries can be addressed by keystroke index or by onset time.
    Iteration yields :class:`KeystrokeAmbiguity` values in note order.
    """

    def __init__(self, entries):
        self._entries = tuple(entries)
        self._by_index = {entry.index: entry for entry in self._entries}
        self._by_onset = {}
        for entry in self._entries:
            self._by_onset.setdefault(entry.onset, entry)

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self._entries[key]
        if isinstance(key, bool):
            raise TypeError('keystroke key must be an index or onset time')
        if isinstance(key, int):
            if key < 0:
                key += len(self._entries)
            if key < 0 or key >= len(self._entries):
                raise IndexError('keystroke index out of range')
            return self._entries[key]
        if _is_number(key):
            return self._by_onset[float(key)]
        raise TypeError('keystroke key must be an index or onset time')

    def get(self, key, default=None):
        """Return an entry or *default* if the key is not present."""
        try:
            return self[key]
        except (KeyError, IndexError, TypeError):
            return default

    @property
    def entries(self):
        """Entries as a tuple in note order."""
        return self._entries

    @property
    def ambiguous_indices(self):
        """Indices of keystrokes which are not uniquely sampleable."""
        return tuple(entry.index for entry in self._entries
                     if entry.ambiguous)

    def possible_groups(self, key):
        """Return possible group ids for a note index or onset time."""
        return self[key].possible_groups

    def to_dict(self):
        """Return ``{onset: frozenset(group ids)}``."""
        return {entry.onset: entry.possible_groups for entry in self._entries}

    def __repr__(self):
        return f'AmbiguityReport({list(self._entries)!r})'


class PerformanceSelection(tuple):
    """Result of :func:`find_performances`.

    It is a two-item tuple ``(performances, ambiguity)``.  The properties
    name both parts, and ``all_optimal_performances`` contains every
    performance which participates in at least one maximum-size solution.
    Group ids used by the ambiguity report index into that tuple.
    """

    def __new__(cls, performances, ambiguity, max_count,
                all_optimal_performances, candidates):
        obj = super().__new__(cls, (tuple(performances), ambiguity))
        obj._max_count = int(max_count)
        obj._all_optimal = tuple(all_optimal_performances)
        obj._candidates = tuple(candidates)
        return obj

    @property
    def performances(self):
        """One deterministic maximum-size selection."""
        return self[0]

    @property
    def groups(self):
        """Alias for :attr:`performances`."""
        return self[0]

    @property
    def ambiguity(self):
        """Per-keystroke ambiguity report."""
        return self[1]

    @property
    def max_count(self):
        """Maximum number of non-overlapping complete performances."""
        return self._max_count

    @property
    def optimal_count(self):
        """Alias for :attr:`max_count`."""
        return self._max_count

    @property
    def all_optimal_performances(self):
        """All performances used by at least one optimal solution."""
        return self._all_optimal

    @property
    def all_groups(self):
        """Alias for :attr:`all_optimal_performances`."""
        return self._all_optimal

    def optimal_solutions(self, limit=1000):
        """Enumerate maximum-size selections, up to *limit* solutions.

        Solutions are tuples of :class:`Performance` objects.  A limit of
        ``None`` enumerates all solutions, which may be exponential.
        """
        if self._max_count == 0:
            return [()] if limit is None or limit > 0 else []

        active = list(self._all_optimal)
        ending_best = {candidate.performance.group_id:
                       candidate.best_starting_here
                       for candidate in self._candidates}
        solutions = []

        def compatible(first, second):
            return first.end_index < second.start_index

        def walk(chain, remaining):
            if limit is not None and len(solutions) >= limit:
                return
            if not remaining:
                solutions.append(tuple(chain))
                return
            options = [
                performance for performance in active
                if ending_best[performance.group_id] == remaining
                and (not chain or compatible(chain[-1], performance))
            ]
            options.sort(key=lambda performance: (
                performance.start_index, performance.end_index))
            for option in options:
                walk(chain + [option], remaining - 1)

        walk([], self._max_count)
        return solutions

    def to_dict(self):
        """Return a JSON-friendly representation of the result."""
        def encode(performance):
            return {
                'group_id': performance.group_id,
                'group_number': performance.group_number,
                'start_index': performance.start_index,
                'end_index': performance.end_index,
                'start': performance.start,
                'end': performance.end,
                'rate': performance.rate,
                'tempo': performance.tempo,
            }

        return {
            'max_count': self._max_count,
            'performances': [encode(performance)
                             for performance in self.performances],
            'all_optimal_performances': [
                encode(performance) for performance in self._all_optimal
            ],
            'ambiguity': {
                str(entry.index): {
                    'onset': entry.onset,
                    'possible_groups': sorted(entry.possible_groups),
                    'possible_group_numbers': list(
                        entry.possible_group_numbers),
                    'possible_positions': list(entry.possible_positions),
                    'possible_group_ordinals': list(
                        entry.possible_group_ordinals),
                    'can_be_unused': entry.can_be_unused,
                    'ambiguous': entry.ambiguous,
                }
                for entry in self.ambiguity
            },
        }

    def __repr__(self):
        return (f'PerformanceSelection(performances={self.performances!r}, '
                f'max_count={self._max_count!r}, '
                f'all_optimal_performances={self._all_optimal!r})')


def _cumulative(intervals):
    total = 0.0
    result = [0.0]
    for interval in intervals:
        total += _as_float(interval, 'interval')
        result.append(total)
    return result


def _notes_from_messages(messages, times_are_absolute=False):
    now = 0.0
    open_notes = {}
    notes = []
    for message in messages:
        if times_are_absolute:
            now = float(getattr(message, 'time', 0) or 0)
        else:
            now += float(getattr(message, 'time', 0) or 0)
        message_type = getattr(message, 'type', None)
        key = (getattr(message, 'channel', 0),
               getattr(message, 'note', 0))
        if message_type == 'note_on' and getattr(message, 'velocity', 1) > 0:
            open_notes.setdefault(key, []).append(len(notes))
            notes.append([now, None])
        elif message_type in ('note_off', 'note_on'):
            stack = open_notes.get(key)
            if stack:
                index = stack.pop(0)
                notes[index][1] = now - notes[index][0]
    return [_Note(time=time, duration=duration)
            for time, duration in notes]


def _normalize_template(template, units='auto', times_are_absolute=None):
    if isinstance(template, RhythmTemplate):
        return template
    if units not in ('auto', 'offsets', 'intervals'):
        raise ValueError("units must be 'auto', 'offsets' or 'intervals'")

    durations = None

    if isinstance(template, Mapping):
        for key in ('onsets', 'offsets', 'times', 'positions'):
            if key in template:
                onsets = template[key]
                break
        else:
            for key in ('intervals', 'gaps'):
                if key in template:
                    onsets = _cumulative(template[key])
                    break
            else:
                raise ValueError(
                    'template mapping needs onsets/offsets or intervals')
        for key in ('durations', 'lengths', 'note_durations'):
            if key in template:
                durations = template[key]
                break
        onsets = list(onsets)

    else:
        if hasattr(template, 'type'):
            values = [template]
        else:
            values = list(template)
        if not values:
            raise ValueError('rhythm template must not be empty')

        if all(hasattr(value, 'type') for value in values):
            message_notes = _notes_from_messages(values,
                                                 bool(times_are_absolute))
            if not message_notes:
                raise ValueError('rhythm template contains no note onsets')
            first_time = message_notes[0].time
            onsets = [note.time - first_time for note in message_notes]
            durations = [note.duration for note in message_notes]
        elif all(isinstance(value, (tuple, list)) and len(value) == 2
                 for value in values):
            onsets = [pair[0] for pair in values]
            durations = [pair[1] for pair in values]
        else:
            onsets = list(values)

        onsets = [_as_float(value, 'template onset') for value in onsets]
        if units == 'intervals':
            onsets = _cumulative(onsets)
        elif units == 'auto' and len(onsets) > 1:
            if not _close(onsets[0], 0):
                onsets = _cumulative(onsets)

    if not onsets:
        raise ValueError('rhythm template must contain at least one note')
    onsets = [_as_float(value, 'template onset') for value in onsets]
    first = onsets[0]
    onsets = tuple(onset - first for onset in onsets)

    if durations is None:
        durations = (None,) * len(onsets)
    else:
        durations = list(durations)
        if len(durations) != len(onsets):
            raise ValueError('template durations must match number of notes')
        durations = tuple(
            None if duration is None else _as_float(duration, 'duration')
            for duration in durations)

    return RhythmTemplate(onsets=onsets, durations=durations)


@dataclass(frozen=True)
class _Note:
    time: float
    duration: Optional[float] = None


def _normalize_notes(source, times_are_absolute=None):
    if hasattr(source, 'ticks_per_beat') and hasattr(source, 'merged_track'):
        notes = _notes_from_messages(source)
        return sorted(notes, key=lambda note: note.time)

    if hasattr(source, 'type'):
        source = [source]

    values = list(source)
    if not values:
        return []

    if all(hasattr(value, 'type') for value in values):
        notes = _notes_from_messages(values, bool(times_are_absolute))
        return sorted(notes, key=lambda note: note.time)

    notes = []
    for value in values:
        duration = None
        if isinstance(value, Mapping):
            if 'onset' in value:
                time = value['onset']
            elif 'time' in value:
                if times_are_absolute is False:
                    raise ValueError(
                        'delta note mappings are not supported; '
                        'pass absolute onset times')
                time = value['time']
            elif 'start' in value:
                time = value['start']
            else:
                raise ValueError('note mapping needs onset/time/start')
            duration = value.get('duration')
        elif hasattr(value, 'onset'):
            time = value.onset
            duration = getattr(value, 'duration', None)
        elif hasattr(value, 'start'):
            time = value.start
            duration = getattr(value, 'duration', None)
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            time, duration = value
        else:
            time = value

        notes.append(_Note(
            _as_float(time, 'note onset'),
            None if duration is None else _as_float(duration, 'duration'),
        ))
        if notes[-1].duration is not None and notes[-1].duration < 0:
            raise ValueError('note durations must be non-negative')

    notes.sort(key=lambda note: note.time)
    return notes


def _normalize_rate_range(rate_range, min_rate, max_rate):
    if rate_range is None:
        low, high = min_rate, max_rate
    elif _is_number(rate_range):
        if min_rate is not None or max_rate is not None:
            raise ValueError('pass either rate_range or min_rate/max_rate')
        value = float(rate_range)
        if value <= 0:
            raise ValueError('rates must be positive')
        return value, value
    else:
        try:
            low, high = rate_range
        except (TypeError, ValueError):
            raise ValueError('rate_range must be a number or (min, max)')
        if min_rate is not None or max_rate is not None:
            raise ValueError('pass either rate_range or min_rate/max_rate')

    has_minimum = low is not None
    has_maximum = high is not None
    low = 0.0 if low is None else _as_float(low, 'min_rate')
    high = math.inf if high is None else _as_float(high, 'max_rate')
    if (has_minimum and low <= 0) or (has_maximum and high <= 0):
        raise ValueError('rates must be positive')
    if high < low:
        raise ValueError('max_rate must not be smaller than min_rate')
    return low, high


def _normalize_forbidden(regions):
    if regions is None:
        return ()
    if isinstance(regions, Mapping):
        regions = [regions]
    values = list(regions)
    if len(values) == 2 and all(_is_number(value) for value in values):
        values = [values]

    result = []
    for region in values:
        if isinstance(region, Mapping):
            start = region.get('start', region.get('from'))
            end = region.get('end', region.get('to'))
        elif hasattr(region, 'start') and hasattr(region, 'end'):
            start, end = region.start, region.end
        else:
            try:
                start, end = region
            except (TypeError, ValueError):
                raise ValueError(
                    'forbidden regions must be (start, end) pairs')
        start = _as_float(start, 'forbidden start')
        end = _as_float(end, 'forbidden end')
        if end < start:
            raise ValueError('forbidden region end must not precede start')
        if end > start:
            result.append((start, end))
    return tuple(result)


@dataclass
class _Candidate:
    performance: Performance
    best_ending_here: int = 0
    best_starting_here: int = 0


def _fit_rate(notes, start_index, template, rate_low, rate_high,
              tolerance, relative_tolerance):
    if template.note_count == 1:
        if math.isinf(rate_high):
            return rate_low if rate_low > 0 else 1.0
        return (rate_low + rate_high) / 2

    start = notes[start_index].time
    lows = [rate_low]
    highs = [rate_high]
    weighted_time = 0.0
    weighted_template = 0.0

    for offset, note in zip(template.onsets[1:],
                            notes[start_index + 1:
                                  start_index + template.note_count]):
        distance = note.time - start
        weighted_time += offset * distance
        weighted_template += offset * offset

        if relative_tolerance:
            lows.append((distance - tolerance) /
                        (offset * (1 + relative_tolerance)))
            highs.append((distance + tolerance) /
                         (offset * (1 - relative_tolerance)))
        else:
            lows.append((distance - tolerance) / offset)
            highs.append((distance + tolerance) / offset)

    low = max(lows)
    high = min(highs)
    if low > high + _EPSILON:
        return None

    if tolerance == 0 and not relative_tolerance:
        ratios = [(notes[start_index + position].time - start) / offset
                  for position, offset
                  in enumerate(template.onsets[1:], start=1)]
        first = ratios[0]
        if not all(_close(first, ratio) for ratio in ratios):
            return None
        if not (rate_low - _EPSILON <= first <= rate_high + _EPSILON):
            return None
        return first

    rate = weighted_time / weighted_template
    return min(max(rate, low), high)


def _build_candidates(notes, template, rate_low, rate_high,
                      forbidden, tolerance, relative_tolerance):
    count = template.note_count
    candidates = []
    if count == 0 or len(notes) < count:
        return candidates

    last_duration = template.durations[-1]

    for start_index in range(len(notes) - count + 1):
        end_index = start_index + count - 1
        rate = _fit_rate(notes, start_index, template, rate_low, rate_high,
                         tolerance, relative_tolerance)
        if rate is None:
            continue

        start = notes[start_index].time
        end = notes[end_index].time
        if last_duration is not None:
            end += last_duration * rate

        rejected = False
        for region_start, region_end in forbidden:
            if start < region_end and end > region_start:
                rejected = True
            if any(region_start - _EPSILON <= note.time < region_end
                   for note in notes[start_index:end_index + 1]):
                rejected = True
            if rejected:
                break
        if rejected:
            continue

        candidates.append(_Candidate(Performance(
            start_index=start_index,
            end_index=end_index,
            start=start,
            end=end,
            rate=rate,
        )))
    return candidates


def _score_candidates(candidates, note_count):
    by_end = {}
    by_start = {}
    for candidate in candidates:
        by_end.setdefault(candidate.performance.end_index, []).append(candidate)
        by_start.setdefault(candidate.performance.start_index, []).append(candidate)

    prefix_best = [0] * max(note_count, 1)
    best = 0
    for index in range(note_count):
        for candidate in by_end.get(index, ()):
            start_index = candidate.performance.start_index
            candidate.best_ending_here = 1 + (
                prefix_best[start_index - 1] if start_index else 0)
            best = max(best, candidate.best_ending_here)
        prefix_best[index] = best

    suffix_best = [0] * (note_count + 1)
    best = 0
    for index in range(note_count - 1, -1, -1):
        for candidate in by_start.get(index, ()):
            candidate.best_starting_here = 1 + suffix_best[
                candidate.performance.end_index + 1]
            best = max(best, candidate.best_starting_here)
        suffix_best[index] = best

    return prefix_best, suffix_best


def _reconstruct(candidates, maximum):
    if maximum == 0:
        return []
    active = [candidate for candidate in candidates
              if candidate.best_ending_here + candidate.best_starting_here - 1
              == maximum]
    active.sort(key=lambda candidate: (
        candidate.performance.start_index,
        candidate.performance.end_index,
    ))

    chosen = []
    previous = None
    remaining = maximum
    while remaining:
        options = [candidate for candidate in active
                   if candidate.best_starting_here == remaining
                   and (previous is None
                        or previous.performance.end_index
                        < candidate.performance.start_index)]
        if not options:
            break
        candidate = options[0]
        chosen.append(candidate)
        previous = candidate
        remaining -= 1
    return chosen


def _with_group_id(performance, group_id):
    return Performance(
        start_index=performance.start_index,
        end_index=performance.end_index,
        start=performance.start,
        end=performance.end,
        rate=performance.rate,
        group_id=group_id,
    )


def find_performances(rhythm_template=None, note_onsets=None,
                      rate_range=None, forbidden_regions=None, *,
                      template=None, onsets=None, keystrokes=None,
                      note_events=None, notes=None,
                      note_times=None, keystroke_times=None,
                      min_rate=None, max_rate=None,
                      min_tempo=None, max_tempo=None,
                      tempo_range=None, speed_range=None,
                      forbidden=None, excluded_regions=None,
                      forbidden_zones=None, forbidden_intervals=None,
                      tolerance=0.0, relative_tolerance=0.0,
                      template_units='auto',
                      times_are_absolute=None):
    """Find the maximum number of complete, non-overlapping performances.

    Parameters
    ----------
    rhythm_template:
        The rhythm template.  It may be a sequence of onset offsets
        starting at zero (``[0, 1, 2]``), a sequence of inter-onset
        intervals (``[1, 1]``), a sequence of ``(onset, duration)``
        pairs, note messages, or a mapping with ``onsets``/``offsets``
        (or ``intervals``) and optional ``durations``.  ``template`` is
        an alias.
    note_onsets:
        Absolute note onset times, note messages, ``(onset, duration)``
        pairs, or a :class:`~mido.MidiFile`.  ``onsets`` is an alias.
    rate_range:
        ``(minimum, maximum)`` global rate multiplier.  A single number
        means exactly that rate.  ``min_rate`` and ``max_rate`` are
        keyword alternatives.
    forbidden_regions:
        Iterable of ``(start, end)`` half-open time ranges which must not
        be used.  A performance is rejected when one of its keystrokes
        lies in a region or its span intersects one.  ``forbidden`` and
        ``excluded_regions`` are aliases.
    tolerance:
        Absolute timing tolerance (same unit as the onsets).  The
        default of zero means exact rhythm matching.
    relative_tolerance:
        Optional tolerance as a fraction of each template interval.
    template_units:
        ``'auto'`` (default), ``'offsets'`` or ``'intervals'``.  In auto
        mode a template beginning with zero is read as offsets and any
        other multi-note numeric template as inter-onset intervals.
    times_are_absolute:
        For message input, whether the message times are already
        absolute.  Mido messages normally carry delta times, which is
        the default.

    Returns
    -------
    PerformanceSelection
        A tuple ``(performances, ambiguity)``.  ``performances`` is one
        deterministic optimal selection.  ``ambiguity`` maps every
        keystroke to the frozenset of group ids it may belong to in any
        optimal selection, and flags keystrokes which may also be unused.
        ``selection.all_optimal_performances`` contains every
        performance which occurs in at least one optimal selection; the
        group ids used by the report index into this tuple.

    Notes
    -----
    A locally matching prefix which cannot reach the end of the template
    is never returned: only intervals covering all template notes become
    candidates.  The maximum is found with interval-scheduling dynamic
    programming, so all candidates, not a greedy prefix, are considered.
    """
    if rhythm_template is None:
        rhythm_template = template
    onset_aliases = [value for value in (
        note_onsets, onsets, keystrokes, note_events, notes,
        note_times, keystroke_times) if value is not None]
    if len(onset_aliases) > 1:
        raise ValueError(
            'pass note_onsets through only one of note_onsets, onsets, '
            'keystrokes, note_events, notes, note_times or '
            'keystroke_times')
    if onset_aliases:
        note_onsets = onset_aliases[0]
    if rhythm_template is None:
        raise TypeError('missing rhythm_template')
    if note_onsets is None:
        raise TypeError('missing note_onsets')

    range_aliases = [value for value in (
        rate_range, tempo_range, speed_range) if value is not None]
    if len(range_aliases) > 1:
        raise ValueError(
            'pass rate range through only one of rate_range, tempo_range '
            'or speed_range')
    if range_aliases:
        rate_range = range_aliases[0]

    if min_rate is not None and min_tempo is not None:
        raise ValueError('pass only one of min_rate or min_tempo')
    if max_rate is not None and max_tempo is not None:
        raise ValueError('pass only one of max_rate or max_tempo')
    if min_rate is None:
        min_rate = min_tempo
    if max_rate is None:
        max_rate = max_tempo

    region_aliases = [value for value in (
        forbidden_regions, forbidden, excluded_regions,
        forbidden_zones, forbidden_intervals) if value is not None]
    if len(region_aliases) > 1:
        raise ValueError(
            'pass forbidden regions through only one alias')
    forbidden_regions = region_aliases[0] if region_aliases else ()

    if tolerance < 0:
        raise ValueError('tolerance must be non-negative')
    if relative_tolerance < 0:
        raise ValueError('relative_tolerance must be non-negative')
    if relative_tolerance >= 1:
        raise ValueError('relative_tolerance must be less than 1')

    template_value = _normalize_template(
        rhythm_template, units=template_units,
        times_are_absolute=times_are_absolute)
    normalized_notes = _normalize_notes(note_onsets,
                                        times_are_absolute=times_are_absolute)
    rate_low, rate_high = _normalize_rate_range(
        rate_range, min_rate, max_rate)
    forbidden_value = _normalize_forbidden(forbidden_regions)

    candidates = _build_candidates(normalized_notes, template_value,
                                   rate_low, rate_high,
                                   forbidden_value,
                                   float(tolerance),
                                   float(relative_tolerance))
    if not normalized_notes:
        return PerformanceSelection(
            performances=(), ambiguity=AmbiguityReport(()), max_count=0,
            all_optimal_performances=(), candidates=())

    prefix_best, suffix_best = _score_candidates(
        candidates, len(normalized_notes))
    maximum = prefix_best[-1]
    canonical = _reconstruct(candidates, maximum)

    active = [candidate for candidate in candidates
              if candidate.best_ending_here
              + candidate.best_starting_here - 1 == maximum]
    active.sort(key=lambda candidate: (
        candidate.performance.start_index,
        candidate.performance.end_index,
        candidate.performance.start,
    ))
    for group_id, candidate in enumerate(active):
        candidate.performance = _with_group_id(candidate.performance,
                                                   group_id)

    canonical_by_start = {candidate.performance.start_index: candidate
                          for candidate in active}
    canonical_performances = [
        canonical_by_start[candidate.performance.start_index].performance
        for candidate in canonical
        if candidate.performance.start_index in canonical_by_start
    ]

    coverage = {}
    for candidate in active:
        for index in candidate.performance.note_indices:
            coverage.setdefault(index, []).append((
                candidate.performance.group_id,
                index - candidate.performance.start_index,
                candidate.best_ending_here,
            ))

    entries = []
    for index, note in enumerate(normalized_notes):
        groups_and_positions = coverage.get(index, ())
        group_ids = {group_id for group_id, _, _ in groups_and_positions}
        positions = {position for _, position, _ in groups_and_positions}
        ordinals = {ordinal for _, _, ordinal in groups_and_positions}
        before = prefix_best[index - 1] if index else 0
        after = (suffix_best[index + 1]
                 if index + 1 < len(normalized_notes) else 0)
        can_be_unused = (before + after == maximum)
        entries.append(KeystrokeAmbiguity(
            index=index,
            onset=note.time,
            group_ids=group_ids,
            positions=positions,
            ordinals=ordinals,
            can_be_unused=can_be_unused,
        ))

    return PerformanceSelection(
        performances=canonical_performances,
        ambiguity=AmbiguityReport(entries),
        max_count=maximum,
        all_optimal_performances=[candidate.performance
                                  for candidate in active],
        candidates=active,
    )


def find_maximal_performances(rhythm_template=None, note_onsets=None,
                              rate_range=None, forbidden_regions=(),
                              **kwargs):
    """Like :func:`find_performances` but return only the performances."""
    return find_performances(rhythm_template, note_onsets, rate_range,
                             forbidden_regions, **kwargs).performances


find_complete_performances = find_performances
find_optimal_performances = find_performances
select_performances = find_performances
extract_performances = find_performances


def segment_performances(note_onsets, rhythm_template, rate_range=None,
                         forbidden_regions=(), **kwargs):
    """Call :func:`find_performances` with the recording first.

    This spelling is convenient when the onsets are the primary input.
    """
    return find_performances(rhythm_template, note_onsets, rate_range,
                             forbidden_regions, **kwargs)


segment_practice = segment_performances
