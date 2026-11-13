# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Tempo maps and queryable timelines for MIDI files.

A :class:`TempoMap` converts absolute ticks to seconds for one time base.
Type 0 and type 1 files share a single time base, so tempo changes from every
track contribute to the same map.  Every track of a type 2 (asynchronous)
file has its own time base and therefore its own map.

A :class:`Timeline` combines the map (or maps) with the messages in the file
and exposes absolute ticks and seconds without modifying the tracks.  It is
available from :attr:`mido.MidiFile.timeline`.
"""

from bisect import bisect_right
from dataclasses import dataclass, field
from numbers import Integral, Real
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

DEFAULT_TEMPO = 500000
DEFAULT_TICKS_PER_BEAT = 480


class EventTable(list):
    """A list of event records that can also be called as a method.

    This lets ``timeline.event_table`` and ``timeline.event_table(track)``
    both work, whether a caller treats it as data or as a query method.
    """

    def __call__(self, track: Optional[int] = None) -> 'EventTable':
        if track is None:
            return self
        return EventTable(row for row in self if row['track'] == track)


class TempoConflictError(ValueError):
    """Raised when an exact time is requested after a tempo conflict.

    A conflict makes absolute times ambiguous, so ``strict=True`` refuses to
    guess.  Without ``strict`` the last declaration at the conflicting tick
    is used, matching sequential playback.
    """


@dataclass(frozen=True)
class TempoChange:
    """A ``set_tempo`` message at an absolute tick.

    ``track`` is the index of the track containing the message and ``index``
    is the message index within that track.
    """

    tick: int
    tempo: int
    track: int
    index: int

    @property
    def microseconds_per_beat(self) -> int:
        return self.tempo

    @property
    def absolute_tick(self) -> int:
        return self.tick

    def as_tuple(self) -> Tuple[int, int]:
        return self.tick, self.tempo

    def __iter__(self) -> Iterator[int]:
        return iter(self.as_tuple())

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> int:
        return self.as_tuple()[index]


@dataclass(frozen=True)
class TempoSegment:
    """A constant-tempo interval, starting at ``start_tick``.

    ``end_tick`` is the tick of the next tempo change, or ``None`` for the
    final segment.  If several different tempos are declared at
    ``start_tick``, ``tempos`` lists all of them, ``tempo`` is the last
    declaration in playback order, and the segment is *ambiguous*: times
    after ``start_tick`` depend on which declaration is chosen.
    """

    start_tick: int
    end_tick: Optional[int]
    tempos: Tuple[int, ...]
    tempo: int
    tracks: Tuple[int, ...]
    changes: Tuple[TempoChange, ...]
    start_seconds: float
    end_seconds: Optional[float]

    _ticks_per_beat: int = field(
        default=DEFAULT_TICKS_PER_BEAT, init=False, repr=False, compare=False)

    @property
    def tick(self) -> int:
        return self.start_tick

    @property
    def tempo_candidates(self) -> Tuple[int, ...]:
        return self.tempos

    @property
    def is_ambiguous(self) -> bool:
        return len(self.tempos) > 1

    @property
    def interval(self) -> Tuple[int, Optional[int]]:
        """This segment as ``(start_tick, end_tick)``."""
        return self.start_tick, self.end_tick

    @property
    def seconds_interval(self) -> Tuple[float, Optional[float]]:
        return self.start_seconds, self.end_seconds

    @property
    def duration_ticks(self) -> Optional[int]:
        if self.end_tick is None:
            return None
        return self.end_tick - self.start_tick

    @property
    def seconds_per_tick(self) -> float:
        return self.tempo * 1e-6 / self._ticks_per_beat

    def as_dict(self) -> Dict[str, Any]:
        return {
            'start_tick': self.start_tick,
            'end_tick': self.end_tick,
            'tempos': self.tempos,
            'tempo': self.tempo,
            'tracks': self.tracks,
            'changes': self.changes,
            'start_seconds': self.start_seconds,
            'end_seconds': self.end_seconds,
            'is_ambiguous': self.is_ambiguous,
        }

    def __iter__(self) -> Iterator[Any]:
        return iter((self.start_tick, self.end_tick, self.tempo))

    def __len__(self) -> int:
        return 3

    def __getitem__(self, index: int) -> Any:
        return (self.start_tick, self.end_tick, self.tempo)[index]

    def __post_init__(self):
        if self.start_tick < 0:
            raise ValueError('tempo segment tick must be non-negative')
        if not self.tempos:
            raise ValueError('tempo segment must have at least one tempo')
        if self.tempo not in self.tempos:
            raise ValueError('segment tempo must be one of its candidates')


@dataclass(frozen=True)
class TempoConflict(TempoSegment):
    """An ambiguous tempo segment with conflicting declarations.

    The earliest conflict is the point after which absolute times are no
    longer uniquely determined.  Ordering the object yields the ambiguous
    tick interval, so ``start, end = timeline.first_uncertainty`` works.
    """

    def as_tuple(self) -> Tuple[int, Optional[int]]:
        return self.start_tick, self.end_tick

    def __iter__(self) -> Iterator[Optional[int]]:
        return iter(self.as_tuple())

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> Optional[int]:
        return self.as_tuple()[index]

    def __eq__(self, other) -> bool:
        if isinstance(other, (tuple, list)):
            return self.as_tuple() == tuple(other)
        return super().__eq__(other)

    __hash__ = TempoSegment.__hash__


@dataclass(frozen=True)
class TimelineEvent:
    """A message with its position in a timeline.

    ``tick`` and ``seconds`` are absolute values from the start of the time
    base.  ``delta_tick`` and ``delta_seconds`` are relative to the previous
    message *in the same track*.  ``message`` is the original message; its
    ``time`` attribute still holds the original tick delta.
    """

    track: int
    index: int
    tick: int
    seconds: float
    delta_tick: int
    delta_seconds: float
    message: Any

    @property
    def type(self) -> str:
        return self.message.type

    @property
    def is_meta(self) -> bool:
        return self.message.is_meta

    @property
    def absolute_tick(self) -> int:
        return self.tick

    @property
    def absolute_seconds(self) -> float:
        return self.seconds

    def as_dict(self) -> Dict[str, Any]:
        """Return a flat event record with message fields included."""
        row = {
            'track': self.track,
            'index': self.index,
            'tick': self.tick,
            'seconds': self.seconds,
            'delta_tick': self.delta_tick,
            'delta_seconds': self.delta_seconds,
            'type': self.type,
            'message': self.message,
        }
        for key, value in self.message.dict().items():
            if key not in row:
                row[key] = value
        return row

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


def _tempo_changes_from_tracks(
        tracks: Sequence[Any],
        track: Optional[int] = None) -> List[TempoChange]:
    if track is None:
        track_indices = range(len(tracks))
    else:
        if not isinstance(track, Integral):
            raise TypeError('track must be an integer or None')
        if track < 0 or track >= len(tracks):
            raise IndexError('track index out of range')
        track_indices = [int(track)]

    changes = []
    for track_index in track_indices:
        tick = 0
        for index, message in enumerate(tracks[track_index]):
            tick += message.time
            if not isinstance(tick, Real):
                raise ValueError('message time must be a number')
            if message.type == 'set_tempo':
                changes.append(TempoChange(
                    tick=int(tick) if isinstance(tick, Integral) else tick,
                    tempo=int(message.tempo),
                    track=int(track_index),
                    index=index,
                ))
    return changes


def _looks_like_track(value: Any) -> bool:
    if isinstance(value, (str, bytes, TempoChange)):
        return False
    if hasattr(value, 'tick'):
        return False
    if not isinstance(value, list):
        return False
    return all(hasattr(message, 'time') and hasattr(message, 'type')
               for message in value)


def _normalize_changes(changes: Any) -> Tuple[TempoChange, ...]:
    if changes is None:
        return ()
    if isinstance(changes, dict):
        items = list(changes.items())
    else:
        items = list(changes)

    result = []
    for item in items:
        if isinstance(item, TempoChange):
            result.append(item)
        elif hasattr(item, 'tick') and hasattr(item, 'tempo'):
            result.append(TempoChange(
                tick=item.tick,
                tempo=item.tempo,
                track=getattr(item, 'track', 0),
                index=getattr(item, 'index', 0),
            ))
        elif isinstance(item, (tuple, list)) and len(item) in (2, 3, 4):
            if len(item) == 2:
                tick, tempo = item
                track = index = 0
            elif len(item) == 3:
                tick, tempo, track = item
                index = 0
            else:
                tick, tempo, track, index = item
            result.append(TempoChange(
                tick=tick, tempo=tempo, track=track, index=index))
        else:
            raise TypeError('unsupported tempo change {!r}'.format(item))
    return tuple(result)


class TempoMap:
    """Conversion between absolute ticks and seconds for one time base.

    The map starts with the default tempo of 500000 microseconds per quarter
    note, then applies every ``set_tempo`` change in tick order.  If a map is
    built for a synchronous file, ``tracks`` should contain every track.
    """

    def __init__(self,
                 changes: Optional[Iterable[Any]] = None,
                 ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                 initial_tempo: int = DEFAULT_TEMPO,
                 *,
                 tempos: Optional[Iterable[Any]] = None,
                 tempo_changes: Optional[Iterable[Any]] = None,
                 track: Optional[int] = None):
        if changes is None:
            changes = tempos if tempos is not None else tempo_changes

        if changes is not None and hasattr(changes, 'tracks') \
                and hasattr(changes, 'ticks_per_beat'):
            midi_file = changes
            if getattr(midi_file, 'type', 1) == 2 and track is None:
                raise ValueError(
                    'track must be specified for a type 2 (asynchronous) file')
            changes = _tempo_changes_from_tracks(midi_file.tracks, track)
            ticks_per_beat = midi_file.ticks_per_beat
        elif changes is not None and isinstance(changes, (list, tuple)) \
                and changes and hasattr(changes[0], 'time'):
            # A single sequence of delta-timed messages.
            changes = _tempo_changes_from_tracks([changes], track=0)
        elif changes is not None and isinstance(changes, (list, tuple)) \
                and changes and all(_looks_like_track(track)
                                    for track in changes):
            # A sequence of tracks: combine their tempo changes.
            changes = _tempo_changes_from_tracks(changes)

        if ticks_per_beat <= 0:
            raise ValueError('ticks_per_beat must be positive')
        if initial_tempo <= 0:
            raise ValueError('initial_tempo must be positive')

        changes = _normalize_changes(changes)
        changes = tuple(sorted(
            changes, key=lambda change: (change.tick, change.track,
                                         change.index)))
        for change in changes:
            if not isinstance(change.tick, Real):
                raise ValueError('tempo change tick must be a number')
            if change.tick < 0:
                raise ValueError('tempo change tick must be non-negative')
            if not isinstance(change.tempo, Integral) or change.tempo <= 0:
                raise ValueError('tempo must be a positive integer')

        self.ticks_per_beat = ticks_per_beat
        self.initial_tempo = initial_tempo
        self.changes = changes
        self._changes_by_tick = self._group_changes(changes)
        self._segments = self._build_segments()
        self._starts = tuple(segment.start_tick for segment in self._segments)
        self._seconds = tuple(segment.start_seconds
                              for segment in self._segments)
        self._min_seconds, self._max_seconds = self._build_bounds()
        self._conflicts = tuple(
            self._make_conflict(segment) for segment in self._segments
            if segment.is_ambiguous)

    @classmethod
    def from_tracks(cls, tracks: Sequence[Any],
                    ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                    track: Optional[int] = None,
                    initial_tempo: int = DEFAULT_TEMPO) -> 'TempoMap':
        """Build a map from all tracks, or from one track if given."""
        changes = _tempo_changes_from_tracks(tracks, track=track)
        return cls(changes, ticks_per_beat, initial_tempo)

    @classmethod
    def from_messages(cls, messages: Sequence[Any],
                      ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                      track: int = 0,
                      initial_tempo: int = DEFAULT_TEMPO) -> 'TempoMap':
        """Build a map from one sequence of delta-timed messages."""
        changes = _tempo_changes_from_tracks([messages], track=0)
        return cls(changes, ticks_per_beat, initial_tempo)

    @classmethod
    def from_dict(cls, tempos: Dict[int, int],
                  ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                  initial_tempo: int = DEFAULT_TEMPO) -> 'TempoMap':
        """Build a map from a ``{tick: tempo}`` mapping."""
        return cls(tempos, ticks_per_beat, initial_tempo)

    @classmethod
    def from_file(cls, midi_file: Any, track: Optional[int] = None,
                  initial_tempo: int = DEFAULT_TEMPO) -> 'TempoMap':
        """Build a map from a :class:`mido.MidiFile`.

        Pass ``track`` for an asynchronous file; a shared map is meaningless
        when the tracks are independent.
        """
        if getattr(midi_file, 'type', 1) == 2 and track is None:
            raise ValueError(
                'track must be specified for a type 2 (asynchronous) file')
        return cls.from_tracks(
            midi_file.tracks, ticks_per_beat=midi_file.ticks_per_beat,
            track=track, initial_tempo=initial_tempo)

    @staticmethod
    def _group_changes(
            changes: Tuple[TempoChange, ...]
            ) -> Dict[Any, List[TempoChange]]:
        groups = {}
        for change in changes:
            groups.setdefault(change.tick, []).append(change)
        return groups

    def _build_segments(self) -> Tuple[TempoSegment, ...]:
        boundaries = [0]
        boundaries.extend(tick for tick in sorted(self._changes_by_tick)
                          if tick != 0)

        segments = []
        seconds = 0.0
        for i, start_tick in enumerate(boundaries):
            end_tick = boundaries[i + 1] if i + 1 < len(boundaries) else None
            group = self._changes_by_tick.get(start_tick)

            if group:
                group = tuple(sorted(
                    group, key=lambda change: (change.track, change.index)))
                tempos = tuple(sorted({change.tempo for change in group}))
                tracks = tuple(sorted({change.track for change in group}))
                changes = group
                tempo = group[-1].tempo
            else:
                tempos = (self.initial_tempo,)
                tracks = ()
                changes = ()
                tempo = self.initial_tempo

            if end_tick is None:
                end_seconds = None
            else:
                end_seconds = seconds + (
                    (end_tick - start_tick) * tempo * 1e-6
                    / self.ticks_per_beat)

            segment = TempoSegment(
                start_tick=start_tick,
                end_tick=end_tick,
                tempos=tempos,
                tempo=tempo,
                tracks=tracks,
                changes=changes,
                start_seconds=seconds,
                end_seconds=end_seconds,
            )
            object.__setattr__(segment, '_ticks_per_beat', self.ticks_per_beat)
            segments.append(segment)
            seconds = end_seconds

        return tuple(segments)

    def _build_bounds(self) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
        min_seconds = []
        max_seconds = []
        min_now = 0.0
        max_now = 0.0

        for segment in self._segments:
            min_seconds.append(min_now)
            max_seconds.append(max_now)

            if segment.end_tick is None:
                break

            duration_ticks = segment.end_tick - segment.start_tick
            min_now += (duration_ticks * min(segment.tempos) * 1e-6
                        / self.ticks_per_beat)
            max_now += (duration_ticks * max(segment.tempos) * 1e-6
                        / self.ticks_per_beat)

        return tuple(min_seconds), tuple(max_seconds)

    def _make_conflict(self, segment: TempoSegment) -> TempoConflict:
        conflict = TempoConflict(
            start_tick=segment.start_tick,
            end_tick=segment.end_tick,
            tempos=segment.tempos,
            tempo=segment.tempo,
            tracks=segment.tracks,
            changes=segment.changes,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
        )
        object.__setattr__(conflict, '_ticks_per_beat', self.ticks_per_beat)
        return conflict

    @property
    def segments(self) -> Tuple[TempoSegment, ...]:
        return self._segments

    @property
    def tempo_changes(self) -> Tuple[TempoChange, ...]:
        return self.changes

    @property
    def conflicts(self) -> Tuple[TempoConflict, ...]:
        return self._conflicts

    @property
    def first_conflict(self) -> Optional[TempoConflict]:
        return self._conflicts[0] if self._conflicts else None

    @property
    def first_uncertainty(self) -> Optional[TempoConflict]:
        """The earliest segment whose tempo is not unambiguous."""
        return self.first_conflict

    @property
    def has_conflicts(self) -> bool:
        return bool(self._conflicts)

    @property
    def uncertain_from_tick(self) -> Optional[int]:
        """First tick at which absolute times are no longer unique."""
        conflict = self.first_conflict
        return conflict.start_tick if conflict else None

    @property
    def uncertain_from_seconds(self) -> Optional[float]:
        """First second at which absolute times are no longer unique."""
        conflict = self.first_conflict
        return conflict.start_seconds if conflict else None

    @property
    def uncertain_intervals(self) -> Tuple[TempoConflict, ...]:
        """All ambiguous segments."""
        return self._conflicts

    @property
    def uncertain_interval(self) -> Optional[Tuple[int, Optional[int]]]:
        """Earliest affected interval as ``(start_tick, None)``.

        A conflict's accumulated offset affects every later event, so the
        affected interval is open-ended; the ambiguous declarations
        themselves are described by :attr:`first_uncertainty_interval`.
        """
        start = self.uncertain_from_tick
        return None if start is None else (start, None)

    @property
    def first_uncertainty_interval(self) -> Optional[Tuple[int, Optional[int]]]:
        """The first ambiguous tick interval as ``(start, end)``."""
        conflict = self.first_conflict
        return None if conflict is None else conflict.as_tuple()

    def segment_at_tick(self, tick: int) -> TempoSegment:
        if tick < 0:
            raise ValueError('tick must be non-negative')
        index = bisect_right(self._starts, tick) - 1
        return self._segments[index]

    def tempo_at_tick(self, tick: int) -> int:
        return self.segment_at_tick(tick).tempo

    def tempo_candidates_at_tick(self, tick: int) -> Tuple[int, ...]:
        return self.segment_at_tick(tick).tempos

    def seconds_at_tick(self, tick: int, strict: bool = False) -> float:
        """Return the absolute time of ``tick`` in seconds.

        With ``strict=True``, a tick after a conflict raises
        :class:`TempoConflictError` instead of selecting a tempo.
        """
        if tick < 0:
            raise ValueError('tick must be non-negative')
        if strict and self.is_uncertain(tick):
            raise TempoConflictError(
                'tick {} is affected by a tempo conflict at tick {}'.format(
                    tick, self.uncertain_from_tick))

        segment = self.segment_at_tick(tick)
        return segment.start_seconds + (
            (tick - segment.start_tick) * segment.tempo * 1e-6
            / self.ticks_per_beat)

    def seconds_at(self, tick: int, strict: bool = False) -> float:
        return self.seconds_at_tick(tick, strict=strict)

    def seconds(self, tick: int, strict: bool = False) -> float:
        return self.seconds_at_tick(tick, strict=strict)

    def tick(self, seconds: float, strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, strict=strict)

    def tick_to_seconds(self, tick: int, strict: bool = False) -> float:
        return self.seconds_at_tick(tick, strict=strict)

    def seconds_to_tick(self, seconds: float, strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, strict=strict)

    def tick2second(self, tick: int, strict: bool = False) -> float:
        return self.seconds_at_tick(tick, strict=strict)

    def seconds_at_tick_bounds(self, tick: int) -> Tuple[float, float]:
        """Return the minimum and maximum possible time at ``tick``.

        The values differ only for ticks after a conflicting tempo
        declaration; before the first conflict they are equal.
        """
        if tick < 0:
            raise ValueError('tick must be non-negative')

        index = bisect_right(self._starts, tick) - 1
        segment = self._segments[index]
        delta_ticks = tick - segment.start_tick
        minimum = self._min_seconds[index] + (
            delta_ticks * min(segment.tempos) * 1e-6 / self.ticks_per_beat)
        maximum = self._max_seconds[index] + (
            delta_ticks * max(segment.tempos) * 1e-6 / self.ticks_per_beat)
        return minimum, maximum

    def tick_at_seconds(self, seconds: float, strict: bool = False) -> int:
        """Return the nearest tick at absolute time ``seconds``."""
        if seconds < 0:
            raise ValueError('seconds must be non-negative')
        if strict:
            uncertain_seconds = self.uncertain_from_seconds
            if uncertain_seconds is not None and seconds > uncertain_seconds:
                raise TempoConflictError(
                    'time {}s is affected by a tempo conflict at tick {}'.format(
                        seconds, self.uncertain_from_tick))

        index = bisect_right(self._seconds, seconds) - 1
        if index < 0:
            index = 0
        segment = self._segments[index]
        tick = segment.start_tick + (
            (seconds - segment.start_seconds) * 1e6 * self.ticks_per_beat
            / segment.tempo)
        return max(0, int(round(tick)))

    def tick_at(self, seconds: float, strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, strict=strict)

    def second2tick(self, seconds: float, strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, strict=strict)

    def is_uncertain(self, tick: int) -> bool:
        """True when ``tick`` is after the first conflicting declaration.

        The conflicting tick itself is still exact: both candidates agree on
        the elapsed time up to that point.
        """
        start = self.uncertain_from_tick
        return start is not None and tick > start

    def is_ambiguous(self, tick: int) -> bool:
        return self.segment_at_tick(tick).is_ambiguous

    def conflict_at(self, tick: int) -> Optional[TempoConflict]:
        """Return the ambiguous segment containing ``tick``, if any."""
        for conflict in self._conflicts:
            if conflict.end_tick is None or tick < conflict.end_tick:
                if tick >= conflict.start_tick:
                    return conflict
        return None

    def as_table(self) -> List[Dict[str, Any]]:
        return [segment.as_dict() for segment in self._segments]

    def __iter__(self) -> Iterator[TempoSegment]:
        return iter(self._segments)

    def __len__(self) -> int:
        return len(self._segments)

    def __getitem__(self, index: int) -> TempoSegment:
        return self._segments[index]

    def __repr__(self) -> str:
        return '{}(ticks_per_beat={}, changes={}, segments={}, conflicts={})'.format(
            self.__class__.__name__,
            self.ticks_per_beat,
            len(self.changes),
            len(self._segments),
            len(self._conflicts),
        )


@dataclass(frozen=True)
class TrackTimeline:
    """The events and tempo map for one track."""

    index: int
    name: str
    tempo_map: TempoMap
    events: Tuple[TimelineEvent, ...]

    @property
    def tempo_changes(self) -> Tuple[TempoChange, ...]:
        return self.tempo_map.changes

    @property
    def conflicts(self) -> Tuple[TempoConflict, ...]:
        return self.tempo_map.conflicts

    @property
    def first_uncertainty(self) -> Optional[TempoConflict]:
        return self.tempo_map.first_uncertainty

    @property
    def has_conflicts(self) -> bool:
        return self.tempo_map.has_conflicts

    @property
    def length(self) -> float:
        """Playback time of this track in seconds."""
        return self.events[-1].seconds if self.events else 0.0

    @property
    def last_tick(self) -> int:
        return self.events[-1].tick if self.events else 0

    def seconds_at_tick(self, tick: int, strict: bool = False) -> float:
        return self.tempo_map.seconds_at_tick(tick, strict=strict)

    def seconds_at(self, tick: int, strict: bool = False) -> float:
        return self.seconds_at_tick(tick, strict=strict)

    def tick_at_seconds(self, seconds: float, strict: bool = False) -> int:
        return self.tempo_map.tick_at_seconds(seconds, strict=strict)

    def tick_at(self, seconds: float, strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, strict=strict)

    def tempo_at_tick(self, tick: int) -> int:
        return self.tempo_map.tempo_at_tick(tick)

    def tempo_at(self, tick: int) -> int:
        return self.tempo_map.tempo_at_tick(tick)

    @property
    def event_table(self) -> EventTable:
        return self.as_table()

    def as_table(self) -> EventTable:
        return EventTable(event.as_dict() for event in self.events)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'track': self.index,
            'name': self.name,
            'tempo_map': self.tempo_map,
            'events': self.as_table(),
        }

    def __iter__(self) -> Iterator[TimelineEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, index):
        return self.events[index]


class Timeline:
    """A queryable absolute-time view of a MIDI file.

    For type 0 and type 1 files there is one shared :class:`TempoMap` built
    from the ``set_tempo`` messages in all tracks.  For type 2 files each
    track has its own map and time queries must name a track.
    """

    def __init__(self, midi_file=None, *,
                 tracks: Optional[Sequence[Any]] = None,
                 midi_type: Optional[int] = None,
                 type: Optional[int] = None,
                 ticks_per_beat: Optional[int] = None,
                 initial_tempo: int = DEFAULT_TEMPO):
        if midi_file is not None:
            tracks = midi_file.tracks
            midi_type = midi_file.type
            ticks_per_beat = midi_file.ticks_per_beat
        if midi_type is None:
            midi_type = 1 if type is None else type
        if ticks_per_beat is None:
            ticks_per_beat = DEFAULT_TICKS_PER_BEAT
        if midi_type not in (0, 1, 2):
            raise ValueError(
                'invalid format {} (must be 0, 1 or 2)'.format(midi_type))

        self.type = midi_type
        self.ticks_per_beat = ticks_per_beat
        self.initial_tempo = initial_tempo
        self._tracks = tuple(tracks or ())

        if midi_type == 2:
            self._tempo_map = None
            self._tempo_maps = tuple(
                TempoMap.from_tracks(self._tracks, ticks_per_beat,
                                     track=index,
                                     initial_tempo=initial_tempo)
                for index in range(len(self._tracks)))
        else:
            self._tempo_map = TempoMap.from_tracks(
                self._tracks, ticks_per_beat, initial_tempo=initial_tempo)
            self._tempo_maps = (self._tempo_map,)

        track_timelines = []
        for index, track in enumerate(self._tracks):
            tempo_map = self._tempo_maps[index if midi_type == 2 else 0]
            track_timelines.append(TrackTimeline(
                index=index,
                name=getattr(track, 'name', ''),
                tempo_map=tempo_map,
                events=self._build_events(index, track, tempo_map),
            ))

        self.tracks = tuple(track_timelines)
        if midi_type == 2:
            self.events = tuple(
                event for track in self.tracks for event in track.events)
        else:
            self.events = tuple(sorted(
                (event for track in self.tracks for event in track.events),
                key=lambda event: (event.tick, event.track, event.index)))

    @staticmethod
    def _build_events(track_index: int, track: Sequence[Any],
                      tempo_map: TempoMap) -> Tuple[TimelineEvent, ...]:
        events = []
        tick = 0
        previous_tick = 0
        previous_seconds = 0.0

        for index, message in enumerate(track):
            tick += message.time
            if not isinstance(tick, Real):
                raise ValueError('message time must be a number in a timeline')
            event_tick = int(tick) if isinstance(tick, Integral) else tick
            seconds = tempo_map.seconds_at_tick(event_tick)
            events.append(TimelineEvent(
                track=track_index,
                index=index,
                tick=event_tick,
                seconds=seconds,
                delta_tick=event_tick - previous_tick,
                delta_seconds=seconds - previous_seconds,
                message=message,
            ))
            previous_tick = event_tick
            previous_seconds = seconds

        return tuple(events)

    @classmethod
    def from_tracks(cls, tracks: Sequence[Any],
                    midi_type: int = 1,
                    ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                    initial_tempo: int = DEFAULT_TEMPO) -> 'Timeline':
        return cls(tracks=tracks, midi_type=midi_type,
                   ticks_per_beat=ticks_per_beat, initial_tempo=initial_tempo)

    @classmethod
    def from_file(cls, midi_file: Any,
                  initial_tempo: int = DEFAULT_TEMPO) -> 'Timeline':
        return cls(midi_file, initial_tempo=initial_tempo)

    @classmethod
    def from_midi_file(cls, midi_file: Any,
                       initial_tempo: int = DEFAULT_TEMPO) -> 'Timeline':
        return cls(midi_file, initial_tempo=initial_tempo)

    @property
    def tempo_map(self) -> TempoMap:
        """The shared tempo map for a type 0 or type 1 file."""
        if self._tempo_map is None:
            raise TypeError(
                "can't use one tempo map in type 2 (asynchronous) file; "
                'use timeline.tracks[i].tempo_map')
        return self._tempo_map

    @property
    def tempo_maps(self) -> Tuple[TempoMap, ...]:
        """One map for a type 0/1 file, or one map per track for type 2."""
        return self._tempo_maps

    @property
    def segments(self) -> Tuple[TempoSegment, ...]:
        if self.type == 2:
            return tuple(segment for map_ in self._tempo_maps
                         for segment in map_.segments)
        return self.tempo_map.segments

    @property
    def tempo_changes(self) -> Tuple[TempoChange, ...]:
        if self.type == 2:
            return tuple(change for map_ in self._tempo_maps
                         for change in map_.changes)
        return self.tempo_map.changes

    @property
    def conflicts(self) -> Tuple[TempoConflict, ...]:
        if self.type == 2:
            return tuple(conflict for map_ in self._tempo_maps
                         for conflict in map_.conflicts)
        return self.tempo_map.conflicts

    @property
    def first_conflict(self) -> Optional[TempoConflict]:
        if self.type == 2:
            return self.conflicts[0] if self.conflicts else None
        return self.tempo_map.first_conflict

    @property
    def first_uncertainty(self) -> Optional[TempoConflict]:
        return self.first_conflict

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def uncertain_from_tick(self) -> Optional[int]:
        conflict = self.first_conflict
        return conflict.start_tick if conflict else None

    @property
    def first_uncertain_tick(self) -> Optional[int]:
        return self.uncertain_from_tick

    @property
    def uncertain_from_seconds(self) -> Optional[float]:
        conflict = self.first_conflict
        return conflict.start_seconds if conflict else None

    @property
    def first_uncertain_seconds(self) -> Optional[float]:
        return self.uncertain_from_seconds

    @property
    def uncertain_intervals(self) -> Tuple[TempoConflict, ...]:
        return self.conflicts

    @property
    def uncertain_segments(self) -> Tuple[TempoConflict, ...]:
        return self.conflicts

    @property
    def uncertain_interval(self) -> Optional[Tuple[int, Optional[int]]]:
        """Earliest interval after which absolute time is not unique."""
        start = self.uncertain_from_tick
        return None if start is None else (start, None)

    @property
    def first_uncertainty_interval(self) -> Optional[Tuple[int, Optional[int]]]:
        conflict = self.first_conflict
        return None if conflict is None else conflict.as_tuple()

    @property
    def length(self) -> float:
        """Whole-file playback time in seconds."""
        if self.type == 2:
            raise ValueError('impossible to compute length'
                             ' for type 2 (asynchronous) file')
        return max((event.seconds for event in self.events), default=0.0)

    @property
    def length_bounds(self) -> Tuple[float, float]:
        """Minimum and maximum whole-file playback time in seconds."""
        if self.type == 2:
            raise ValueError('impossible to compute length bounds'
                             ' for type 2 (asynchronous) file')
        if not self.events:
            return 0.0, 0.0
        return self.tempo_map.seconds_at_tick_bounds(self.events[-1].tick)

    @property
    def track_lengths(self) -> Tuple[float, ...]:
        """Per-track playback time; available for every file type."""
        return tuple(track.length for track in self.tracks)

    def track(self, index: int) -> TrackTimeline:
        if not isinstance(index, Integral):
            raise TypeError('track must be an integer')
        if index < 0 or index >= len(self.tracks):
            raise IndexError('track index out of range')
        return self.tracks[int(index)]

    def tempo_changes_for_track(self, track: int) -> Tuple[TempoChange, ...]:
        return self.track(track).tempo_changes

    def conflicts_for_track(self, track: int) -> Tuple[TempoConflict, ...]:
        return self.track(track).conflicts

    def seconds_at_tick(self, tick: int, track: Optional[int] = None,
                        strict: bool = False) -> float:
        if self.type == 2:
            return self._require_track(track).seconds_at_tick(tick, strict=strict)
        return self.tempo_map.seconds_at_tick(tick, strict=strict)

    def seconds_at(self, tick: int, track: Optional[int] = None,
                   strict: bool = False) -> float:
        return self.seconds_at_tick(tick, track=track, strict=strict)

    def seconds(self, tick: int, track: Optional[int] = None,
                strict: bool = False) -> float:
        return self.seconds_at_tick(tick, track=track, strict=strict)

    def tick(self, seconds: float, track: Optional[int] = None,
             strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, track=track, strict=strict)

    def tick_to_seconds(self, tick: int, track: Optional[int] = None,
                        strict: bool = False) -> float:
        return self.seconds_at_tick(tick, track=track, strict=strict)

    def seconds_to_tick(self, seconds: float, track: Optional[int] = None,
                        strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, track=track, strict=strict)

    def tick2second(self, tick: int, track: Optional[int] = None,
                    strict: bool = False) -> float:
        return self.seconds_at_tick(tick, track=track, strict=strict)

    def tick_at_seconds(self, seconds: float, track: Optional[int] = None,
                        strict: bool = False) -> int:
        if self.type == 2:
            return self._require_track(track).tick_at_seconds(
                seconds, strict=strict)
        return self.tempo_map.tick_at_seconds(seconds, strict=strict)

    def tick_at(self, seconds: float, track: Optional[int] = None,
                strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, track=track, strict=strict)

    def second2tick(self, seconds: float, track: Optional[int] = None,
                    strict: bool = False) -> int:
        return self.tick_at_seconds(seconds, track=track, strict=strict)

    def seconds_at_tick_bounds(self, tick: int,
                               track: Optional[int] = None) -> Tuple[float, float]:
        if self.type == 2:
            return self._require_track(track).tempo_map.seconds_at_tick_bounds(
                tick)
        return self.tempo_map.seconds_at_tick_bounds(tick)

    def tempo_at_tick(self, tick: int, track: Optional[int] = None) -> int:
        if self.type == 2:
            return self._require_track(track).tempo_at_tick(tick)
        return self.tempo_map.tempo_at_tick(tick)

    def tempo_at(self, tick: int, track: Optional[int] = None) -> int:
        return self.tempo_at_tick(tick, track=track)

    def events_at_tick(self, tick: int) -> Tuple[TimelineEvent, ...]:
        """All events at exactly ``tick``, in playback order."""
        return tuple(event for event in self.events if event.tick == tick)

    def events_between(self, start_tick: int,
                       end_tick: Optional[int] = None
                       ) -> Tuple[TimelineEvent, ...]:
        """Events with ``start_tick <= tick`` and, if given, ``tick < end``."""
        if end_tick is None:
            return tuple(event for event in self.events
                         if event.tick >= start_tick)
        return tuple(event for event in self.events
                     if start_tick <= event.tick < end_tick)

    def _require_track(self, track: Optional[int]) -> 'TrackTimeline':
        if track is None:
            raise ValueError(
                'track must be specified for a type 2 (asynchronous) file')
        return self.track(track)

    def events_for_track(self, track: int) -> Tuple[TimelineEvent, ...]:
        return self.track(track).events

    def track_events(self, track: int) -> Tuple[TimelineEvent, ...]:
        return self.track(track).events

    def timeline_for_track(self, track: int) -> 'TrackTimeline':
        return self.track(track)

    def get_timeline(self) -> 'Timeline':
        return self

    def events_by_track(self) -> Tuple[Tuple[TimelineEvent, ...], ...]:
        return tuple(track.events for track in self.tracks)

    def tempo_map_for_track(self, track: int) -> TempoMap:
        return self.track(track).tempo_map

    @property
    def event_table(self) -> EventTable:
        """Event records with absolute ticks and seconds.

        Iterating yields dictionaries in playback order.  The returned list
        is also callable, so ``timeline.event_table(track=1)`` returns the
        records for one track.
        """
        return EventTable(event.as_dict() for event in self.events)

    def as_table(self, track: Optional[int] = None) -> EventTable:
        if track is None:
            return self.event_table
        return EventTable(
            event.as_dict() for event in self.events_for_track(track))

    def merged_table(self) -> List[Dict[str, Any]]:
        """Event records in playback order with whole-file deltas."""
        rows = []
        previous_seconds = 0.0
        previous_tick = 0

        for event in self.events:
            row = event.as_dict()
            row['global_delta_tick'] = event.tick - previous_tick
            row['global_delta_seconds'] = event.seconds - previous_seconds
            previous_tick = event.tick
            previous_seconds = event.seconds
            rows.append(row)
        return rows

    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type,
            'ticks_per_beat': self.ticks_per_beat,
            'tracks': [track.to_dict() for track in self.tracks],
        }

    def __iter__(self) -> Iterator[TimelineEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, index: int) -> TrackTimeline:
        return self.track(index)

    def __repr__(self) -> str:
        return '{}(type={}, ticks_per_beat={}, tracks={}, events={})'.format(
            self.__class__.__name__,
            self.type,
            self.ticks_per_beat,
            len(self.tracks),
            len(self.events),
        )


def build_tempo_map(tracks: Sequence[Any],
                    ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                    track: Optional[int] = None,
                    initial_tempo: int = DEFAULT_TEMPO) -> TempoMap:
    """Build a tempo map from track messages.

    With ``track=None`` tempo changes from every track are combined, which
    is correct for synchronous (type 0 and type 1) files.  Pass a track
    index for an independent (type 2) sequence.
    """
    return TempoMap.from_tracks(
        tracks, ticks_per_beat=ticks_per_beat, track=track,
        initial_tempo=initial_tempo)


def build_timeline(source: Any,
                   midi_type: int = 1,
                   ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                   initial_tempo: int = DEFAULT_TEMPO) -> Timeline:
    """Build a timeline from a MIDI file or a sequence of tracks."""
    if hasattr(source, 'tracks') and hasattr(source, 'ticks_per_beat'):
        return Timeline(source, initial_tempo=initial_tempo)
    return Timeline.from_tracks(
        source, midi_type=midi_type, ticks_per_beat=ticks_per_beat,
        initial_tempo=initial_tempo)


def export_event_table(source: Any,
                       track: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return event records with absolute ticks and seconds.

    ``source`` may be a :class:`mido.MidiFile` or a :class:`Timeline`.
    """
    timeline = source if isinstance(source, Timeline) else Timeline(source)
    return timeline.as_table(track=track)
