#!/usr/bin/env python
# coding=utf8

from __future__ import unicode_literals

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
CLI = os.path.join(WORKSPACE, "srt_tools", "srt-align")

SOURCE_TEXT = """\
1
00:00:00,000 --> 00:00:02,000
第一句话。

2
00:00:02,000 --> 00:00:04,000
第二句。

3
00:00:04,000 --> 00:00:06,000
第三句。
"""

TRANSLATION_TEXT = """\
1
00:00:00,000 --> 00:00:01,000
First sentence.

2
00:00:01,000 --> 00:00:02,000
Really?

3
00:00:02,000 --> 00:00:04,000
Second sentence.

4
00:00:04,000 --> 00:00:06,000
Third sentence.
"""


def run_cli(args, stdin=None, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, CLI] + args,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=True,
    )


@pytest.fixture
def subtitle_files(tmp_path):
    source = tmp_path / "source.srt"
    translation = tmp_path / "translation.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    translation.write_text(TRANSLATION_TEXT, encoding="utf-8")
    return str(source), str(translation)


def test_cli_tsv_output(subtitle_files):
    source, translation = subtitle_files
    result = run_cli(["-i", source, "-i", translation])
    out = result.stdout.decode("utf-8")

    lines = out.splitlines()
    assert lines[0] == "index\tstart\tend\tsource\ttranslation\tstatus\t" \
        "candidates"
    assert "第一句话。" in out
    assert "First sentence." in out
    assert "Really?" in out
    assert "untranslated" not in out


def test_cli_csv_and_no_header(subtitle_files):
    source, translation = subtitle_files
    result = run_cli(
        ["-i", source, "-i", translation, "-f", "csv", "--no-header"]
    )
    out = result.stdout.decode("utf-8")
    assert out.splitlines()[0].startswith("1,")
    assert "第一句话。" in out


def test_cli_json_has_candidate_section(subtitle_files):
    source, translation = subtitle_files
    result = run_cli(["-i", source, "-i", translation, "-f", "json"])
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["rows"]
    assert "candidates" in payload
    assert payload["rows"][0]["source"]


def test_cli_stdin_and_output_file(subtitle_files, tmp_path):
    source, translation = subtitle_files
    with open(source, "rb") as source_file:
        result = run_cli(
            ["-i", "-", "-i", translation], stdin=source_file.read()
        )
    assert "第一句话。".encode("utf-8") in result.stdout

    out_path = tmp_path / "out.tsv"
    run_cli(["-i", source, "-i", translation, "-o", str(out_path)])
    assert "第一句话。" in out_path.read_text(encoding="utf-8")


def test_cli_anchors_file(subtitle_files, tmp_path):
    source, translation = subtitle_files
    anchors = tmp_path / "anchors.tsv"
    anchors.write_text("source\ttarget\n1\t1\n", encoding="utf-8")

    result = run_cli(
        ["-i", source, "-i", translation, "-a", str(anchors)]
    )
    out = result.stdout.decode("utf-8")
    rows = [line.split("\t") for line in out.splitlines()[1:]]
    assert rows[0][3] == "第一句话。"
    assert rows[0][4] == "First sentence."


def test_cli_json_anchors(subtitle_files, tmp_path):
    source, translation = subtitle_files
    anchors = tmp_path / "anchors.json"
    anchors.write_text(
        json.dumps([["#1", "#1"]]), encoding="utf-8"
    )
    result = run_cli(
        ["-i", source, "-i", translation, "-a", str(anchors)]
    )
    assert "第一句话。".encode("utf-8") in result.stdout


def test_cli_requires_two_inputs():
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE
    result = subprocess.run(
        [sys.executable, CLI, "-i", "-"],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert result.returncode != 0
    assert b"exactly two inputs" in result.stderr


def test_cli_invalid_anchor_raises(subtitle_files, tmp_path):
    source, translation = subtitle_files
    anchors = tmp_path / "bad.tsv"
    anchors.write_text("1\t99\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE
    result = subprocess.run(
        [sys.executable, CLI, "-i", source, "-i", translation,
         "-a", str(anchors)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert result.returncode != 0
    assert b"not found" in result.stderr or b"outside" in result.stderr
