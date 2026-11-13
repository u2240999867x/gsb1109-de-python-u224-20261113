# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Estimate how many synthesizer voices (parts) a MIDI file needs.

The estimator never expands a whole score into a flat list of notes.
Tracks are merged lazily, one simultaneous group of events at a time,
and only the notes that are currently sounding or held by the sustain
pedal are kept in memory.  This makes the memory usage proportional to
the number of simultaneously active notes instead of the total number
of notes in the file.

Voice allocation is device specific and depends on the timbre
(program).  The caller supplies the number of voices one sounding note
occupies for each program through ``voices_per_program``; programs that
are not listed fall back to ``default_voices``.

MIDI events that land on the same tick have no guaranteed ordering
across tracks.  Two reachable bounds are therefore reported:

* ``peak_lower`` is reachable when every release that may happen before
  a simultaneous attack does happen first.
* ``peak_upper`` is reachable when every attack happens before any
  simultaneous release.

Events written in the same track do have an explicit order, so a
note-off followed by a note-on in one track never takes an extra voice,
even in the upper bound.
"""

from dataclasses import dataclass, field
from heapq import heappop, heappush
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .meta import MetaMessage

#: MIDI channel messages (everything below 0xF0).
CHANNEL_MESSAGE_TYPES = frozenset((
    'note_off',
    'note_on',
    'polytouch',
    'control_change',
    'program_change',
    'aftertouch',
    'pitchwheel',
))

#: Hold pedal (damper pedal).
SUSTAIN_CONTROL = 64

#: All Sound Off (CC 120): immediately silences everything on the channel.
ALL_SOUND_OFF = 120

#: Reset All Controllers (CC 121): releases the hold pedal and held notes.
RESET_ALL_CONTROLLERS = 121

#: All Notes Off (CC 123): releases every sounding and held note.
ALL_NOTES_OFF = 123

# Priority of an event inside one simultaneous group.  Lower numbers are
# processed first.  The two tables below describe the two reachable
# orderings when several tracks put events on the same tick.
#
# The optimistic ordering frees voices as early as possible (minimum
# peak), the pessimistic ordering allocates voices before any release
# (maximum peak).  Events inside one track always keep their written
# order, which the per-track chain scheduling in _simulate() enforces.
_PRIORITY_OFF_FIRST = {
    'all_off': 0,
    'pedal_up': 1,
    'note_off': 2,
    'other': 3,
    'pedal_down': 4,
    'program_change': 5,
    'note_on': 6,
}

_PRIORITY_ON_FIRST = {
    'note_on': 0,
    'program_change': 1,
    'pedal_down': 2,
    'other': 3,
    'note_off': 4,
    'pedal_up': 5,
    'all_off': 6,
}


@dataclass(frozen=True)
class VoicePeak:
    """A tick at which a peak voice count is reached.

    ``lower`` and ``upper`` are the reachable voice counts at this tick
    given the unspecified ordering of simultaneous events from
    different tracks.
    """

    tick: int
    lower: int
    upper: int
    kind: str
    tracks: Tuple[int, ...]
    channels: Tuple[int, ...]


@dataclass(frozen=True)
class VoiceEstimate:
    """Result of a streaming voice count estimation.

    ``peaks`` holds the first tick at which each strict maximum is
    reached for both bounds, together with the tracks and channels
    involved.  Only strict increases are recorded, so the number of
    stored positions is bounded by the number of simultaneous notes
    rather than the total number of events in the file.
    """

    peak_lower: int
    peak_upper: int
    peaks: Tuple[VoicePeak, ...]

    @property
    def exact(self) -> bool:
        """True when every simultaneous group has a determined order."""
        return self.peak_lower == self.peak_upper

    @property
    def peak_tick(self) -> Optional[int]:
        """First tick where the upper peak is reached, if any."""
        for peak in self.peaks:
            if peak.upper == self.peak_upper:
                return peak.tick
        return None


@dataclass
class _ChannelState:
    program: int = 0
    sustain: bool = False
    # Note number -> number of voices occupied by that note.  Sounding
    # notes have received a note_on, held notes are kept alive only by
    # the hold pedal.  Both occupy voices on the device.
    sounding: Dict[int, int] = field(default_factory=dict)
    held: Dict[int, int] = field(default_factory=dict)


class _VoiceSimulator:
    """One deterministic interpretation of ambiguous event orderings."""

    def __init__(self, voices_per_program: Mapping[int, int],
                 default_voices: int, priority: Mapping[str, int]):
        self._costs = dict(voices_per_program)
        self._default_voices = default_voices
        self._priority = priority
        self._channels: Dict[int, _ChannelState] = {}
        self.total = 0

    def _state(self, channel: int) -> _ChannelState:
        state = self._channels.get(channel)
        if state is None:
            state = _ChannelState()
            self._channels[channel] = state
        return state

    def _cost(self, state: _ChannelState) -> int:
        return self._costs.get(state.program, self._default_voices)

    def _channel_voices(self, state: _ChannelState) -> int:
        occupied = sum(state.sounding.values()) + sum(state.held.values())
        return occupied * self._cost(state)

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        if velocity == 0:
            # A note_on with velocity 0 is equivalent to a note_off.
            self.note_off(channel, note)
            return

        state = self._state(channel)
        self.total -= self._channel_voices(state)
        if state.held.get(note, 0) > 0:
            # Re-triggering a note kept alive by the pedal reuses that
            # voice instead of piling up a second one.
            state.held[note] -= 1
            if state.held[note] == 0:
                del state.held[note]
        state.sounding[note] = state.sounding.get(note, 0) + 1
        self.total += self._channel_voices(state)

    def note_off(self, channel: int, note: int) -> None:
        state = self._state(channel)
        if state.sounding.get(note, 0) == 0:
            # Stale note_off without a matching note_on: nothing to free.
            return
        self.total -= self._channel_voices(state)
        state.sounding[note] -= 1
        if state.sounding[note] == 0:
            del state.sounding[note]
        if state.sustain:
            # The pedal is down, so the voice keeps ringing.
            state.held[note] = state.held.get(note, 0) + 1
        self.total += self._channel_voices(state)

    def control_change(self, channel: int, control: int, value: int) -> None:
        if control == SUSTAIN_CONTROL:
            state = self._state(channel)
            pedal_down = value >= 64
            if pedal_down == state.sustain:
                return
            self.total -= self._channel_voices(state)
            state.sustain = pedal_down
            if not pedal_down:
                # Releasing the pedal silences every held note.
                state.held.clear()
            self.total += self._channel_voices(state)
        elif control in (ALL_SOUND_OFF, ALL_NOTES_OFF):
            state = self._state(channel)
            if state.sounding or state.held:
                self.total -= self._channel_voices(state)
                state.sounding.clear()
                state.held.clear()
                self.total += self._channel_voices(state)
        elif control == RESET_ALL_CONTROLLERS:
            state = self._state(channel)
            if state.sustain or state.held:
                self.total -= self._channel_voices(state)
                state.sustain = False
                state.held.clear()
                self.total += self._channel_voices(state)

    def program_change(self, channel: int, program: int) -> None:
        state = self._state(channel)
        if state.program == program:
            return
        self.total -= self._channel_voices(state)
        state.program = program
        self.total += self._channel_voices(state)


def _event_kind(msg) -> str:
    msg_type = msg.type
    if msg_type == 'note_on':
        if msg.velocity == 0:
            return 'note_off'
        return 'note_on'
    if msg_type == 'note_off':
        return 'note_off'
    if msg_type == 'control_change':
        control = msg.control
        if control == SUSTAIN_CONTROL:
            return 'pedal_down' if msg.value >= 64 else 'pedal_up'
        if control in (ALL_SOUND_OFF, ALL_NOTES_OFF):
            return 'all_off'
        if control == RESET_ALL_CONTROLLERS:
            return 'pedal_up'
        return 'other'
    if msg_type == 'program_change':
        return 'program_change'
    return 'other'


def _is_channel_message(msg) -> bool:
    return (not isinstance(msg, MetaMessage)
            and msg.type in CHANNEL_MESSAGE_TYPES
            and not msg.is_realtime)


def _simulate(simulator: _VoiceSimulator,
              group: Sequence[Tuple[int, object]]) -> int:
    """Feed one simultaneous group to one simulator.

    Events are split into per-track chains so the written order inside
    a track is preserved.  At every step the event with the smallest
    priority value that is next in its own track is processed.

    Returns the highest voice count reached while processing the
    group, since a transient peak can sit between two events that land
    on the same tick.
    """
    chains: Dict[int, List[object]] = {}
    for track_index, msg in group:
        if not _is_channel_message(msg):
            continue
        chains.setdefault(track_index, []).append(msg)

    pointers = {track_index: 0 for track_index in chains}
    remaining = sum(len(chain) for chain in chains.values())
    group_peak = simulator.total

    while remaining:
        candidates = []
        for track_index, chain in chains.items():
            index = pointers[track_index]
            if index < len(chain):
                msg = chain[index]
                candidates.append(
                    (simulator._priority[_event_kind(msg)], track_index, msg)
                )
        _, track_index, msg = min(candidates, key=lambda item: item[:2])
        pointers[track_index] += 1
        remaining -= 1

        kind = _event_kind(msg)
        if kind == 'note_on':
            simulator.note_on(msg.channel, msg.note, msg.velocity)
        elif kind == 'note_off':
            simulator.note_off(msg.channel, msg.note)
        elif msg.type == 'control_change':
            simulator.control_change(msg.channel, msg.control, msg.value)
        elif kind == 'program_change':
            simulator.program_change(msg.channel, msg.program)

        if simulator.total > group_peak:
            group_peak = simulator.total

    return group_peak


def _iter_merged(tracks: Sequence[Sequence[object]]):
    """Yield (tick, [(track_index, message), ...]) groups lazily.

    Only one message per track is buffered at a time, so the full score
    is never materialized in memory.
    """
    heap: List[Tuple[int, int, int]] = []
    for track_index, track in enumerate(tracks):
        if track:
            heappush(heap, (track[0].time, track_index, 0))

    while heap:
        tick = heap[0][0]
        group: List[Tuple[int, object]] = []
        while heap and heap[0][0] == tick:
            _, track_index, msg_index = heappop(heap)
            group.append((track_index, tracks[track_index][msg_index]))
            msg_index += 1
            track = tracks[track_index]
            if msg_index < len(track):
                heappush(
                    heap,
                    (tick + track[msg_index].time, track_index, msg_index),
                )
        yield tick, group


def _validate_costs(voices_per_program: Mapping[int, int],
                    default_voices: int) -> None:
    if not isinstance(default_voices, int) or default_voices < 1:
        raise ValueError('default_voices must be a positive integer')
    for program, voices in voices_per_program.items():
        if not isinstance(program, int) or not 0 <= program <= 127:
            raise ValueError(
                'program numbers must be integers in the range 0..127')
        if not isinstance(voices, int) or voices < 1:
            raise ValueError(
                'voice counts must be positive integers; '
                'program {} has {}'.format(program, voices))


def estimate_voices(mid,
                    voices_per_program: Mapping[int, int],
                    default_voices: int = 1) -> VoiceEstimate:
    """Estimate the peak number of voices needed to play ``mid``.

    ``mid`` is a :class:`~mido.midifiles.midifiles.MidiFile`.
    ``voices_per_program`` maps a MIDI program number to the number of
    voices occupied by one sounding note in that timbre; programs
    missing from the mapping use ``default_voices``.

    The file is streamed track by track, so notes that have already
    ended do not stay in memory.  Channels, the hold pedal and
    all-notes-off / all-sound-off events are taken into account.

    Returns a :class:`VoiceEstimate` with reachable lower and upper
    peaks and the ticks where they occur.  Type 2 (asynchronous) files
    raise :class:`TypeError` because their tracks share no time line.
    """
    if mid.type == 2:
        raise TypeError(
            "can't estimate voices for a type 2 (asynchronous) file")

    voices_per_program = dict(voices_per_program)
    _validate_costs(voices_per_program, default_voices)

    optimistic = _VoiceSimulator(
        voices_per_program, default_voices, _PRIORITY_OFF_FIRST)
    pessimistic = _VoiceSimulator(
        voices_per_program, default_voices, _PRIORITY_ON_FIRST)

    peak_lower = 0
    peak_upper = 0
    lower_peaks: List[VoicePeak] = []
    upper_peaks: List[VoicePeak] = []

    for tick, group in _iter_merged(mid.tracks):
        lower = _simulate(optimistic, group)
        upper = _simulate(pessimistic, group)

        if lower < peak_lower and upper < peak_upper:
            continue

        if lower == 0 and upper == 0:
            continue

        channel_messages = [
            msg for _, msg in group if _is_channel_message(msg)
        ]
        if not channel_messages:
            continue

        peak = VoicePeak(
            tick=tick,
            lower=lower,
            upper=upper,
            kind='+'.join(sorted({_event_kind(msg)
                                  for msg in channel_messages})),
            tracks=tuple(sorted({track_index
                                 for track_index, msg in group
                                 if _is_channel_message(msg)})),
            channels=tuple(sorted({msg.channel
                                   for msg in channel_messages})),
        )

        # Only the first tick that reaches a strict new maximum is
        # remembered, so the stored positions can never grow with the
        # total number of events: they are bounded by the number of
        # distinct peak values, which cannot exceed the polyphony that
        # is already held in memory.
        if lower > peak_lower:
            peak_lower = lower
            lower_peaks = [peak]

        if upper > peak_upper:
            peak_upper = upper
            upper_peaks = [peak]

    seen = set()
    peaks: List[VoicePeak] = []
    for peak in lower_peaks + upper_peaks:
        if peak.tick not in seen:
            seen.add(peak.tick)
            peaks.append(peak)
    peaks.sort(key=lambda peak: peak.tick)

    return VoiceEstimate(
        peak_lower=peak_lower,
        peak_upper=peak_upper,
        peaks=tuple(peaks),
    )
