#!/usr/bin/env python
# coding=utf8

from __future__ import unicode_literals

from datetime import timedelta
import json

import pytest

import srt
import srt_align


def make_subs(texts, starts=None, durations=None, first_index=1):
    """Build subtitles from text, with simple consecutive timing by default."""
    if starts is None:
        starts = [idx * 2000 for idx in range(len(texts))]
    if durations is None:
        durations = [1000] * len(texts)

    subs = []
    for offset, (text, start, duration) in enumerate(
        zip(texts, starts, durations)
    ):
        subs.append(
            srt.Subtitle(
                index=first_index + offset,
                start=timedelta(milliseconds=start),
                end=timedelta(milliseconds=start + duration),
                content=text,
            )
        )
    return subs


def texts(rows, attr="source"):
    return [getattr(row, attr) for row in rows]


def test_one_to_many_grouping_and_missing_translation():
    source = make_subs(
        [
            "第一句话。",
            "第二句话，比较长。",
            "第三句。",
            "第四句。",
        ],
        starts=[0, 2000, 4000, 6000],
        durations=[2000, 2000, 2000, 2000],
    )
    translation = make_subs(
        [
            "First sentence.",
            "Really?",
            "Second sentence, which is long.",
            "Yes.",
            "Third sentence.",
            "Extra line.",
            "Fourth sentence.",
        ],
        starts=[0, 1000, 2000, 3000, 4000, 4500, 5000],
        durations=[1000, 1000, 1000, 1000, 500, 500, 3000],
    )

    alignment = srt_align.align_subtitles(source, translation)

    assert len(alignment) == 4
    assert texts(alignment) == [
        "第一句话。",
        "第二句话，比较长。",
        "第三句。",
        "第四句。",
    ]
    # A single Chinese subtitle can own two consecutive English subtitles.
    assert alignment[0].translation == "First sentence.\nReally?"
    assert alignment[1].translation == (
        "Second sentence, which is long.\nYes."
    )
    assert alignment[2].translation == "Third sentence.\nExtra line."
    assert alignment[3].translation == "Fourth sentence."
    assert all(row.status == srt_align.ROW_ALIGNED for row in alignment)
    assert all(row.status != srt_align.ROW_CANDIDATE for row in alignment)


def test_missing_translation_row_is_kept():
    source = make_subs(
        ["甲。", "乙。"], starts=[0, 3000], durations=[1000, 1000]
    )
    translation = make_subs(["A."], starts=[0], durations=[1000])

    alignment = srt_align.align_subtitles(source, translation)

    assert alignment[-1].status == srt_align.ROW_SOURCE_ONLY
    assert alignment[-1].source == "乙。"
    assert alignment[-1].translation == ""


def test_missing_source_row_is_kept():
    source = make_subs(["甲。", "乙。"], starts=[0, 2000], durations=[1000, 1000])
    translation = make_subs(
        ["A.", "Unrelated insert.", "B."],
        starts=[0, 1000, 2000],
        durations=[1000, 1000, 1000],
    )

    alignment = srt_align.align_subtitles(source, translation)

    assert any(row.status == srt_align.ROW_TRANSLATION_ONLY for row in alignment)
    translation_only = [
        row for row in alignment if row.status == srt_align.ROW_TRANSLATION_ONLY
    ]
    assert translation_only[0].translation == "Unrelated insert."
    assert translation_only[0].source == ""


def test_anchors_are_hard_boundaries():
    source = make_subs(
        ["一。", "二。", "三。"],
        starts=[0, 2000, 4000],
        durations=[2000, 2000, 2000],
    )
    translation = make_subs(
        [
            "One.",
            "Extra.",
            "Uno.",
            "Two.",
            "Trois.",
        ],
        starts=[0, 900, 1000, 2000, 4000],
        durations=[900, 100, 1000, 2000, 2000],
    )

    # Timing alone would like to attach "Extra." to the first Chinese
    # subtitle, but the anchor says "一。" pairs with "One." only.
    alignment = srt_align.align_subtitles(
        source, translation, anchors=[(("position", 1), ("position", 1))]
    )

    assert alignment[0].source == "一。"
    assert alignment[0].translation == "One."
    # Nothing from before the anchor can be pulled across it into a later row.
    for row in alignment[1:]:
        assert "One." not in row.translation
    assert "Extra." not in alignment[0].translation


def test_anchor_can_use_srt_index_and_timestamp():
    source = make_subs(["一。", "二。"], starts=[0, 2000])
    translation = make_subs(["One.", "Two."], starts=[0, 2000])

    by_index = srt_align.align_subtitles(
        source, translation, anchors=[(1, 1)]
    )
    assert by_index.anchors[0][0].content == "一。"
    assert by_index.anchors[0][1].content == "One."

    by_time = srt_align.align_subtitles(
        source,
        translation,
        anchors=[
            (("time", timedelta(0)), ("time", timedelta(0)))
        ],
    )
    assert by_time.anchors[0][0].content == "一。"


@pytest.mark.parametrize("spec", ["1 1", "1,1", "1:1", "#1 #1"])
def test_parse_anchor_forms(spec):
    assert srt_align.parse_anchor(spec) == (("index", 1), ("index", 1))


def test_parse_position_anchor_form():
    assert srt_align.parse_anchor("@1 @1") == (
        ("position", 1),
        ("position", 1),
    )


def test_parse_anchor_timestamp():
    assert srt_align.parse_anchor("00:00:01,000\t00:00:02,500") == (
        ("time", timedelta(seconds=1)),
        ("time", timedelta(milliseconds=2500)),
    )


def test_ambiguous_span_becomes_candidate_and_rest_is_delivered():
    source = make_subs(
        ["甲句一。", "甲句二。"], starts=[0, 2000], durations=[2000, 2000]
    )
    translation = make_subs(
        ["One.", "Two.", "Three."], starts=[0, 1000, 3000],
        durations=[1000, 2000, 1000],
    )

    alignment = srt_align.align_subtitles(source, translation)

    assert len(alignment.candidates) == 1
    candidate = alignment.candidates[0]
    assert candidate.alternatives

    chosen_splits = [
        (row.source, row.translation) for row in candidate.chosen.rows
    ]
    alternative_splits = [
        (row.source, row.translation)
        for row in candidate.alternatives[0].rows
    ]
    assert chosen_splits != alternative_splits

    # The chosen layout is still present in the table, but flagged so the
    # editor knows to check it.
    candidate_rows = [
        row for row in alignment if row.status == srt_align.ROW_CANDIDATE
    ]
    assert candidate_rows
    for row in candidate_rows:
        assert row.candidates

    # Both source and translation content are fully represented.
    assert "".join(row.source for row in alignment) == "甲句一。甲句二。"
    assert sorted(
        text
        for row in alignment
        for text in row.translation.split("\n")
        if text
    ) == ["One.", "Three.", "Two."]


def test_candidate_options_include_distinct_splits():
    source = make_subs(
        ["上句。", "下句。"], starts=[0, 2000], durations=[2000, 2000]
    )
    translation = make_subs(
        ["First half.", "Second half.", "Tail."],
        starts=[0, 1000, 3000],
        durations=[1000, 2000, 1000],
    )

    alignment = srt_align.align_subtitles(source, translation)
    assert alignment.candidates
    splits = {
        tuple(
            (row.start, row.end) for row in option.rows
        )
        for candidate in alignment.candidates
        for option in [candidate.chosen] + list(candidate.alternatives)
    }
    assert len(splits) > 1


def test_no_anchors_means_one_global_alignment():
    source = make_subs(["一。", "二。", "三。"], starts=[0, 2000, 4000])
    translation = make_subs(["One.", "Two.", "Three."], starts=[0, 2000, 4000])
    alignment = srt_align.align_subtitles(source, translation)
    assert [row.translation for row in alignment] == ["One.", "Two.", "Three."]
    assert alignment.anchors == []


def test_invalid_anchor_ordering_raises():
    source = make_subs(["一。", "二。"], starts=[0, 2000])
    translation = make_subs(["One.", "Two."], starts=[0, 2000])
    with pytest.raises(srt_align.AlignmentError):
        srt_align.align_subtitles(
            source,
            translation,
            anchors=[
                (("position", 1), ("position", 2)),
                (("position", 2), ("position", 1)),
            ],
        )


def test_duplicate_anchor_source_raises():
    source = make_subs(["一。"], starts=[0])
    translation = make_subs(["One."], starts=[0])
    with pytest.raises(srt_align.AlignmentError):
        srt_align.align_subtitles(
            source,
            translation,
            anchors=[
                (("position", 1), ("position", 1)),
                (("position", 1), ("position", 1)),
            ],
        )


def test_out_of_range_anchor_raises():
    source = make_subs(["一。"], starts=[0])
    translation = make_subs(["One."], starts=[0])
    with pytest.raises(srt_align.AlignmentError):
        srt_align.align_subtitles(
            source, translation, anchors=[(("position", 5), ("position", 1))]
        )


def test_empty_inputs():
    assert len(srt_align.align_subtitles([], [])) == 0
    source = make_subs(["一。"], starts=[0])
    alignment = srt_align.align_subtitles(source, [])
    assert len(alignment) == 1
    assert alignment[0].status == srt_align.ROW_SOURCE_ONLY


def test_render_tsv_and_json():
    source = make_subs(["一。", "二。"], starts=[0, 2000])
    translation = make_subs(["One.", "Two."], starts=[0, 2000])
    alignment = srt_align.align_subtitles(source, translation)

    tsv = alignment.render("tsv")
    assert "source\ttranslation" in tsv
    assert "一。\tOne." in tsv

    json_text = alignment.render("json")
    payload = json.loads(json_text)
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["source"] == "一。"


def test_render_csv_markdown_and_html():
    source = make_subs(["一。"], starts=[0])
    translation = make_subs(["One."], starts=[0])
    alignment = srt_align.align_subtitles(source, translation)

    assert "一。,One." in alignment.render("csv")
    assert "| 一。 | One. |" in alignment.render("markdown")
    assert "<td>一。</td>" in alignment.render("html")


def test_unsupported_format_raises():
    source = make_subs(["一。"], starts=[0])
    translation = make_subs(["One."], starts=[0])
    alignment = srt_align.align_subtitles(source, translation)
    with pytest.raises(srt_align.AlignmentError):
        alignment.render("xml")


def test_alignment_rows_are_indexed_from_one():
    source = make_subs(["一。", "二。"], starts=[0, 2000])
    translation = make_subs(["One.", "Two."], starts=[0, 2000])
    alignment = srt_align.align_subtitles(source, translation)
    assert [row.index for row in alignment] == [1, 2]


def test_align_srt_parses_text():
    source_text = (
        "1\n00:00:00,000 --> 00:00:02,000\n第一句话。\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二句。\n"
    )
    translation_text = (
        "1\n00:00:00,000 --> 00:00:01,000\nFirst sentence.\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nMore.\n\n"
        "3\n00:00:02,000 --> 00:00:04,000\nSecond sentence.\n"
    )
    alignment = srt_align.align_srt(source_text, translation_text)
    assert alignment[0].translation == "First sentence.\nMore."


def test_load_anchors_file(tmp_path):
    anchor_file = tmp_path / "anchors.tsv"
    anchor_file.write_text(
        "source\ttarget\n"
        "# manually confirmed\n"
        "1\t2\n"
        "\n"
        "3\t4\n", encoding="utf-8"
    )
    anchors = srt_align.load_anchors(str(anchor_file))
    assert anchors == [
        (("index", 1), ("index", 2)),
        (("index", 3), ("index", 4)),
    ]


def test_anchor_reference_tuple_wrong_shape_raises():
    source = make_subs(["一。"], starts=[0])
    translation = make_subs(["One."], starts=[0])
    with pytest.raises(srt_align.AlignmentError):
        srt_align.align_subtitles(
            source,
            translation,
            anchors=[
                (("wat", 1), ("index", 1)),
            ],
        )


def test_candidate_rows_cover_only_the_ambiguous_span():
    source = make_subs(
        ["前句。", "甲句一。", "甲句二。", "后句。"],
        starts=[0, 1000, 3000, 5000],
        durations=[1000, 2000, 2000, 1000],
    )
    translation = make_subs(
        [
            "Before.",
            "One.",
            "Two.",
            "Three.",
            "After.",
        ],
        starts=[0, 1000, 2500, 3500, 5000],
        durations=[1000, 1500, 1000, 1500, 1000],
    )

    alignment = srt_align.align_subtitles(source, translation)
    assert len(alignment.candidates) == 1
    candidate_rows = [
        row for row in alignment if row.status == srt_align.ROW_CANDIDATE
    ]
    assert [row.source for row in candidate_rows] == ["甲句一。", "甲句二。"]
    assert alignment[0].status == srt_align.ROW_ALIGNED
    assert alignment[-1].status == srt_align.ROW_ALIGNED


def test_multiple_independent_ambiguous_spans_are_reported():
    source = make_subs(
        ["甲一。", "甲二。", "乙一。", "乙二。"],
        starts=[0, 2000, 4000, 6000],
        durations=[2000, 2000, 2000, 2000],
    )
    translation = make_subs(
        ["A1.", "A2.", "A3.", "B1.", "B2.", "B3."],
        starts=[0, 1000, 3000, 4000, 5000, 7000],
        durations=[1000, 2000, 1000, 1000, 2000, 1000],
    )
    alignment = srt_align.align_subtitles(source, translation)
    assert len(alignment.candidates) == 2


def test_prefixed_anchor_references():
    assert srt_align.parse_reference("index:3") == ("index", 3)
    assert srt_align.parse_reference("position:3") == ("position", 3)
    assert srt_align.parse_reference("time:00:00:03,000") == (
        "time",
        timedelta(seconds=3),
    )


def test_anchor_references_may_be_strings():
    source = make_subs(["一。", "二。"], starts=[0, 2000])
    translation = make_subs(["One.", "Two."], starts=[0, 2000])
    alignment = srt_align.align_subtitles(
        source,
        translation,
        anchors=[("index:1", "index:2")],
    )
    assert alignment.anchors[0][0].content == "一。"
    assert alignment.anchors[0][1].content == "Two."
