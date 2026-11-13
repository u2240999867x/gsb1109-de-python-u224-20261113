#!/usr/bin/env python

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader

import srt

HERE = os.path.dirname(os.path.realpath(__file__))
SCRIPT = os.path.join(HERE, "..", "srt-diff")
EXAMPLE_DIR = os.path.join(HERE, "..", "..", "examples", "srt-diff")


def _load_diff_module():
    loader = SourceFileLoader("srt_diff_tool", SCRIPT)
    spec = importlib.util.spec_from_loader("srt_diff_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


diff = _load_diff_module()


def sub(index, start_seconds, end_seconds, content):
    return srt.Subtitle(
        index,
        dt.timedelta(seconds=start_seconds),
        dt.timedelta(seconds=end_seconds),
        content,
    )


def totals(subs_a, subs_b, **kwargs):
    normalize = diff.make_normalizer(kwargs.get("case_sensitive", False))
    return diff.summarize(
        diff.compare_subtitles(subs_a, subs_b, normalize)
    )


def seconds(delta):
    return delta.total_seconds()


def test_boundary_move_is_not_a_text_change():
    old = [sub(1, 0, 10, "one two three")]
    new = [sub(1, 0, 4, "one two"), sub(2, 4, 10, "three")]

    result = totals(old, new)

    assert seconds(result[diff.EQUAL]) == 10.0
    assert seconds(result["total_different"]) == 0.0


def test_line_wrap_and_whitespace_are_not_text_changes():
    old = [sub(1, 0, 5, "one two")]
    new = [sub(1, 0, 5, "one  two\n")]

    result = totals(old, new)

    assert seconds(result[diff.EQUAL]) == 5.0
    assert seconds(result["total_different"]) == 0.0


def test_repeated_sentence_is_counted_once_on_timeline():
    old = [sub(1, 0, 6, "repeat me"), sub(2, 6, 12, "repeat me")]
    new = [sub(1, 0, 12, "repeat me")]

    normalize = diff.make_normalizer(False)
    intervals = diff.compare_subtitles(old, new, normalize)

    assert len(intervals) == 1
    assert intervals[0].kind == diff.EQUAL
    assert seconds(intervals[0].end - intervals[0].start) == 12.0
    assert intervals[0].count_a == 1
    assert intervals[0].count_b == 1


def test_text_change_is_measured_in_seconds():
    old = [sub(1, 0, 5, "quick brown fox")]
    new = [sub(1, 0, 5, "lazy red dog")]

    result = totals(old, new)

    assert seconds(result[diff.DIFFERENT]) == 5.0
    assert seconds(result[diff.EQUAL]) == 0.0


def test_one_sided_intervals_are_directional():
    old = [sub(1, 0, 4, "only old")]
    new = [sub(1, 5, 9, "only new")]

    result = totals(old, new)

    assert seconds(result[diff.ONLY_A]) == 4.0
    assert seconds(result[diff.ONLY_B]) == 4.0
    assert seconds(result[diff.EQUAL]) == 0.0
    assert seconds(result[diff.DIFFERENT]) == 0.0


def test_simultaneous_speaker_count_is_visible():
    old = [sub(1, 0, 10, "main speaker")]
    new = [
        sub(1, 0, 6, "main speaker"),
        sub(2, 4, 10, "second speaker"),
    ]

    normalize = diff.make_normalizer(False)
    intervals = diff.compare_subtitles(old, new, normalize)
    overlap = next(
        interval
        for interval in intervals
        if interval.count_a == 1 and interval.count_b == 2
    )

    assert overlap.kind == diff.DIFFERENT
    assert seconds(overlap.start) == 4.0
    assert seconds(overlap.end) == 6.0


def test_case_sensitive_comparison():
    old = [sub(1, 0, 3, "Hello")]
    new = [sub(1, 0, 3, "hello")]

    assert seconds(totals(old, new)["total_different"]) == 0.0
    assert seconds(
        totals(old, new, case_sensitive=True)[diff.DIFFERENT]
    ) == 3.0


def test_swap_inputs_preserves_totals_and_flips_direction():
    old = [
        sub(1, 0, 4, "same"),
        sub(2, 4, 8, "old only"),
        sub(3, 12, 17, "old wording"),
    ]
    new = [
        sub(1, 0, 4, "same"),
        sub(2, 9, 13, "new only"),
        sub(3, 12, 17, "new wording"),
    ]

    normalize = diff.make_normalizer(False)
    forward = diff.compare_subtitles(old, new, normalize)
    swapped = diff.compare_subtitles(new, old, normalize)
    forward_totals = diff.summarize(forward)
    swapped_totals = diff.summarize(swapped)

    assert seconds(forward_totals[diff.EQUAL]) == seconds(
        swapped_totals[diff.EQUAL]
    )
    assert seconds(forward_totals[diff.DIFFERENT]) == seconds(
        swapped_totals[diff.DIFFERENT]
    )
    assert seconds(forward_totals["total_different"]) == seconds(
        swapped_totals["total_different"]
    )
    assert seconds(forward_totals[diff.ONLY_A]) == seconds(
        swapped_totals[diff.ONLY_B]
    )
    assert seconds(forward_totals[diff.ONLY_B]) == seconds(
        swapped_totals[diff.ONLY_A]
    )

    flip = {diff.ONLY_A: diff.ONLY_B, diff.ONLY_B: diff.ONLY_A}
    for forward_interval, swapped_interval in zip(forward, swapped):
        assert forward_interval.start == swapped_interval.start
        assert forward_interval.end == swapped_interval.end
        assert swapped_interval.kind == flip.get(
            forward_interval.kind, forward_interval.kind
        )
        assert forward_interval.count_a == swapped_interval.count_b
        assert forward_interval.count_b == swapped_interval.count_a
        assert forward_interval.texts_a == swapped_interval.texts_b
        assert forward_interval.texts_b == swapped_interval.texts_a


def _run_cli_json(first, second):
    raw = subprocess.check_output(
        [
            sys.executable,
            SCRIPT,
            "-i",
            first,
            "-i",
            second,
            "--json",
        ],
        env={"PYTHONPATH": os.path.join(HERE, "..", "..")},
    )
    return json.loads(raw.decode("utf-8"))


def test_example_files_match_hand_checked_numbers():
    old = os.path.join(EXAMPLE_DIR, "old.srt")
    new = os.path.join(EXAMPLE_DIR, "new.srt")

    report = _run_cli_json(old, new)
    summary = report["summary_seconds"]

    assert summary["equal"] == 39.0
    assert summary["different"] == 25.0
    assert summary["only_a"] == 4.0
    assert summary["only_b"] == 4.0
    assert summary["total_different"] == 33.0
    assert summary["covered"] == 72.0

    kinds = [interval["kind"] for interval in report["intervals"]]
    assert diff.EQUAL in kinds
    assert diff.DIFFERENT in kinds
    assert diff.ONLY_A in kinds
    assert diff.ONLY_B in kinds

    speaker_change = next(
        interval
        for interval in report["intervals"]
        if interval["speakers_a"] > 0
        and interval["speakers_b"] > 0
        and interval["speakers_a"] != interval["speakers_b"]
    )
    assert (
        speaker_change["speakers_a"],
        speaker_change["speakers_b"],
    ) == (1, 2)

    swapped = _run_cli_json(new, old)
    swapped_summary = swapped["summary_seconds"]

    assert swapped_summary["equal"] == summary["equal"]
    assert swapped_summary["different"] == summary["different"]
    assert swapped_summary["total_different"] == summary["total_different"]
    assert swapped_summary["only_a"] == summary["only_b"]
    assert swapped_summary["only_b"] == summary["only_a"]


def test_cli_accepts_stdin_as_one_input():
    new = os.path.join(EXAMPLE_DIR, "new.srt")
    with open(os.path.join(EXAMPLE_DIR, "old.srt"), "rb") as old_f:
        raw = subprocess.check_output(
            [sys.executable, SCRIPT, "-i", "-", "-i", new, "--json"],
            stdin=old_f,
            env={"PYTHONPATH": os.path.join(HERE, "..", "..")},
        )

    report = json.loads(raw.decode("utf-8"))

    assert report["file_a"] == "<stdin>"
    assert report["summary_seconds"]["total_different"] == 33.0


def test_cli_rejects_wrong_input_count():
    old = os.path.join(EXAMPLE_DIR, "old.srt")
    proc = subprocess.run(
        [sys.executable, SCRIPT, "-i", old],
        env={"PYTHONPATH": os.path.join(HERE, "..", "..")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert proc.returncode != 0
    assert b"exactly two --input files" in proc.stderr
