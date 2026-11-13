#!/usr/bin/env python
# coding=utf8

from __future__ import unicode_literals

from datetime import timedelta

import re
import pytest

import srt


def make_sub(index, start_seconds, end_seconds, content):
    return srt.Subtitle(
        index=index,
        start=timedelta(seconds=start_seconds),
        end=timedelta(seconds=end_seconds),
        content=content,
    )


MIXED_SRT = """\
1
00:00:01,000 --> 00:00:02,000
Alice: hi there
Bob: hello

2
00:00:03,000 --> 00:00:04,000
just a plain line
wrapped continuation

3
00:00:05,000 --> 00:00:06,000
Ally: renamed now
still me

4
00:00:10,000 --> 00:00:11,000
Alice: one
Bob: two
Alice: three
"""


def parse_mixed():
    return list(srt.parse(MIXED_SRT))


def all_source_lines(subtitles):
    return [line for sub in subtitles for line in sub.content.split("\n")]


def all_spoken_lines(subtitles):
    return [
        srt.parse_speaker_line(line)[1]
        for sub in subtitles
        for line in sub.content.split("\n")
    ]


def all_track_lines(tracks):
    return [
        line
        for _speaker, subtitle in tracks.sequence
        for line in subtitle.content.split("\n")
    ]


def test_parse_speaker_line_marker_variants():
    assert srt.parse_speaker_line("Alice: hello") == ("Alice", "hello")
    assert srt.parse_speaker_line("Alice：hello") == ("Alice", "hello")
    assert srt.parse_speaker_line("Alice:hello") == ("Alice", "hello")
    assert srt.parse_speaker_line("plain continuation") == (None, "plain continuation")
    assert srt.parse_speaker_line("") == (None, "")


def test_split_two_speakers_sharing_one_subtitle():
    subtitles = [make_sub(1, 1, 2, "Alice: hi there\nBob: hello")]
    tracks = srt.split_speaker_tracks(subtitles)

    assert tracks.speakers() == ["Alice", "Bob"]
    assert [sub.content for sub in tracks.tracks["Alice"]] == ["hi there"]
    assert [sub.content for sub in tracks.tracks["Bob"]] == ["hello"]

    for fragment in tracks.sequence:
        assert fragment[1].start == timedelta(seconds=1)
        assert fragment[1].end == timedelta(seconds=2)


def test_continuation_lines_belong_to_last_speaker():
    subtitles = [make_sub(1, 1, 2, "Alice: first line\nsecond line\nthird line")]
    tracks = srt.split_speaker_tracks(subtitles)

    assert list(tracks.tracks) == ["Alice"]
    assert tracks.tracks["Alice"][0].content == "first line\nsecond line\nthird line"
    assert tracks.unattributed == []


def test_unattributed_lines_keep_original_position():
    subtitles = parse_mixed()
    tracks = srt.split_speaker_tracks(subtitles)

    assert len(tracks.unattributed) == 1
    leftover = tracks.unattributed[0]
    assert leftover.content == "just a plain line\nwrapped continuation"
    assert leftover.start == timedelta(seconds=3)
    assert leftover.end == timedelta(seconds=4)


def test_alias_table_maps_renamed_display_name_with_window():
    subtitles = parse_mixed()
    aliases = srt.SpeakerAliasTable(
        [("Alice", "Ally", (timedelta(seconds=5), timedelta(seconds=8)))]
    )
    tracks = srt.split_speaker_tracks(subtitles, aliases=aliases)

    assert "Ally" not in tracks.tracks
    alice_contents = [sub.content for sub in tracks.tracks["Alice"]]
    assert "renamed now\nstill me" in alice_contents


def test_alias_window_is_half_open():
    aliases = srt.SpeakerAliasTable()
    start = timedelta(seconds=5)
    end = timedelta(seconds=8)
    aliases.add_alias("Alice", "Ally", (start, end))

    assert aliases.resolve("Ally", timedelta(seconds=5)) == "Alice"
    assert aliases.resolve("Ally", timedelta(seconds=7, milliseconds=999)) == "Alice"
    assert aliases.resolve("Ally", timedelta(seconds=8)) == "Ally"
    assert aliases.resolve("Ally", timedelta(seconds=4)) == "Ally"


def test_alias_without_window_always_valid():
    aliases = srt.SpeakerAliasTable([("Alice", "Ally")])
    assert aliases.resolve("Ally", timedelta(seconds=0)) == "Alice"
    assert aliases.resolve("Ally", timedelta(days=99)) == "Alice"
    assert aliases.resolve("Bob", timedelta(seconds=0)) == "Bob"


def test_last_added_alias_wins_and_bad_window_raises():
    aliases = srt.SpeakerAliasTable(
        [
            ("Alice", "X", (timedelta(seconds=0), timedelta(seconds=10))),
            ("Bob", "X", (timedelta(seconds=0), timedelta(seconds=10))),
        ]
    )
    assert aliases.resolve("X", timedelta(seconds=5)) == "Bob"

    with pytest.raises(ValueError):
        srt.SpeakerAliasTable(
            [("Alice", "X", (timedelta(seconds=10), timedelta(seconds=5)))]
        )


def test_line_count_and_order_are_preserved():
    subtitles = parse_mixed()
    tracks = srt.split_speaker_tracks(subtitles)

    source_lines = all_source_lines(subtitles)
    spoken_lines = all_spoken_lines(subtitles)
    output_lines = all_track_lines(tracks)
    # Marker lines lose only their "Name: " prefix; the spoken text, the
    # total number of lines and their relative order are unchanged.
    assert output_lines == spoken_lines
    assert len(output_lines) == len(source_lines)


def test_active_at_rebuilds_utterance_order_with_repeats():
    subtitles = parse_mixed()
    aliases = srt.SpeakerAliasTable(
        [("Alice", "Ally", (timedelta(seconds=5), timedelta(seconds=8)))]
    )
    tracks = srt.split_speaker_tracks(subtitles, aliases=aliases)

    active = tracks.active_at(timedelta(seconds=10, milliseconds=500))
    assert [speaker for speaker, _sub in active] == ["Alice", "Bob", "Alice"]
    assert [sub.content for _speaker, sub in active] == ["one", "two", "three"]

    earlier = tracks.active_at(timedelta(seconds=5, milliseconds=500))
    assert [speaker for speaker, _sub in earlier] == ["Alice"]
    assert earlier[0][1].content == "renamed now\nstill me"

    assert tracks.active_at(timedelta(seconds=30)) == []


def test_active_at_includes_unattributed_in_place():
    subtitles = parse_mixed()
    tracks = srt.split_speaker_tracks(subtitles)
    active = tracks.active_at(timedelta(seconds=3, milliseconds=500))
    assert active == [(None, tracks.unattributed[0])]


def test_reindex_per_track_and_original_indices():
    subtitles = parse_mixed()
    aliases = srt.SpeakerAliasTable(
        [("Alice", "Ally", (timedelta(seconds=5), timedelta(seconds=8)))]
    )

    reindexed = srt.split_speaker_tracks(subtitles, aliases=aliases, reindex=True)
    assert [sub.index for sub in reindexed.tracks["Alice"]] == [1, 2, 3, 4]
    assert [sub.index for sub in reindexed.tracks["Bob"]] == [1, 2]

    raw = srt.split_speaker_tracks(subtitles, aliases=aliases, reindex=False)
    shared = [sub for sub in raw.tracks["Bob"]]
    assert [sub.index for sub in shared] == [1, 4]


def test_custom_marker_regex():
    subtitles = [make_sub(1, 1, 2, "[Alice] hi\n[Bob] hello")]
    bracket_rule = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
    tracks = srt.split_speaker_tracks(subtitles, marker_regex=bracket_rule)

    assert tracks.tracks["Alice"][0].content == "hi"
    assert tracks.tracks["Bob"][0].content == "hello"

    default_tracks = srt.split_speaker_tracks(subtitles)
    assert default_tracks.tracks == {}
    assert default_tracks.unattributed[0].content == "[Alice] hi\n[Bob] hello"


def test_tracks_round_trip_through_compose():
    subtitles = parse_mixed()
    tracks = srt.split_speaker_tracks(subtitles)

    alice_srt = tracks.compose_track("Alice")
    reparsed = list(srt.parse(alice_srt))
    assert [sub.content for sub in reparsed] == [
        sub.content for sub in tracks.tracks["Alice"]
    ]

    leftover_srt = tracks.compose_unattributed()
    assert list(srt.parse(leftover_srt))[0].content == (
        "just a plain line\nwrapped continuation"
    )


def test_full_width_colon_marker():
    subtitles = [make_sub(1, 1, 2, "张三：你好\n李四：嗨")]
    tracks = srt.split_speaker_tracks(subtitles)
    assert tracks.tracks["张三"][0].content == "你好"
    assert tracks.tracks["李四"][0].content == "嗨"


def test_alias_table_equality_and_len():
    entries = [("Alice", "Ally", (timedelta(seconds=5), timedelta(seconds=8)))]
    assert srt.SpeakerAliasTable(entries) == srt.SpeakerAliasTable(entries)
    assert len(srt.SpeakerAliasTable(entries)) == 1
    assert srt.SpeakerAliasTable(entries) != object()


def test_speaker_tracks_equality_and_repr():
    subtitles = parse_mixed()
    first = srt.split_speaker_tracks(subtitles)
    second = srt.split_speaker_tracks(parse_mixed())
    assert first == second
    assert first != object()
    assert "SpeakerTracks(" in repr(first)
    assert "Alice" in repr(first)
