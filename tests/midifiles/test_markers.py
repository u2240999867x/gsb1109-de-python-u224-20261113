# SPDX-FileCopyrightText: 2026 The mido contributors
#
# SPDX-License-Identifier: MIT

import io

import pytest

import mido
from mido.midifiles.markers import (
    Marker,
    MarkerGroup,
    MarkerTable,
    merge_markers,
)
from mido.midifiles.meta import MetaMessage
from mido.midifiles.tracks import MidiTrack


def marker_message(name, time):
    return MetaMessage('marker', text=name, time=time)


def cue_message(name, time):
    return MetaMessage('cue_marker', text=name, time=time)


def track_from_abs(entries):
    """Build a track from ``(type, name, absolute_time)`` entries."""
    messages = []
    previous = 0
    for kind, name, absolute in entries:
        messages.append(MetaMessage(kind, text=name,
                                    time=absolute - previous))
        previous = absolute
    return MidiTrack(messages)


def marker_track(*entries):
    """Build a track from ``(name, absolute_time)`` marker entries."""
    return track_from_abs([('marker', name, time)
                           for name, time in entries])


def test_marker_uses_absolute_time_and_ignores_cue_markers():
    track = track_from_abs([
        ('marker', 'verse', 100),
        ('cue_marker', 'not-a-marker', 150),
        ('marker', 'chorus', 300),
    ])

    table = merge_markers([track])

    assert table.groups == (
        MarkerGroup(Marker('verse', 100, 0)),
        MarkerGroup(Marker('chorus', 300, 0)),
    )


def test_replica_markers_merge_but_keep_original_positions():
    primary = marker_track(('intro', 100))
    replica = marker_track(('intro', 107))

    table = merge_markers([primary, replica], [(0, 1)], tolerance=10)

    assert len(table) == 1
    group = table.groups[0]
    assert group.resolved
    assert group.primary == Marker('intro', 100, 0)
    # The copy stays at its own absolute tick (107) instead of being
    # snapped to the primary position (100).
    assert group.copies == (Marker('intro', 107, 1),)
    assert group.tracks == (0, 1)


def test_same_name_markers_on_one_track_are_never_collapsed():
    # Two markers with the same name, written for two different scenes.
    primary = marker_track(('scene', 100), ('scene', 200))
    replica = marker_track(('scene', 100), ('scene', 200))

    table = merge_markers([primary, replica], [(0, 1)], tolerance=50)

    assert len(table) == 2
    replica_tracks = [
        copy.track
        for group in table
        for copy in group.copies
    ]
    # Each replica marker is assigned at most once.
    assert sorted(replica_tracks) == [1, 1]
    observations = {(m.track, m.time) for m in table.markers()}
    assert observations == {(0, 100), (0, 200), (1, 100), (1, 200)}


def test_ambiguous_replica_keeps_both_interpretations():
    # A single replica marker sits within tolerance of two adjacent
    # primary markers with the same name.
    primary = marker_track(('scene', 100), ('scene', 200))
    replica = marker_track(('scene', 150))

    table = merge_markers([primary, replica], [(0, 1)], tolerance=60)

    assert len(table) == 2
    ambiguous = table.unresolved()
    assert len(ambiguous) == 1
    group = ambiguous[0]
    assert group.primary == Marker('scene', 100, 0)
    assert group.copies == (Marker('scene', 150, 1),)
    assert group.candidates == (Marker('scene', 200, 0),)
    assert [m.time for m in group.interpretations()] == [100, 200]

    # Both primary markers still have their own row: no interpretation
    # is allowed to merge two markers of the same track.
    assert [g.primary.time for g in table] == [100, 200]
    assert table.counts == (1, 1)
    assert table.resolved_count == 1
    assert table.unresolved_count == 1


def test_markers_outside_tolerance_are_not_merged():
    primary = marker_track(('intro', 100))
    replica = marker_track(('intro', 120))

    table = merge_markers([primary, replica], [(0, 1)], tolerance=10)

    assert len(table) == 2
    assert all(group.resolved for group in table)
    standalone = [g for g in table if g.primary.track == 1]
    assert len(standalone) == 1
    assert standalone[0].primary.time == 120
    assert table.unresolved_count == 0


def test_markers_with_different_names_are_not_merged():
    primary = marker_track(('intro', 100))
    replica = marker_track(('outro', 100))

    table = merge_markers([primary, replica], [(0, 1)], tolerance=10)

    assert len(table) == 2
    assert {group.name for group in table} == {'intro', 'outro'}


def test_unmatched_replica_marker_is_kept_on_its_own_track():
    primary = marker_track(('intro', 100))
    replica = marker_track(('intro', 100), ('encore', 400))

    table = merge_markers([primary, replica], [(0, 1)], tolerance=0)

    names = {group.primary.name: group.primary.track for group in table}
    assert names == {'intro': 0, 'encore': 1}


def test_tracks_outside_replica_groups_are_left_alone():
    solo = marker_track(('a', 10), ('a', 20))

    table = merge_markers([solo], replica_groups=(), tolerance=100)

    assert len(table) == 2
    assert table.counts == (2, 0)


def test_multiple_replica_groups_and_primary_selection():
    tracks = [
        marker_track(('a', 100)),
        marker_track(('a', 101)),
        marker_track(('b', 200)),
        marker_track(('b', 202)),
    ]

    table = merge_markers(tracks, [(0, 1), (2, 3)], tolerance=5)

    assert len(table) == 2
    by_name = {group.name: group for group in table}
    assert by_name['a'].primary.track == 0
    assert by_name['a'].copies[0].track == 1
    assert by_name['b'].primary.track == 2
    assert by_name['b'].copies[0].track == 3
    assert table.counts == (2, 0)


def test_invalid_arguments():
    tracks = [marker_track(('a', 0)), MidiTrack()]

    with pytest.raises(ValueError):
        merge_markers(tracks, [(0, 1)], tolerance=-1)

    with pytest.raises(ValueError):
        merge_markers(tracks, [(0, 1)], tolerance=1.5)

    with pytest.raises(ValueError):
        merge_markers(tracks, [(0, 0)])

    with pytest.raises(ValueError):
        merge_markers(tracks, [(0, 1), (1,)])

    with pytest.raises(ValueError):
        merge_markers(tracks, [(0, 5)])


def test_marker_table_iteration_and_helpers():
    table = MarkerTable([
        MarkerGroup(Marker('a', 10, 0)),
        MarkerGroup(Marker('b', 20, 0),
                    candidates=[Marker('b', 25, 0)]),
    ])

    assert len(table) == 2
    assert [group.name for group in table] == ['a', 'b']
    assert [group.name for group in table.resolved()] == ['a']
    assert [group.name for group in table.unresolved()] == ['b']
    assert table.counts == (1, 1)
    assert {(m.name, m.time) for m in table.markers()} == {
        ('a', 10), ('b', 20), ('b', 25),
    }


def test_midifile_merge_markers_round_trip():
    mid = mido.MidiFile(ticks_per_beat=480)
    primary = mid.add_track('primary')
    primary.append(marker_message('scene', 100))
    primary.append(marker_message('scene', 100))
    replica = mid.add_track('replica')
    replica.append(marker_message('scene', 150))

    buffer = io.BytesIO()
    mid.save(file=buffer)
    buffer.seek(0)
    loaded = mido.MidiFile(file=buffer)

    table = loaded.merge_markers(replica_groups=[(0, 1)], tolerance=60)

    assert isinstance(table, MarkerTable)
    assert table.counts == (1, 1)
    ambiguous = table.unresolved()[0]
    assert ambiguous.primary.time == 100
    assert ambiguous.copies[0].time == 150
    assert ambiguous.candidates[0].time == 200
