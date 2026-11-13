# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import pytest

from mido import Message, MetaMessage, MidiFile, MidiTrack
from mido.midifiles.notes import (
    SegmentMatch,
    find_all_matches,
    find_note_matches,
    find_segment_matches,
    migrate_annotations,
    migrate_notes,
    transpose_messages,
)


def phrase(pitches, gap=10, duration=10, start=0, channel=0):
    """Build a track containing one note_on/note_off pair per pitch."""
    track = MidiTrack()
    for index, pitch in enumerate(pitches):
        track.append(Message(
            'note_on', channel=channel, note=pitch, velocity=64,
            time=start if index == 0 else gap))
        track.append(Message(
            'note_off', channel=channel, note=pitch, velocity=0,
            time=duration))
    return track


def shifted(track, semitones, gap=0, before=None):
    """Transpose a track's notes and optionally offset its first time."""
    result = MidiTrack()
    for index, msg in enumerate(track):
        time = msg.time + gap if index == 0 and before is None else msg.time
        if msg.type in ('note_on', 'note_off'):
            result.append(msg.copy(note=msg.note + semitones, time=time))
        else:
            result.append(msg.copy(time=time))
    if before:
        result = MidiTrack(before) + result
    return result


def test_transposes_within_range():
    source = phrase([60, 62, 64, 65])
    target = phrase([67, 69, 71, 72])

    matches = find_segment_matches(
        source, target, transpose_range=range(0, 13))

    assert len(matches) == 1
    match = matches[0]
    assert match.start == 0
    assert match.stop == len(target)
    assert match.offset == 7
    assert match.transpose == 7
    assert match.track is None


def test_transposition_outside_range_is_not_reported():
    source = phrase([60, 62, 64, 65])
    target = phrase([67, 69, 71, 72])

    assert find_segment_matches(
        source, target, transpose_range=range(0, 7)) == []


def test_negative_transposition():
    source = phrase([60, 62, 64, 65])
    target = phrase([55, 57, 59, 60])

    matches = find_segment_matches(
        source, target, transpose_range=(-5, -5))

    assert [m.offset for m in matches] == [-5]


def test_ornaments_are_skipped_with_insert_tolerance():
    source = phrase([60, 62, 64])
    target = phrase([60, 80, 62, 81, 64])

    matches = find_segment_matches(
        source, target, transpose_range=(0, 0),
        insert_tolerance=4, time_tolerance=100)

    assert len(matches) == 1
    assert matches[0].start == 0
    assert matches[0].stop == len(target)


def test_insert_tolerance_is_a_count_not_a_large_gap():
    source = phrase([60, 62, 64])
    target = phrase([60, 80, 62, 81, 64])

    assert find_segment_matches(
        source, target, transpose_range=(0, 0),
        insert_tolerance=1, time_tolerance=100) == []


def test_relative_timing_is_preserved_after_insertion():
    source = phrase([60, 62, 64], gap=10)
    target = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        Message('note_off', note=60, velocity=0, time=10),
        Message('note_on', note=80, velocity=64, time=5),
        Message('note_off', note=80, velocity=0, time=5),
        Message('note_on', note=62, velocity=64, time=10),
        Message('note_off', note=62, velocity=0, time=10),
        Message('note_on', note=64, velocity=64, time=10),
        Message('note_off', note=64, velocity=0, time=10),
    ])

    matches = find_segment_matches(
        source, target, transpose_range=(0, 0),
        insert_tolerance=2, time_tolerance=0)

    assert len(matches) == 1


def test_repeated_chorus_returns_both_candidates():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64]) + phrase([60, 62, 64], start=100)

    matches = find_segment_matches(
        source, target, transpose_range=(0, 0))

    assert len(matches) == 2
    assert [m.start for m in matches] == [0, 6]
    assert matches[0].start < matches[1].start


def test_repeated_chorus_with_transposition_returns_both():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64]) + phrase([67, 69, 71], start=100)

    matches = find_segment_matches(
        source, target, transpose_range=(0, 12))

    assert len(matches) == 2
    assert sorted(m.offset for m in matches) == [0, 7]


def test_track_numbers_are_ignored():
    source = MidiFile()
    source.add_track().extend(phrase([60, 62, 64]))

    target = MidiFile()
    target.add_track().extend(phrase([67, 69, 71], channel=3))

    matches = source.find_segment_matches(
        target, transpose_range=(7, 7))

    assert len(matches) == 1
    assert matches[0].offset == 7


def test_merged_tracks_match_across_tracks():
    source = MidiFile()
    source.add_track().extend(phrase([60, 62]))
    source.add_track().extend(phrase([64, 65], start=20))

    # The same performance flattened into one track: the second part is
    # on a different original track but at the same absolute times.
    target = MidiTrack(source.merged_track)

    matches = source.find_segment_matches(
        target, transpose_range=(0, 0))

    assert len(matches) == 1


def test_two_anchors_select_the_locked_occurrence():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64]) + phrase([60, 62, 64], start=100)

    # Both editor-locked anchors point into the second chorus.
    matches = find_segment_matches(
        source,
        target,
        transpose_range=(0, 0),
        anchors=[
            (source[0], target[6]),
            (source[-1], target[11]),
        ])

    assert len(matches) == 1
    assert matches[0].start == 6


def test_two_anchors_can_select_the_first_occurrence():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64]) + phrase([60, 62, 64], start=100)

    matches = find_segment_matches(
        source,
        target,
        transpose_range=(0, 0),
        anchors=[
            (source[0], target[0]),
            (source[-1], target[5]),
        ])

    assert len(matches) == 1
    assert matches[0].start == 0


def test_anchor_can_be_absolute_time():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64]) + phrase([60, 62, 64], start=100)

    matches = find_segment_matches(
        source, target, transpose_range=(0, 0),
        anchors=[(0, 150), (50, 200)])

    assert len(matches) == 1
    assert matches[0].start == 6


def test_unknown_anchor_raises():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64])

    with pytest.raises(ValueError):
        find_segment_matches(
            source, target, anchors=[(999, 0)])


def test_original_track_layout_is_not_required_for_target():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64], channel=9)

    matches = find_note_matches(source, target, transpose_range=(0, 0))

    assert len(matches) == 1


def test_meta_messages_inside_run_are_matched():
    source = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        MetaMessage('marker', text='cue', time=0),
        Message('note_off', note=60, velocity=0, time=10),
    ])
    target = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        MetaMessage('marker', text='cue', time=0),
        Message('note_off', note=60, velocity=0, time=10),
    ])

    matches = find_segment_matches(source, target, transpose_range=(0, 0))

    assert len(matches) == 1


def test_inserted_meta_event_within_tolerance():
    source = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        Message('note_off', note=60, velocity=0, time=10),
    ])
    target = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        MetaMessage('marker', text='ornament', time=0),
        Message('note_off', note=60, velocity=0, time=10),
    ])

    matches = find_segment_matches(
        source, target, transpose_range=(0, 0),
        insert_tolerance=1, time_tolerance=0)

    assert len(matches) == 1


def test_note_match_is_tuple_compatible():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64])

    match = find_segment_matches(
        source, target, transpose_range=(0, 0))[0]

    start, stop, offset, events = match
    assert start == 0
    assert stop == 6
    assert offset == 0
    assert len(events) == 6
    assert isinstance(match, SegmentMatch)
    assert match.index == match.start
    assert match.messages == match.events


def test_aliases_agree():
    source = phrase([60, 62, 64])
    target = phrase([60, 62, 64])

    assert find_all_matches(
        source, target, transpose_range=(0, 0)) == find_segment_matches(
            source, target, transpose_range=(0, 0))
    assert migrate_notes(
        source, target, transpose_range=(0, 0)) == find_segment_matches(
            source, target, transpose_range=(0, 0))
    assert migrate_annotations(
        source, target, transpose_range=(0, 0)) == find_segment_matches(
            source, target, transpose_range=(0, 0))


def test_transpose_messages_only_changes_note_pitch():
    track = MidiTrack([
        Message('program_change', program=1, time=0),
        Message('note_on', note=60, velocity=64, time=5),
        Message('note_off', note=60, velocity=0, time=10),
    ])

    result = transpose_messages(track, 2)

    assert result[0] == track[0]
    assert result[1].note == 62
    assert result[2].note == 62
    assert result[1].time == 5


def test_empty_inputs_return_no_matches():
    empty = MidiTrack()

    assert find_segment_matches(
        empty, phrase([60]), transpose_range=(0, 0)) == []
    assert find_segment_matches(
        phrase([60]), empty, transpose_range=(0, 0)) == []


def test_invalid_tolerances():
    source = phrase([60])
    target = phrase([60])

    with pytest.raises(ValueError):
        find_segment_matches(
            source, target, insert_tolerance=-1)
    with pytest.raises(ValueError):
        find_segment_matches(
            source, target, time_tolerance=-1)
