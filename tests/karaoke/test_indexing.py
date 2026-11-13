# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import io

import pytest

from mido import Message, MetaMessage, MidiFile, MidiTrack
from mido.karaoke import (
    LyricIndex,
    build_lyric_index,
    extract_lyric_events,
    extract_notes,
)


def make_file(lyrics, tracks):
    """Build a type 1 file from lyric deltas and vocal note specs.

    ``lyrics`` is a list of ``(text, delta)`` meta events.
    ``tracks`` is a list of ``(pitch, start_delta, length)`` lists.
    """
    mid = MidiFile(type=1, ticks_per_beat=480)
    lyric_track = MidiTrack()
    for text, delta in lyrics:
        lyric_track.append(MetaMessage('lyrics', text=text, time=delta))
    lyric_track.append(MetaMessage('end_of_track', time=0))
    mid.tracks.append(lyric_track)

    for notes in tracks:
        track = MidiTrack()
        for pitch, start_delta, length in notes:
            track.append(Message('note_on', note=pitch, velocity=64,
                                 time=start_delta))
            track.append(Message('note_off', note=pitch, velocity=64,
                                 time=length))
        track.append(MetaMessage('end_of_track', time=0))
        mid.tracks.append(track)

    return mid


def note_numbers(entry):
    return [tuple(note.note for note in option)
            for option in entry.note_candidates]


def test_extract_notes_pairs_on_and_off():
    track = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        Message('note_off', note=60, velocity=64, time=240),
        Message('note_on', note=62, velocity=64, time=240),
        Message('note_off', note=62, velocity=64, time=240),
    ])
    notes = extract_notes(track)
    # Times in the track are deltas; absolute starts are 0 and 480.
    assert [(n.note, n.start, n.end) for n in notes] == [
        (60, 0, 240), (62, 480, 720),
    ]


def test_extract_notes_accepts_velocity_zero_as_off():
    track = MidiTrack([
        Message('note_on', note=60, velocity=64, time=10),
        Message('note_on', note=60, velocity=0, time=90),
    ])
    notes = extract_notes(track)
    assert len(notes) == 1
    assert (notes[0].start, notes[0].end) == (10, 100)


def test_extract_notes_handles_overlapping_same_pitch():
    track = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        Message('note_on', note=60, velocity=64, time=100),
        Message('note_off', note=60, velocity=64, time=50),
        Message('note_off', note=60, velocity=64, time=50),
    ])
    notes = extract_notes(track)
    assert [(n.start, n.end) for n in notes] == [(0, 150), (100, 200)]


def test_extract_lyric_events_marks_hierarchy_only():
    track = MidiTrack([
        MetaMessage('lyrics', text='La', time=0),
        MetaMessage('lyrics', text='\n', time=10),
        MetaMessage('lyrics', text='', time=0),
    ])
    events = extract_lyric_events(track)
    assert [event.marker for event in events] == [False, True, True]
    assert [event.text for event in events] == ['La', '\n', '']


def test_melisma_one_syllable_over_several_notes():
    mid = make_file(
        [('Hold', 0), ('\n', 480)],
        [[(60, 0, 240), (62, 0, 240)]],
    )
    index = build_lyric_index(mid)
    entry = index.segments[0].entries[0]
    assert entry.text == 'Hold'
    assert note_numbers(entry) == [(60, 62)]
    assert entry.ambiguous is False


def test_split_syllables_on_one_note_keep_melody_candidates():
    mid = make_file(
        [('a', 0), ('b', 0), ('\n', 480)],
        [[(64, 0, 480)]],
    )
    index = build_lyric_index(mid)
    entries = index.segments[0].entries
    assert [entry.text for entry in entries] == ['a', 'b']
    for entry in entries:
        assert note_numbers(entry) == [(64,)]
        assert entry.start == 0
        assert entry.end == 480
    assert entries[0].ambiguous is False


def test_multiple_vocal_tracks_become_candidates():
    mid = make_file(
        [('La', 0), ('\n', 480)],
        [[(60, 0, 480)], [(72, 0, 480)]],
    )
    index = build_lyric_index(mid, vocal_tracks=[1, 2])
    entry = index.segments[0].entries[0]
    assert note_numbers(entry) == [(60,), (72,)]
    assert entry.ambiguous is True


def test_markers_do_not_consume_notes_or_syllable_indices():
    mid = make_file(
        [('a', 0), ('\n', 100), ('\r', 0), ('b', 380), ('\n', 0)],
        [[(60, 0, 240), (62, 240, 240)]],
    )
    index = build_lyric_index(mid, anchors=[(1, 240)])
    assert len(index.segments) == 2
    assert index.segments[0].text == 'a'
    assert index.segments[1].text == 'b'
    assert note_numbers(index.segments[0].entries[0]) == [(60,)]
    assert note_numbers(index.segments[1].entries[0]) == [(62,)]


def test_interlude_lookup_is_empty_segment():
    # First line ends at tick 480; next sung syllable starts at 1440.
    mid = make_file(
        [('La', 0), ('\n', 480), ('Boom', 960), ('\n', 480)],
        [[(60, 0, 480), (64, 960, 480)]],
    )
    index = build_lyric_index(mid)

    gap = index.lookup(900)
    assert gap.entry is None
    assert gap.text == ''
    assert gap.notes == ()
    assert gap.segment.entries == ()
    assert (gap.segment.start, gap.segment.end) == (480, 1440)

    # The previous line is not extended over the interlude.
    assert index.lyric_at(480) == ''
    assert index.lookup(500).segment.entries == ()
    # Singing resumes normally afterwards.
    assert index.lyric_at(1500) == 'Boom'


def test_lookup_inside_sung_line_returns_syllable_and_notes():
    mid = make_file(
        [('La', 0), ('la', 480), ('\n', 480)],
        [[(60, 0, 480), (62, 0, 480)]],
    )
    index = build_lyric_index(mid)
    result = index.lookup(600)
    assert result.entry.text == 'la'
    assert tuple(note.note for note in result.notes) == (62,)


def test_anchor_overrides_raw_lyric_position():
    mid = make_file(
        [('a', 0), ('b', 0), ('\n', 480)],
        [[(60, 0, 240), (62, 240, 240)]],
    )
    index = build_lyric_index(mid, anchors=[(1, 240)])
    entries = index.segments[0].entries
    assert entries[0].start == 0
    assert entries[1].start == 240
    assert note_numbers(entries[1]) == [(62,)]


def test_anchor_must_keep_syllables_ordered():
    mid = make_file(
        [('a', 100), ('b', 140), ('\n', 240)],
        [[(60, 0, 480)]],
    )
    with pytest.raises(ValueError):
        build_lyric_index(mid, anchors=[(1, 50)])


def test_explicit_vocal_track_selection():
    mid = make_file(
        [('La', 0), ('\n', 480)],
        [[(60, 0, 480)], [(72, 0, 480)]],
    )
    index = build_lyric_index(mid, vocal_tracks=[2])
    assert note_numbers(index.segments[0].entries[0]) == [(72,)]


def test_roundtrip_through_file_bytes():
    mid = make_file(
        [('La', 0), ('\n', 480), ('Boom', 480), ('\n', 480)],
        [[(60, 0, 480), (64, 480, 480)]],
    )
    buffer = io.BytesIO()
    mid.save(file=buffer)
    buffer.seek(0)
    loaded = MidiFile(file=buffer)

    index = build_lyric_index(loaded)
    assert isinstance(index, LyricIndex)
    assert [segment.text for segment in index.segments] == ['La', 'Boom']
    # 'Boom' starts at tick 960, after the 480-tick interlude.
    assert index.lyric_at(1000) == 'Boom'
    assert index.lyric_at(600) == ''


def test_missing_lyric_track_raises():
    mid = MidiFile(type=1)
    track = MidiTrack([
        Message('note_on', note=60, velocity=64, time=0),
        Message('note_off', note=60, velocity=64, time=10),
        MetaMessage('end_of_track', time=0),
    ])
    mid.tracks.append(track)
    with pytest.raises(ValueError):
        build_lyric_index(mid)


def test_no_vocal_track_raises():
    mid = MidiFile(type=1)
    track = MidiTrack([
        MetaMessage('lyrics', text='La', time=0),
        MetaMessage('end_of_track', time=0),
    ])
    mid.tracks.append(track)
    with pytest.raises(ValueError):
        build_lyric_index(mid, vocal_tracks=[])


def test_marker_hard_ends_line_without_extending_over_gap():
    # A newline at tick 240 hard-ends line 'a'; line 'b' starts at 480.
    # The 240..480 region must stay empty even though a note is held
    # across it.
    mid = make_file(
        [('a', 0), ('\n', 240), ('b', 240), ('\n', 480)],
        [[(60, 0, 720), (62, 0, 240)]],
    )
    index = build_lyric_index(mid)
    assert index.segments[0].end == 240

    gap = index.lookup(300)
    assert gap.entry is None
    assert gap.segment.entries == ()
    assert (gap.segment.start, gap.segment.end) == (240, 480)

    # The held note remains a melody candidate of the next sung line.
    next_entry = index.segments[1].entries[0]
    assert note_numbers(next_entry) == [(60, 62)]


def test_intro_gap_before_first_syllable_is_empty():
    # The first syllable starts at tick 240; everything before is an
    # instrumental intro and must not resolve to any lyric.
    mid = make_file(
        [('La', 240), ('\n', 240)],
        [[(60, 240, 240)]],
    )
    index = build_lyric_index(mid)
    result = index.lookup(100)
    assert result.entry is None
    assert result.segment.entries == ()
    assert (result.segment.start, result.segment.end) == (0, 240)
    assert index.lyric_at(240) == 'La'


def test_segment_text_and_lookup_beyond_end():
    mid = make_file(
        [('La', 0), ('\n', 480)],
        [[(60, 0, 480)]],
    )
    index = build_lyric_index(mid)
    assert index.segments[0].text == 'La'
    result = index.lookup(100000)
    assert result.entry is None
    assert result.segment.entries == ()
