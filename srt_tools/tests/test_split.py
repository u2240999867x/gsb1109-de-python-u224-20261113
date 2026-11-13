#!/usr/bin/env python

"""Tests for the corpus splitter."""

import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

import split as corpus


def make_manifest(segments, **kwargs):
    data = {"segments": segments}
    data.update(kwargs)
    return corpus.Manifest.from_dict(data)


def test_parse_time_accepts_srt_and_milliseconds():
    assert corpus.parse_time_ms("00:00:01,500") == 1500
    assert corpus.parse_time_ms("00:01:00.000") == 60000
    assert corpus.parse_time_ms("00：00：02，250") == 2250
    assert corpus.parse_time_ms("1:02") == 62000
    assert corpus.parse_time_ms(90) == 90
    assert corpus.parse_time_ms("250") == 250
    with pytest.raises(corpus.SplitError):
        corpus.parse_time_ms("00:61:00,000")


def test_identical_source_is_indivisible_and_balanced():
    manifest = make_manifest(
        [
            {"id": "original", "source": "broadcast", "duration_ms": 400},
            {"id": "repeat", "source": "rebroadcast", "duration_ms": 100},
            {"id": "other", "duration_ms": 100},
        ],
        source_correspondences=[["broadcast", "rebroadcast"]],
        ratio=[4, 1],
    )
    result = corpus.split(manifest)
    assert result.ok
    assert result.sets[corpus.TRAIN] == ["original", "repeat"]
    assert result.sets[corpus.VERIFICATION] == ["other"]
    assert result.durations == {corpus.TRAIN: 500, corpus.VERIFICATION: 100}
    assert result.ratio_actual == [0.833333, 0.166667]


def test_must_link_is_transitive():
    manifest = make_manifest(
        [
            {"id": "a", "duration_ms": 10},
            {"id": "b", "duration_ms": 10},
            {"id": "c", "duration_ms": 10},
            {"id": "d", "duration_ms": 10},
        ],
        must_link=[["a", "b"], ["b", "c"]],
        ratio=[3, 1],
    )
    result = corpus.split(manifest)
    sets = result.sets
    for name in corpus.SET_NAMES:
        members = set(sets[name])
        assert set(["a", "b", "c"]) <= members or set(["a", "b", "c"]).isdisjoint(
            members
        )
    assert result.ok


def test_must_link_group_with_more_than_two_members():
    manifest = make_manifest(
        [
            {"id": "a", "duration_ms": 10},
            {"id": "b", "duration_ms": 10},
            {"id": "c", "duration_ms": 10},
            {"id": "d", "duration_ms": 10},
        ],
        must_link=[["a", "b", "c"]],
        ratio=[3, 1],
    )
    result = corpus.split(manifest)
    assert set(["a", "b", "c"]) <= set(result.sets[corpus.TRAIN])


def test_must_link_lock_conflict_names_the_edge():
    manifest = make_manifest(
        [
            {"id": "a", "duration_ms": 10},
            {"id": "b", "duration_ms": 10},
        ],
        must_link=[["a", "b"]],
        locks=[
            {"segments": ["a"], "set": "train"},
            {"segments": ["b"], "set": "verification"},
        ],
    )
    result = corpus.split(manifest)
    assert not result.ok
    message = result.conflict_chains[0]["message"]
    assert "explicit must-link" in message
    assert message.startswith("a [locked train]")
    assert message.endswith("b [locked verification]")


def test_title_merges_same_program_by_default():
    manifest = make_manifest(
        [
            {"id": "a", "title": "Nightline", "duration_ms": 300},
            {"id": "b", "title": "Nightline", "duration_ms": 200},
            {"id": "c", "title": "Other", "duration_ms": 200},
        ],
        ratio=[1, 1],
    )
    result = corpus.split(manifest)
    assert result.sets[corpus.TRAIN] == ["a", "b"]
    assert result.sets[corpus.VERIFICATION] == ["c"]


def test_independent_same_name_is_not_merged():
    manifest = make_manifest(
        [
            {"id": "a", "title": "60 Minutes", "duration_ms": 300},
            {"id": "b", "title": "60 Minutes", "duration_ms": 100},
            {"id": "c", "title": "60 Minutes", "duration_ms": 100},
        ],
        independent=[["a", "b"]],
        ratio=[1, 1],
    )
    result = corpus.split(manifest)
    assert result.ok
    groups = result.as_dict()["groups"]
    # No same-title merging at all once two of the programmes are declared
    # independent: title identity is transitive and could otherwise join a to
    # b through c.
    assert len(groups) == 3
    assert any("independent" in warning for warning in result.warnings)


def test_independent_pair_must_not_be_must_linked():
    with pytest.raises(corpus.SplitError, match="also must-linked"):
        make_manifest(
            [{"id": "a", "duration_ms": 1}, {"id": "b", "duration_ms": 1}],
            must_link=[["a", "b"]],
            independent=[["a", "b"]],
        )


def test_lock_conflict_reports_chain_and_keeps_all_segments():
    manifest = make_manifest(
        [
            {"id": "a", "source": "broadcast", "duration_ms": 10},
            {"id": "b", "source": "rebroadcast", "duration_ms": 10},
            {"id": "c", "duration_ms": 10},
        ],
        source_correspondences=[["broadcast", "rebroadcast"]],
        locks=[
            {"segments": ["a"], "set": "train"},
            {"segments": ["b"], "set": "verification"},
        ],
    )
    result = corpus.split(manifest)
    assert not result.ok
    assert result.sets == {corpus.TRAIN: [], corpus.VERIFICATION: []}
    assert ("a", "b") in result.unassigned
    # The unrelated segment is also left unassigned: emitting a partial split
    # could silently hide the conflict.
    assert ("c",) in result.unassigned
    assert result.durations == {corpus.TRAIN: 0, corpus.VERIFICATION: 0}

    chain = result.conflict_chains[0]
    assert chain["locks"][0]["set"] == "train"
    assert chain["locks"][1]["set"] == "verification"
    assert "a [locked train]" in chain["message"]
    assert "b [locked verification]" in chain["message"]
    assert "confirmed source correspondence" in chain["message"]
    assert "c" not in chain["component"]
    assert result.warnings[-1] == "conflicting locks; no split was produced"


def test_transitive_conflict_chain_details():
    manifest = make_manifest(
        [
            {"id": "a", "source": "s1", "duration_ms": 10},
            {"id": "b", "source": "s2", "duration_ms": 10},
            {"id": "c", "source": "s3", "duration_ms": 10},
        ],
        source_correspondences=[["s1", "s2"], ["s2", "s3"]],
        locks=[
            {"source": "s1", "set": "train"},
            {"source": "s3", "set": "verification"},
        ],
    )
    result = corpus.split(manifest)
    assert not result.ok
    chain = result.conflict_chains[0]
    message = chain["message"]
    assert "source s1" in message
    assert "source s3" in message
    assert "a [locked train]" in message
    assert "c [locked verification]" in message
    assert len(chain["steps"]) == 4
    assert chain["path"] == [
        "a [locked train]",
        "source s1",
        "source s2",
        "source s3",
        "c [locked verification]",
    ]


def test_lock_that_names_several_segments_is_a_group_but_not_a_crossing_chain():
    manifest = make_manifest(
        [
            {"id": "a", "duration_ms": 10},
            {"id": "b", "duration_ms": 10},
            {"id": "c", "duration_ms": 10},
        ],
        locks=[{"segments": ["a", "b"], "set": "train"}],
        ratio=[1, 2],
    )
    result = corpus.split(manifest)
    assert result.ok
    assert set(result.sets[corpus.TRAIN]) == {"a", "b"}
    assert result.sets[corpus.VERIFICATION] == ["c"]
    assert result.as_dict()["groups"][0]["locked"] is True


def test_conflict_between_group_lock_and_source_link():
    manifest = make_manifest(
        [
            {"id": "a", "source": "x", "duration_ms": 10},
            {"id": "b", "source": "y", "duration_ms": 10},
            {"id": "c", "source": "z", "duration_ms": 10},
        ],
        source_correspondences=[["y", "z"]],
        locks=[
            {"segments": ["a", "b"], "set": "train"},
            {"source": "z", "set": "verification"},
        ],
    )
    result = corpus.split(manifest)
    assert not result.ok
    assert "#" in result.conflict_chains[0]["message"] or "c [locked" in (
        result.conflict_chains[0]["message"]
    )
    assert set(result.conflict_chains[0]["component"]) == {"a", "b", "c"}


def test_budget_is_respected_when_possible():
    manifest = make_manifest(
        [{"id": str(index), "duration_ms": 100} for index in range(4)],
        ratio=[1, 1],
        budgets_ms={"train": 200, "verification": 200},
    )
    result = corpus.split(manifest)
    assert result.ok
    assert result.durations[corpus.TRAIN] == 200
    assert result.durations[corpus.VERIFICATION] == 200
    assert result.budget_feasible


def test_impossible_budget_reports_overage_without_dropping():
    manifest = make_manifest(
        [
            {"id": "a", "duration_ms": 100, "set": "train"},
            {"id": "b", "duration_ms": 100},
        ],
        budgets_ms={"train": 50},
    )
    result = corpus.split(manifest)
    assert result.ok
    assert not result.budget_feasible
    assert result.sets[corpus.TRAIN] == ["a"]
    assert result.sets[corpus.VERIFICATION] == ["b"]
    assert any("exceeds its 50 ms budget" in warning for warning in result.warnings)


def test_known_duration_beats_count_balancing():
    manifest = make_manifest(
        [
            {"id": "long", "duration_ms": 900},
            {"id": "short1", "duration_ms": 50},
            {"id": "short2", "duration_ms": 50},
        ],
        ratio=[9, 1],
    )
    result = corpus.split(manifest)
    assert result.durations[corpus.TRAIN] == 900
    assert result.durations[corpus.VERIFICATION] == 100
    assert result.within_tolerance


def test_short_subtitles_are_never_used_as_ballast():
    manifest = make_manifest(
        [
            {"id": "a", "duration_ms": 500},
            {"id": "b", "duration_ms": 400},
            {"id": "tiny", "duration_ms": 1},
        ],
        ratio=[1, 1],
        tolerance_ms=1000,
    )
    result = corpus.split(manifest)
    # Even though dropping ``tiny`` would get closer to 1:1, every segment
    # must appear exactly once.
    assigned = result.sets[corpus.TRAIN] + result.sets[corpus.VERIFICATION]
    assert sorted(assigned) == ["a", "b", "tiny"]
    assert set(assigned).isdisjoint(result.unassigned)


def test_ratio_direction_and_zero_sides():
    manifest = make_manifest(
        [{"id": str(index), "duration_ms": 100} for index in range(5)],
        ratio=[1, 4],
    )
    result = corpus.split(manifest)
    assert result.durations[corpus.TRAIN] == 100
    assert result.durations[corpus.VERIFICATION] == 400

    manifest = make_manifest([{"id": "a", "duration_ms": 10}], ratio=[0, 1])
    result = corpus.split(manifest)
    assert result.sets[corpus.TRAIN] == []
    assert result.sets[corpus.VERIFICATION] == ["a"]


def test_unknown_durations_are_placed_by_count():
    manifest = make_manifest(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        ratio=[2, 1],
    )
    result = corpus.split(manifest)
    assert result.ok
    assert len(result.sets[corpus.TRAIN]) == 2
    assert len(result.sets[corpus.VERIFICATION]) == 1
    assert any("no known speaking durations" in w for w in result.warnings)


def test_srt_file_duration_is_used():
    srt_text = (
        "1\n"
        "00:00:01,000 --> 00:00:03,000\n"
        "hello\n\n"
        "2\n"
        "00:00:05,000 --> 00:00:06,500\n"
        "world\n\n"
    )
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".srt", delete=False, encoding="utf-8"
    )
    try:
        handle.write(srt_text)
        handle.close()
        path = handle.name
        manifest = make_manifest(
            [
                {"id": "a", "path": path},
                {"id": "b", "duration_ms": 2500},
            ],
            ratio=[1, 1],
        )
        result = corpus.split(manifest)
        assert result.durations[corpus.TRAIN] == 3500
        assert result.durations[corpus.VERIFICATION] == 2500
    finally:
        os.unlink(handle.name)


def test_per_segment_budget_is_used_as_cap():
    manifest = make_manifest(
        [
            {"id": "a", "start": "00:00:00,000", "end": "00:01:00,000"},
            {"id": "b", "start": "00:00:00,000", "end": "00:00:10,000"},
            {"id": "c", "duration_ms": 10000},
            {"id": "d", "budget_ms": 10000},
            {"id": "e", "duration_ms": 40000, "budget_ms": 20000},
        ],
    )
    assert manifest.segments["a"].duration_ms == 60000
    assert manifest.segments["d"].duration_ms == 10000
    assert manifest.segments["e"].duration_ms == 20000


def test_duplicate_ids_and_unknown_links_are_errors():
    with pytest.raises(corpus.SplitError, match="duplicate segment"):
        make_manifest(
            [
                {"id": "a", "duration_ms": 1},
                {"id": "a", "duration_ms": 2},
            ]
        )
    with pytest.raises(corpus.SplitError, match="unknown id"):
        make_manifest(
            [{"id": "a", "duration_ms": 1}],
            must_link=[["a", "missing"]],
        )


def test_invalid_manifest_shapes_are_errors():
    with pytest.raises(corpus.SplitError):
        corpus.Manifest.from_dict({})
    with pytest.raises(corpus.SplitError):
        corpus.Manifest.from_dict({"segments": []})
    with pytest.raises(corpus.SplitError):
        corpus.Manifest.from_dict(
            {"segments": [{"id": "a"}], "ratio": [0, 0]}
        )


def test_cli_json_and_text_output():
    manifest = {
        "ratio": [1, 1],
        "segments": [
            {"id": "a", "duration_ms": 100},
            {"id": "b", "duration_ms": 100},
        ],
    }
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(manifest, handle)
        handle.close()
        env = dict(os.environ)
        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        env["PYTHONPATH"] = repo_root
        module_path = os.path.join(repo_root, "srt_tools", "split.py")
        output = subprocess.check_output(
            [sys.executable, module_path, handle.name], env=env
        )
        payload = json.loads(output.decode("utf-8"))
        assert payload["ok"] is True
        assert payload["sets"]["train"] == ["a"]
        assert payload["sets"]["verification"] == ["b"]

        output = subprocess.check_output(
            [sys.executable, module_path, "--text", handle.name], env=env
        )
        text = output.decode("utf-8")
        assert "train: 1 groups, 100 ms" in text
    finally:
        os.unlink(handle.name)


def test_cli_conflict_exit_code_and_chain():
    manifest = {
        "segments": [
            {"id": "a", "duration_ms": 100},
            {"id": "b", "duration_ms": 100},
        ],
        "must_link": [["a", "b"]],
        "locks": [
            {"segments": ["a"], "set": "train"},
            {"segments": ["b"], "set": "verification"},
        ],
    }
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(manifest, handle)
        handle.close()
        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        module_path = os.path.join(repo_root, "srt_tools", "split.py")
        process = subprocess.Popen(
            [sys.executable, module_path, handle.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, PYTHONPATH=repo_root),
        )
        stdout, _ = process.communicate()
        assert process.returncode == 1
        payload = json.loads(stdout.decode("utf-8"))
        assert payload["ok"] is False
        chain = payload["conflict_chains"][0]
        assert chain["locks"][0]["set"] == "train"
        assert chain["locks"][1]["set"] == "verification"
        assert payload["unassigned"] == [["a", "b"]]
    finally:
        os.unlink(handle.name)


def test_cli_check_mode_and_overrides():
    manifest = {
        "segments": [
            {"id": "a", "duration_ms": 100},
            {"id": "b", "duration_ms": 300},
        ],
    }
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(manifest, handle)
        handle.close()
        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        module_path = os.path.join(repo_root, "srt_tools", "split.py")
        env = dict(os.environ, PYTHONPATH=repo_root)
        output = subprocess.check_output(
            [
                sys.executable,
                module_path,
                "--check",
                "--ratio",
                "3:1",
                "--budget",
                "train=500",
                handle.name,
            ],
            env=env,
        )
        assert output.decode("utf-8").strip() == "manifest ok"
    finally:
        os.unlink(handle.name)
