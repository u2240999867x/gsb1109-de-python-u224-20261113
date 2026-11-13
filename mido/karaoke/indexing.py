# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Karaoke lyric indexing.

Old MIDI karaoke files keep lyric events and notes in separate
tracks. The mapping between them is not one-to-one:

- a syllable may be held across several notes (a melisma), and
- several syllables may share a single note, which makes the exact
  note-to-syllable allocation impossible to determine.

This module aligns lyric syllables with the notes of one or more
candidate vocal tracks, optionally using editor-confirmed anchor
points, and keeps every alternative melody that cannot be told apart.

Newline and paragraph markers only describe the hierarchy of the
lyrics. They split segments but never consume a note, so an
instrumental interlude stays empty instead of extending the previous
line.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..messages import Message
from ..midifiles.meta import MetaMessage
from ..midifiles.midifiles import MidiFile
from ..midifiles.tracks import MidiTrack

# Standard MIDI karaoke control characters embedded in lyric events.
_NEWLINE_MARKERS = frozenset(('\n', '\r'))


@dataclass(frozen=True)
class Note:
    """A note with its lifetime in absolute ticks."""

    note: int
    start: int
    end: int
    channel: int = 0
    velocity: int = 0


@dataclass(frozen=True)
class LyricEvent:
    """A raw lyric meta event in absolute ticks.

    ``marker`` is true for newline/paragraph markers and empty
    events, which only structure the lyrics and are never sung.
    """

    text: str
    time: int
    marker: bool = False


@dataclass(frozen=True)
class SyllableEntry:
    """One sung syllable and the notes it may be sung to."""

    text: str
    start: int
    end: int
    note_candidates: Tuple[Tuple[Note, ...], ...]

    @property
    def notes(self) -> Tuple[Note, ...]:
        """The preferred (first) note allocation for this syllable."""
        return self.note_candidates[0] if self.note_candidates else ()

    @property
    def ambiguous(self) -> bool:
        """True when more than one note allocation is possible."""
        return len(self.note_candidates) > 1


@dataclass(frozen=True)
class LyricSegment:
    """A line or paragraph of sung syllables.

    An empty segment (no entries) represents an instrumental
    interlude.
    """

    start: int
    end: int
    entries: Tuple[SyllableEntry, ...] = ()

    @property
    def text(self) -> str:
        return ''.join(entry.text for entry in self.entries)


@dataclass(frozen=True)
class LookupResult:
    """Result of a reverse lookup at a point in time."""

    time: int
    segment: LyricSegment
    entry: Optional[SyllableEntry]

    @property
    def text(self) -> str:
        return self.entry.text if self.entry is not None else ''

    @property
    def notes(self) -> Tuple[Note, ...]:
        return self.entry.notes if self.entry is not None else ()


@dataclass
class LyricIndex:
    """The aligned lyrics of a MIDI file."""

    segments: List[LyricSegment] = field(default_factory=list)
    end_of_file: int = 0

    def segment_at(self, tick: int) -> LyricSegment:
        """Return the segment active at ``tick``.

        A point inside an instrumental interlude returns an empty
        segment covering the gap; the previous sung line is never
        extended over it.
        """
        for segment in self.segments:
            if segment.start <= tick < segment.end:
                return segment

        gap_start = 0
        gap_end = self.end_of_file
        for segment in self.segments:
            if segment.end <= tick:
                gap_start = segment.end
            elif segment.start > tick:
                gap_end = segment.start
                break

        return LyricSegment(start=gap_start, end=gap_end, entries=())

    def lookup(self, tick: int) -> LookupResult:
        """Reverse lookup of the syllable sung at ``tick``.

        Interludes and regions outside any sung line resolve to an
        empty segment with no entry and no notes.
        """
        segment = self.segment_at(tick)
        entry = None
        for candidate in segment.entries:
            if candidate.start <= tick < candidate.end:
                entry = candidate
                break
        return LookupResult(time=tick, segment=segment, entry=entry)

    def lyric_at(self, tick: int) -> str:
        """Convenience wrapper returning the lyric text or ``''``."""
        return self.lookup(tick).text


def _absolute_times(messages: Sequence) -> List:
    """Return copies of the messages with absolute tick times."""
    absolute = []
    now = 0
    for message in messages:
        now += message.time
        absolute.append(message.copy(time=now))
    return absolute


def extract_notes(track: MidiTrack) -> Tuple[Note, ...]:
    """Pair note on/off events into notes with absolute tick spans."""
    active: Dict[Tuple[int, int], List[Message]] = {}
    notes: List[Note] = []

    for message in _absolute_times(track):
        if message.type not in ('note_on', 'note_off'):
            continue

        key = (message.channel, message.note)
        is_off = (message.type == 'note_off'
                  or (message.type == 'note_on' and message.velocity == 0))

        if not is_off:
            active.setdefault(key, []).append(message)
            continue

        started = active.get(key, [])
        if not started:
            continue
        on_message = started.pop(0)
        notes.append(Note(
            note=message.note,
            start=on_message.time,
            end=message.time,
            channel=message.channel,
            velocity=on_message.velocity,
        ))

    # Notes that are never switched off end where they started, which
    # keeps a dangling note from spanning later interludes.
    for pending in active.values():
        for on_message in pending:
            notes.append(Note(
                note=on_message.note,
                start=on_message.time,
                end=on_message.time,
                channel=on_message.channel,
                velocity=on_message.velocity,
            ))

    return tuple(sorted(notes, key=lambda item: (item.start, item.note)))


def extract_lyric_events(track: MidiTrack) -> Tuple[LyricEvent, ...]:
    """Return lyric meta events of a track in absolute tick order."""
    events: List[LyricEvent] = []
    for message in _absolute_times(track):
        if not isinstance(message, MetaMessage) or message.type != 'lyrics':
            continue
        text = message.text
        marker = text == '' or text in _NEWLINE_MARKERS
        events.append(LyricEvent(text=text, time=message.time, marker=marker))
    return tuple(events)


def _find_lyric_track(mid: MidiFile) -> Optional[int]:
    for index, track in enumerate(mid.tracks):
        if any(isinstance(msg, MetaMessage) and msg.type == 'lyrics'
               for msg in track):
            return index
    return None


def _split_segments(
    events: Sequence[LyricEvent],
    anchors: Dict[int, int],
) -> List[dict]:
    """Group sung events into segments at hierarchy markers.

    Markers only cut segments; they produce no syllable themselves
    and therefore never occupy an index or a note.
    """
    segments: List[dict] = []
    current = {'events': [], 'end_marker': None}
    syllable_index = 0

    for event in events:
        if event.marker:
            if current['events']:
                current['end_marker'] = event.time
                segments.append(current)
                current = {'events': [], 'end_marker': None}
            continue

        start = anchors.get(syllable_index, event.time)
        if current['events'] and start < current['events'][-1]['start']:
            raise ValueError(
                'syllable starts must be non-decreasing; '
                f'syllable {syllable_index} starts at {start} after '
                f"{current['events'][-1]['start']}"
            )
        current['events'].append({
            'text': event.text,
            'start': start,
        })
        syllable_index += 1

    if current['events']:
        segments.append(current)
    return segments


def _block_candidates(
    block: Sequence[dict],
    region_notes: Sequence[Note],
) -> List[Tuple[Tuple[Note, ...], ...]]:
    """Allocate one region of notes to a block of tied syllables.

    A block groups syllables that start on the same tick. A lone
    syllable simply receives every note of its region (a melisma).
    When several syllables share a start tick the exact division of
    the region notes cannot be determined, so every syllable keeps
    the whole region as its melody candidate instead of guessing.

    One element per syllable; each element holds every note tuple
    that the syllable may be sung to.
    """
    region = tuple(region_notes)
    if len(block) == 1:
        return [(region,)]
    return [(region,) for _ in block]


def _align_segment(
    syllables: Sequence[dict],
    notes: Sequence[Note],
    end_marker: Optional[int],
    next_start: Optional[int],
    end_of_file: int,
) -> Tuple[List[SyllableEntry], int]:
    """Align the syllables of one segment with its vocal notes."""
    segment_start = syllables[0]['start']

    # Boundaries between blocks of syllables sharing a start tick.
    block_bounds: List[int] = [0]
    for position in range(1, len(syllables)):
        if syllables[position]['start'] != syllables[position - 1]['start']:
            block_bounds.append(position)
    block_bounds.append(len(syllables))

    note_candidates: List[Tuple[Tuple[Note, ...], ...]] = [((),)] * len(
        syllables)
    for block_index in range(len(block_bounds) - 1):
        first = block_bounds[block_index]
        last = block_bounds[block_index + 1]
        block = syllables[first:last]
        region_start = block[0]['start']
        if block_index + 1 < len(block_bounds) - 1:
            region_end = syllables[last]['start']
        elif next_start is not None:
            region_end = next_start
        else:
            region_end = end_of_file

        region_notes = [
            note for note in notes
            if region_start <= note.start < region_end
        ]
        if block_index == 0:
            # Notes still sounding from an interlude attach to the
            # first sung block instead of being dropped.
            region_notes = [
                note for note in notes
                if note.start < region_start and note.end > region_start
            ] + region_notes

        candidates = _block_candidates(block, region_notes)
        for offset, candidate in enumerate(candidates):
            note_candidates[first + offset] = candidate

    last_start = syllables[-1]['start']
    hard_end = next_start if next_start is not None else end_of_file
    note_end = max(
        (note.end for note in notes
         if segment_start <= note.start < hard_end),
        default=last_start,
    )
    # A hierarchy marker hard-ends the line: the region between the
    # marker and the next sung syllable stays an empty interlude
    # instead of stretching the previous line. A final segment
    # without a marker runs to its last note.
    if end_marker is not None:
        segment_end = max(end_marker, last_start)
    else:
        segment_end = max(note_end, last_start)
    if next_start is not None:
        segment_end = min(segment_end, next_start)
    segment_end = max(segment_end, last_start)

    entries = []
    for position, item in enumerate(syllables):
        block_index = next(
            index for index in range(len(block_bounds) - 1)
            if block_bounds[index] <= position < block_bounds[index + 1]
        )
        if block_index + 1 < len(block_bounds) - 1:
            end = syllables[block_bounds[block_index + 1]]['start']
        else:
            end = segment_end
        entries.append(SyllableEntry(
            text=item['text'],
            start=item['start'],
            end=max(end, item['start']),
            note_candidates=note_candidates[position],
        ))

    return entries, segment_end


def build_lyric_index(
    mid: MidiFile,
    vocal_tracks: Optional[Sequence[int]] = None,
    lyric_track: Optional[int] = None,
    anchors: Optional[Sequence[Tuple[int, int]]] = None,
) -> LyricIndex:
    """Build a lyric index from a MIDI file.

    ``vocal_tracks`` are the indices of tracks that may contain the
    vocal melody. When more than one is given each one becomes a
    melody candidate because the real vocal track cannot be told
    apart automatically.

    ``lyric_track`` is the track holding lyric meta events. It
    defaults to the first track that contains any.

    ``anchors`` is a sequence of ``(syllable_index, tick)`` pairs
    confirmed by an editor; the index counts sung syllables only and
    overrides the raw lyric event position.
    """
    if lyric_track is None:
        lyric_track = _find_lyric_track(mid)
    if lyric_track is None:
        raise ValueError('no track containing lyric events was found')

    if vocal_tracks is None:
        vocal_tracks = [
            index for index, track in enumerate(mid.tracks)
            if index != lyric_track
            and any(isinstance(msg, Message) and msg.type == 'note_on'
                    for msg in track)
        ]
    if not vocal_tracks:
        raise ValueError('no candidate vocal tracks were given')

    anchor_map = dict(anchors or ())
    events = extract_lyric_events(mid.tracks[lyric_track])
    raw_segments = _split_segments(events, anchor_map)

    note_streams = [
        extract_notes(mid.tracks[index]) for index in vocal_tracks
    ]

    end_of_file = 0
    for track in mid.tracks:
        now = 0
        for message in track:
            now += message.time
        end_of_file = max(end_of_file, now)

    segments: List[LyricSegment] = []
    for seg_index, raw in enumerate(raw_segments):
        syllables = raw['events']
        next_start = (raw_segments[seg_index + 1]['events'][0]['start']
                      if seg_index + 1 < len(raw_segments) else None)

        stream_entries: List[List[SyllableEntry]] = []
        segment_ends: List[int] = []
        for notes in note_streams:
            entries, segment_end = _align_segment(
                syllables, notes, raw['end_marker'], next_start,
                end_of_file,
            )
            stream_entries.append(entries)
            segment_ends.append(segment_end)

        segment_end = max(segment_ends)
        merged = _merge_stream_candidates(stream_entries)
        segments.append(LyricSegment(
            start=syllables[0]['start'],
            end=segment_end,
            entries=tuple(merged),
        ))

    return LyricIndex(segments=segments, end_of_file=end_of_file)


def _merge_stream_candidates(
    streams: Sequence[Sequence[SyllableEntry]],
) -> List[SyllableEntry]:
    """Merge note candidates coming from different vocal tracks."""
    if not streams:
        return []

    merged = []
    for position in range(len(streams[0])):
        options: List[Tuple[Note, ...]] = []
        seen = set()
        for stream in streams:
            for candidate in stream[position].note_candidates:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    options.append(candidate)
        if not options:
            options = [()]
        reference = streams[0][position]
        merged.append(SyllableEntry(
            text=reference.text,
            start=reference.start,
            end=reference.end,
            note_candidates=tuple(options),
        ))
    return merged
