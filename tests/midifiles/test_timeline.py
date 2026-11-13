# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import io
import math

from pytest import approx as pytest_approx
from pytest import raises

import mido
from mido import Message, MetaMessage, MidiFile, MidiTrack
from mido.midifiles.timeline import (
    TempoChange,
    TempoConflictError,
    TempoMap,
    Timeline,
)


def make_file(type=1, ticks_per_beat=480):
    return MidiFile(type=type, ticks_per_beat=ticks_per_beat)


def track(*messages):
    result = MidiTrack()
    for message in messages:
        result.append(message)
    return result


def tempo(tempo, time=0):
    return MetaMessage('set_tempo', tempo=tempo, time=time)


def note(note, time=0):
    return Message('note_on', note=note, velocity=64, time=time)


def collect_notes(mid):
    return [(msg.note, msg.time) for msg in mid
            if msg.type == 'note_on' and not msg.is_meta]


def test_tempo_changes_on_other_track_affect_iteration():
    mid = make_file()
    mid.tracks.append(track(tempo(500000), tempo(250000, time=480)))
    mid.tracks.append(track(note(60), note(62, time=480), note(64, time=480)))

    notes = collect_notes(mid)
    assert notes == [(60, 0.0), (62, 0.0), (64, 0.25)]
    assert mid.length == 0.75


def test_timeline_exposes_absolute_ticks_and_seconds():
    mid = make_file()
    mid.tracks.append(track(tempo(500000), tempo(250000, time=480)))
    mid.tracks.append(track(note(60), note(62, time=480), note(64, time=480)))

    events = mid.timeline.events

    notes = [event for event in events if event.type == 'note_on']
    assert [(event.track, event.tick, event.seconds) for event in notes] == [
        (1, 0, 0.0),
        (1, 480, 0.5),
        (1, 960, 0.75),
    ]

    assert mid.timeline.seconds_at_tick(0) == 0.0
    assert mid.timeline.seconds_at_tick(480) == 0.5
    assert mid.timeline.seconds_at_tick(960) == 0.75
    assert mid.timeline.tick_at_seconds(0.75) == 960
    assert mid.timeline.tempo_at_tick(960) == 250000


def test_moving_tempo_messages_to_another_track_does_not_change_note_times():
    def build(tempo_track, note_track):
        mid = make_file()
        tracks = [MidiTrack(), MidiTrack()]
        tracks[tempo_track].append(tempo(400000, time=240))
        tracks[tempo_track].append(tempo(200000, time=240))
        tracks[note_track].append(note(60))
        tracks[note_track].append(note(62, time=480))
        tracks[note_track].append(note(64, time=480))
        mid.tracks.extend(tracks)
        return mid

    first = build(0, 1)
    second = build(1, 0)

    first_notes = [
        (event.tick, event.seconds) for event in first.timeline
        if event.type == 'note_on']
    second_notes = [
        (event.tick, event.seconds) for event in second.timeline
        if event.type == 'note_on']

    assert first_notes == second_notes
    assert first.length == second.length


def test_conflict_reports_earliest_uncertain_point():
    store = MidiTrack()
    store.append(tempo(300000, time=480))
    store.append(tempo(400000, time=0))

    mid = make_file()
    mid.tracks.append(store)
    mid.tracks.append(track(note(60), note(62, time=480), note(64, time=480)))

    timeline = mid.timeline
    conflict = timeline.first_uncertainty

    assert conflict is not None
    assert conflict.start_tick == 480
    assert conflict.start_seconds == 0.5
    assert conflict.tempos == (300000, 400000)
    assert timeline.uncertain_from_tick == 480
    assert timeline.uncertain_from_seconds == 0.5
    assert timeline.uncertain_interval == (480, None)

    # Before the conflict time is exact.
    assert timeline.seconds_at_tick(240) == 0.25
    assert timeline.seconds_at_tick_bounds(240) == (0.25, 0.25)

    # After it, bounds cover both candidates.
    low, high = timeline.seconds_at_tick_bounds(960)
    assert math.isclose(low, 0.5 + 480 * 300000 * 1e-6 / 480)
    assert math.isclose(high, 0.5 + 480 * 400000 * 1e-6 / 480)
    assert low < high

    with raises(TempoConflictError):
        timeline.seconds_at_tick(960, strict=True)


def test_no_conflict_when_same_tempo_declared_twice_at_same_tick():
    map_ = TempoMap([
        TempoChange(tick=480, tempo=400000, track=0, index=0),
        TempoChange(tick=480, tempo=400000, track=1, index=0),
    ], ticks_per_beat=480)

    assert map_.first_conflict is None
    assert map_.conflicts == ()
    assert map_.tempo_at_tick(960) == 400000
    assert map_.seconds_at_tick(960, strict=True) == pytest_approx(0.9)


def test_tempo_at_tick_zero_and_default_tempo():
    map_ = TempoMap([], ticks_per_beat=100)
    assert map_.tempo_at_tick(0) == 500000
    assert map_.seconds_at_tick(100) == 0.5

    map_ = TempoMap([TempoChange(0, 100000, 0, 0)], ticks_per_beat=100)
    assert map_.segments[0].start_tick == 0
    assert map_.segments[0].end_tick is None
    assert map_.seconds_at_tick(100) == 0.1


def test_type_2_tracks_have_independent_time_bases():
    mid = make_file(type=2)
    mid.tracks.append(track(tempo(500000), note(60, time=480)))
    mid.tracks.append(track(tempo(250000), note(62, time=480)))

    timeline = mid.timeline
    assert len(timeline.tempo_maps) == 2
    assert timeline.tracks[0].seconds_at_tick(480) == 0.5
    assert timeline.tracks[1].seconds_at_tick(480) == 0.25
    assert timeline.seconds_at_tick(480, track=0) == 0.5
    assert timeline.seconds_at_tick(480, track=1) == 0.25

    with raises(ValueError):
        timeline.seconds_at_tick(480)
    with raises(TypeError):
        timeline.tempo_map


def test_type_2_does_not_combine_tempo_changes_from_other_tracks():
    mid = make_file(type=2)
    mid.tracks.append(track(tempo(250000)))
    mid.tracks.append(track(tempo(1000000), note(60, time=480)))

    assert mid.timeline.tracks[1].seconds_at_tick(480) == 1.0


def test_event_table_is_per_track_and_keeps_original_messages():
    mid = make_file()
    mid.tracks.append(track(tempo(500000), tempo(250000, time=480)))
    mid.tracks.append(track(note(60), note(62, time=480)))

    rows = mid.timeline.event_table()
    note_rows = [row for row in rows if row['type'] == 'note_on']

    assert [row['track'] for row in note_rows] == [1, 1]
    assert [row['tick'] for row in note_rows] == [0, 480]
    assert [row['delta_tick'] for row in note_rows] == [0, 480]
    assert [row['seconds'] for row in note_rows] == [0.0, 0.5]
    assert note_rows[1]['message'] is mid.tracks[1][1]
    assert note_rows[1]['message'].time == 480

    track_rows = mid.timeline.track(1).event_table()
    assert [row['index'] for row in track_rows] == [0, 1]


def test_timeline_round_trips_through_saved_file():
    mid = make_file()
    mid.tracks.append(track(tempo(400000), tempo(200000, time=480)))
    mid.tracks.append(track(note(60), note(62, time=960)))

    buffer = io.BytesIO()
    mid.save(file=buffer)
    buffer.seek(0)
    loaded = MidiFile(file=buffer)

    assert [event.seconds for event in loaded.timeline
            if event.type == 'note_on'] == pytest_approx([0.0, 0.6])


def test_timeline_is_exported_from_package():
    assert mido.Timeline is Timeline
    assert mido.TempoMap is TempoMap


def test_timeline_rebuilds_after_tracks_are_edited():
    mid = make_file()
    mid.tracks.append(track(tempo(250000)))
    mid.tracks.append(track(note(60, time=480)))
    assert mid.timeline.seconds_at_tick(480) == 0.25

    mid.tracks.append(track(note(62, time=480)))
    assert mid.timeline.seconds_at_tick(480) == 0.25
    assert len(mid.timeline.events) == 3

    mid.tracks[0][0] = tempo(125000)
    assert mid.timeline.seconds_at_tick(480) == 0.125

    mid.tracks[0] = track(tempo(500000))
    assert mid.timeline.seconds_at_tick(480) == 0.5


def test_segment_and_conflict_sequence_access():
    store = track(tempo(300000, time=480), tempo(400000, time=0))
    mid = make_file()
    mid.tracks.append(store)

    conflict = mid.timeline.first_conflict
    assert tuple(conflict) == (480, None)
    assert conflict == (480, None)
    assert conflict[0] == 480
    assert conflict.tempos == (300000, 400000)

    segment = mid.timeline.tempo_map[0]
    assert tuple(segment) == (0, 480, 500000)


def test_event_table_contains_message_fields():
    mid = make_file()
    mid.tracks.append(track(note(60, time=0)))

    row = mid.timeline.event_table()[0]
    assert row['note'] == 60
    assert row['velocity'] == 64
    assert row['channel'] == 0
    assert row['seconds'] == 0.0
    assert row['delta_tick'] == 0


def test_tempo_map_positional_arguments_and_dict_input():
    assert TempoMap([(0, 250000)], 480).seconds_at(480) == 0.25
    assert TempoMap({0: 500000, 480: 250000}, 480).seconds_at(960) == 0.75
    assert TempoMap.from_dict({0: 250000}, 480).seconds_at(480) == 0.25


def test_tempo_map_rejects_invalid_values():
    with raises(ValueError):
        TempoMap([], ticks_per_beat=0)
    with raises(ValueError):
        TempoMap([TempoChange(-1, 500000, 0, 0)])
    with raises(ValueError):
        TempoMap([TempoChange(0, 0, 0, 0)])


def test_merged_table_has_playback_order_deltas():
    mid = make_file()
    mid.tracks.append(track(tempo(250000, time=0), tempo(500000, time=480)))
    mid.tracks.append(track(note(60, time=0), note(62, time=960)))

    rows = mid.timeline.merged_table()
    assert [row['global_delta_tick'] for row in rows] == [0, 0, 480, 480]
    assert rows[-1]['global_delta_seconds'] == pytest_approx(0.5)


def test_type_2_length_is_rejected_but_track_lengths_are_available():
    mid = make_file(type=2)
    mid.tracks.append(track(tempo(500000), note(60, time=480)))
    mid.tracks.append(track(tempo(250000), note(62, time=480)))

    with raises(ValueError):
        mid.length
    with raises(TypeError):
        list(mid)
    assert mid.timeline.track_lengths == (0.5, 0.25)


def test_conflict_affects_every_later_event():
    store = track(tempo(300000, time=480), tempo(400000, time=0))
    mid = make_file()
    mid.tracks.append(store)
    mid.tracks.append(track(note(60), note(62, time=960)))

    timeline = mid.timeline
    assert timeline.first_conflict.end_tick is None
    assert timeline.uncertain_from_tick == 480
    low, high = timeline.seconds_at_tick_bounds(1440)
    assert low < high
    with raises(TempoConflictError):
        timeline.tick_at_seconds(2.0, strict=True)


def test_midifile_query_helpers():
    mid = make_file()
    mid.tracks.append(track(tempo(250000, time=480)))
    mid.tracks.append(track(note(60, time=480)))

    assert mid.get_timeline() is mid.timeline
    assert mid.timeline_for_track(1).length == 0.5
    assert mid.get_tempo_map().seconds_at(480) == 0.5
    assert mid.track_lengths == (0.5, 0.5)
    assert mid.length_bounds == (0.5, 0.5)
    assert mid.export_event_table(track=1)[0]['seconds'] == 0.5


def test_type_2_midifile_query_helpers():
    mid = make_file(type=2)
    mid.tracks.append(track(tempo(500000), note(60, time=480)))
    mid.tracks.append(track(tempo(250000), note(62, time=480)))

    assert mid.get_tempo_map(0).seconds_at(480) == 0.5
    assert mid.get_tempo_map(1).seconds_at(480) == 0.25
    assert mid.timeline_for_track(1).seconds_at_tick(480) == 0.25
    with raises(TypeError):
        mid.get_tempo_map()


def test_events_between_and_at_tick():
    mid = make_file()
    mid.tracks.append(track(tempo(250000), tempo(500000, time=480)))
    mid.tracks.append(track(note(60), note(62, time=480), note(64, time=960)))

    timeline = mid.timeline
    assert [event.type for event in timeline.events_at_tick(480)] == [
        'set_tempo', 'note_on']
    assert [event.tick for event in timeline.events_between(480, 960)] == [
        480, 480]
