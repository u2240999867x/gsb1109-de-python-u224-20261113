# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import pytest

from mido.messages import Message
from mido.midifiles.midifiles import MidiFile
from mido.midifiles.voices import (
    ALL_NOTES_OFF,
    ALL_SOUND_OFF,
    RESET_ALL_CONTROLLERS,
    SUSTAIN_CONTROL,
    estimate_voices,
)


def make_file(*tracks):
    mid = MidiFile(type=1 if len(tracks) > 1 else 0)
    for messages in tracks:
        mid.tracks.append(list(messages))
    return mid


def note_on(note, channel=0, velocity=64, time=0):
    return Message('note_on', note=note, channel=channel,
                   velocity=velocity, time=time)


def note_off(note, channel=0, time=0):
    return Message('note_off', note=note, channel=channel, time=time)


def sustain(value, channel=0, time=0):
    return Message('control_change', channel=channel,
                   control=SUSTAIN_CONTROL, value=value, time=time)


def control(control, value=0, channel=0, time=0):
    return Message('control_change', channel=channel,
                   control=control, value=value, time=time)


def program(program, channel=0, time=0):
    return Message('program_change', channel=channel,
                   program=program, time=time)


def test_empty_file_needs_no_voices():
    estimate = estimate_voices(make_file([]), {})
    assert estimate.peak_lower == 0
    assert estimate.peak_upper == 0
    assert estimate.peaks == ()
    assert estimate.peak_tick is None
    assert estimate.exact is True


def test_sequential_notes_peak_at_one():
    mid = make_file([
        note_on(60, time=0),
        note_off(60, time=10),
        note_on(62, time=0),
        note_off(62, time=10),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_lower == 1
    assert estimate.peak_upper == 1
    assert estimate.exact is True


def test_explicit_close_before_open_in_same_track_does_not_double():
    mid = make_file([
        note_on(60, time=0),
        note_off(60, time=10),
        note_on(62, time=0),
        note_off(62, time=10),
    ])
    estimate = estimate_voices(mid, {})
    # The reopen reuses the released voice instead of stacking a second.
    assert estimate.peak_lower == 1
    assert estimate.peak_upper == 1
    assert estimate.peak_tick == 0
    assert all(peak.upper <= 1 for peak in estimate.peaks)


def test_simultaneous_cross_track_close_and_open_has_bounds():
    mid = make_file(
        [note_on(60, time=0), note_off(60, time=10)],
        [note_on(62, time=10)],
    )
    estimate = estimate_voices(mid, {})
    # Off first (optimistic): the voice is reused.
    assert estimate.peak_lower == 1
    # On first (pessimistic): both overlap for one instant.
    assert estimate.peak_upper == 2
    assert estimate.exact is False
    peak = next(peak for peak in estimate.peaks if peak.tick == 10)
    assert peak.lower == 1
    assert peak.upper == 2
    assert peak.tracks == (0, 1)
    assert peak.channels == (0,)
    assert estimate.peak_tick == 10


def test_same_track_close_and_reopen_never_overlaps_even_at_upper():
    # Both events share one track, so the note_off is always processed
    # before the note_on regardless of other tracks.
    mid = make_file(
        [
            note_on(60, time=0),
            note_off(60, time=10),
            note_on(60, time=0),
            note_off(60, time=10),
        ],
        [note_on(72, time=10), note_off(72, time=10)],
    )
    estimate = estimate_voices(mid, {})
    assert estimate.peak_lower == 2
    assert estimate.peak_upper == 2



def test_two_pianos_same_pitch_same_time_count_twice():
    mid = make_file(
        [note_on(60, channel=0, time=0)],
        [note_on(60, channel=1, time=0)],
    )
    estimate = estimate_voices(mid, {})
    assert estimate.peak_lower == 2
    assert estimate.peak_upper == 2
    assert estimate.peaks[0].channels == (0, 1)


def test_same_pitch_same_channel_different_tracks_counts_twice():
    mid = make_file(
        [note_on(60, channel=0, time=0)],
        [note_on(60, channel=0, time=0)],
    )
    estimate = estimate_voices(mid, {})
    assert estimate.peak_lower == 2
    assert estimate.peak_upper == 2


def test_channels_are_independent():
    mid = make_file([
        note_on(60, channel=0, time=0),
        note_on(60, channel=1, time=0),
        note_on(60, channel=2, time=0),
        note_off(60, channel=1, time=5),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 3


def test_sustain_keeps_voice_after_note_off():
    mid = make_file([
        sustain(127, time=0),
        note_on(60, time=0),
        note_off(60, time=10),
        note_on(62, time=10),
        sustain(0, time=10),
        note_off(62, time=0),
    ])
    estimate = estimate_voices(mid, {})
    # At tick 20 the first note is held by the pedal while the second
    # one sounds, so two voices are needed.
    peak = next(peak for peak in estimate.peaks if peak.tick == 20)
    assert peak.upper == 2
    assert peak.lower == 2


def test_sustain_pedal_retrigger_reuses_held_voice():
    mid = make_file([
        sustain(127, time=0),
        note_on(60, time=0),
        note_off(60, time=10),
        note_on(60, time=10),
        note_off(60, time=10),
        sustain(0, time=10),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 1


def test_all_notes_off_releases_everything():
    mid = make_file([
        note_on(60, time=0),
        note_on(62, time=5),
        control(ALL_NOTES_OFF, time=5),
        # A fresh note after the release only needs one voice, proving
        # the earlier voices were actually freed.
        note_on(64, time=10),
        note_off(64, time=5),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 2
    assert all(peak.upper > 0 for peak in estimate.peaks)


def test_all_sound_off_releases_sustained_notes():
    mid = make_file([
        sustain(127, time=0),
        note_on(60, time=0),
        note_off(60, time=5),
        control(ALL_SOUND_OFF, time=5),
        sustain(0, time=5),
        note_on(62, time=5),
        note_off(62, time=5),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 1


def test_reset_all_controllers_releases_pedal_and_held_notes():
    mid = make_file([
        sustain(127, time=0),
        note_on(60, time=0),
        note_off(60, time=5),
        control(RESET_ALL_CONTROLLERS, time=5),
        note_on(62, time=5),
        note_off(62, time=5),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 1


def test_velocity_zero_note_on_is_a_note_off():
    mid = make_file([
        note_on(60, time=0),
        note_on(60, velocity=0, time=10),
        note_on(62, time=0),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_lower == 1
    assert estimate.peak_upper == 1


def test_stale_note_off_is_ignored():
    mid = make_file([note_off(60, time=0), note_on(60, time=5)])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 1


def test_voice_cost_follows_program():
    mid = make_file([
        program(5, time=0),
        note_on(60, time=0),
        note_on(62, time=10),
        note_off(60, time=10),
        note_off(62, time=10),
    ])
    estimate = estimate_voices(mid, {5: 3})
    assert estimate.peak_upper == 6
    assert estimate.peak_lower == 6


def test_default_voice_cost_for_unknown_program():
    mid = make_file([note_on(60, time=0), note_on(62, time=5)])
    estimate = estimate_voices(mid, {}, default_voices=4)
    assert estimate.peak_upper == 8


def test_program_change_reprices_sounding_notes():
    mid = make_file([
        note_on(60, time=0),
        program(7, time=10),
        note_off(60, time=10),
    ])
    estimate = estimate_voices(mid, {7: 5})
    assert estimate.peak_upper == 5


def test_peak_position_records_tick_tracks_and_channels():
    mid = make_file(
        [note_on(60, channel=0, time=0), note_off(60, channel=0, time=10)],
        [note_on(70, channel=2, time=10)],
    )
    estimate = estimate_voices(mid, {})
    peak = next(peak for peak in estimate.peaks if peak.tick == 10)
    assert peak.tracks == (0, 1)
    assert peak.channels == (0, 2)
    assert 'note_off' in peak.kind
    assert 'note_on' in peak.kind


def test_meta_and_non_note_messages_do_not_add_voices():
    mid = make_file([
        Message('pitchwheel', channel=0, pitch=100, time=0),
        Message('polytouch', channel=0, note=60, value=10, time=0),
        note_on(60, time=5),
        note_off(60, time=5),
    ])
    estimate = estimate_voices(mid, {})
    assert estimate.peak_upper == 1


def test_type_two_file_is_rejected():
    mid = MidiFile(type=2)
    mid.tracks.append([note_on(60)])
    with pytest.raises(TypeError):
        estimate_voices(mid, {})


def test_invalid_voice_counts_are_rejected():
    mid = make_file([note_on(60)])
    with pytest.raises(ValueError):
        estimate_voices(mid, {}, default_voices=0)
    with pytest.raises(ValueError):
        estimate_voices(mid, {-1: 2})
    with pytest.raises(ValueError):
        estimate_voices(mid, {0: 0})


def test_large_file_is_streamed_without_growing_peak():
    mid = MidiFile(type=1)
    for track_index in range(6):
        track = []
        for _ in range(2000):
            track.append(note_on(60, time=1000 + 100 * track_index))
            track.append(note_off(60, time=500))
        mid.tracks.append(track)

    estimate = estimate_voices(mid, {})
    # Only a handful of notes can ever overlap, regardless of the
    # 24000 messages in the file.
    assert 1 <= estimate.peak_upper <= 6
    assert estimate.peak_lower <= estimate.peak_upper
