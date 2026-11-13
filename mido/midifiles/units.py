# SPDX-FileCopyrightText: 2017 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

def tick2second(tick, ticks_per_beat, tempo):
    """Convert absolute time in ticks to seconds.

    Returns absolute time in seconds for a chosen MIDI file time resolution
    (ticks/pulses per quarter note, also called PPQN) and tempo (microseconds
    per quarter note).
    """
    scale = tempo * 1e-6 / ticks_per_beat
    return tick * scale


def second2tick(second, ticks_per_beat, tempo):
    """Convert absolute time in seconds to ticks.

    Returns absolute time in ticks for a chosen MIDI file time resolution
    (ticks/pulses per quarter note, also called PPQN) and tempo (microseconds
    per quarter note). Normal rounding applies.
    """
    scale = tempo * 1e-6 / ticks_per_beat
    return int(round(second / scale))


def bpm2tempo(bpm, time_signature=(4, 4)):
    """Convert BPM (beats per minute) to MIDI file tempo (microseconds per
    quarter note).

    Depending on the chosen time signature a bar contains a different number of
    beats. These beats are multiples/fractions of a quarter note, thus the
    returned BPM depend on the time signature. Normal rounding applies.
    """
    return int(round(60 * 1e6 / bpm * time_signature[1] / 4.))


def tempo2bpm(tempo, time_signature=(4, 4)):
    """Convert MIDI file tempo (microseconds per quarter note) to BPM (beats
    per minute).

    Depending on the chosen time signature a bar contains a different number of
    beats. The beats are multiples/fractions of a quarter note, thus the
    returned tempo depends on the time signature denominator.
    """
    return 60 * 1e6 / tempo * time_signature[1] / 4.


# SMPTE time code belongs with the other time conversions.  Importing
# them here keeps ``mido.midifiles.units`` the one place to look for
# time unit conversions while the implementation lives in
# ``mido.midifiles.timecode``.
from .timecode import (  # noqa: E402,F401
    DroppedFrameError,
    InvalidTimecodeError,
    Reel,
    ReelTable,
    Timecode,
    TimecodeError,
    TimecodeGapError,
    TimecodeTable,
    format_timecode,
    frame_rate_fraction,
    frames_per_day,
    frames_to_smpte_offset,
    frames_to_timecode,
    is_drop_frame_rate,
    midnight_wrap,
    nominal_frame_rate,
    parse_timecode,
    seconds_to_timecode,
    smpte_offset_to_frames,
    timecode_to_frames,
    timecode_to_seconds,
)
