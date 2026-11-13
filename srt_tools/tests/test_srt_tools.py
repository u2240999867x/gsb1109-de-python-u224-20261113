#!/usr/bin/env python

import json
import os
import subprocess
import sys
import tempfile

try:
    from shlex import quote
except ImportError:  # <3.3 fallback
    from pipes import quote


import srt

sample_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "files")

PARAGRAPHED_SRT = """\
1
00:00:01,000 --> 00:00:02,000
ALICE:
你好。

BOB:

2
00:00:03,000 --> 00:00:04,000
ALICE:
再见。

慢走。
"""


if os.name == "nt":
    # Sigh, shlex.quote quotes incorrectly on Windows
    quote = lambda x: windows_crappy_quote(x)


def run_srt_util(cmd, shell=False, encoding="utf-8-sig"):
    extra_env = {}

    env = {"PYTHONPATH": ".", "SystemRoot": r"C:\Windows"}
    env.update(extra_env)

    raw_out = subprocess.check_output(cmd, shell=shell, env=env)
    return raw_out.decode(encoding)


def windows_crappy_quote(data):
    """
    I'm 100% sure this isn't secure, please don't use it with untrusted code. I
    beg you.
    """
    data = data.replace('"', '""')
    return '"' + data + '"'


def assert_supports_all_io_methods(cmd, exclude_output=False, exclude_stdin=False):
    # TODO: pytype doesn't like the mixed types in the matrix, but this works
    # fine. Maybe it would be happier with a namedtuple?
    cmd[0] = "srt_tools/" + cmd[0]  # pytype: disable=unsupported-operands
    cmd.insert(0, sys.executable)  # pytype: disable=attribute-error
    in_file = os.path.join(sample_dir, "ascii.srt")
    in_file_gb = os.path.join(sample_dir, "gb2312.srt")
    fd, out_file = tempfile.mkstemp()

    # This is accessed by filename, not fd
    os.close(fd)

    outputs = []
    cmd_string = " ".join(quote(x) for x in cmd)

    try:
        outputs.append(run_srt_util(cmd + ["-i", in_file]))
        if not exclude_stdin:
            outputs.append(
                run_srt_util("%s < %s" % (cmd_string, quote(in_file)), shell=True)
            )
        if not exclude_output:
            run_srt_util(cmd + ["-i", in_file, "-o", out_file])
            run_srt_util(
                cmd + ["-i", in_file_gb, "-o", out_file, "-e", "gb2312"],
                encoding="gb2312",
            )
            if not exclude_stdin:
                run_srt_util(
                    "%s < %s > %s" % (cmd_string, quote(in_file), quote(out_file)),
                    shell=True,
                )
                run_srt_util(
                    "%s < %s > %s"
                    % (cmd_string + " -e gb2312", quote(in_file), quote(out_file)),
                    shell=True,
                    encoding="gb2312",
                )
        assert len(set(outputs)) == 1, repr(outputs)

        if os.name == "nt":
            assert "\r\n" in outputs[0]
        else:
            assert "\r\n" not in outputs[0]
    finally:
        os.remove(out_file)


def test_tools_support():
    matrix = [
        (["srt-normalise"], False),
        (["srt-deduplicate"], False),
        (["srt-fixed-timeshift", "--seconds", "5"], False),
        (
            [
                "srt-linear-timeshift",
                "--f1",
                "00:00:01,000",
                "--f2",
                "00:00:02,000",
                "--t1",
                "00:00:03,000",
                "--t2",
                "00:00:04,000",
            ],
            False,
        ),
        (["srt-lines-matching", "-f", "lambda x: True"], False),
        (["srt-process", "-f", "lambda x: x"], False),
        (["srt-mux"], False, True),
        (["srt-mux", "-t"], False, True),
        # Need to sort out time/thread issues
        # (('srt-play'), True),
    ]

    for args in matrix:
        assert_supports_all_io_methods(*args)


def run_paragraphed(extra_args, stdin=None):
    cmd = [sys.executable, "srt_tools/srt-paragraphed"] + extra_args
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    return subprocess.run(
        cmd,
        input=stdin,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_temp(content, suffix=".srt"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as temp_f:
        temp_f.write(content.encode("utf-8"))
    return path


def test_paragraphed_export_then_restore_roundtrip():
    input_path = write_temp(PARAGRAPHED_SRT)
    strict_path = write_temp("")
    manifest_path = write_temp("", suffix=".json")

    try:
        export = run_paragraphed(
            [
                "export",
                "-i",
                input_path,
                "-o",
                strict_path,
                "--manifest",
                manifest_path,
            ]
        )
        assert export.returncode == 0, export.stderr

        with open(strict_path, encoding="utf-8") as strict_f:
            strict_text = strict_f.read()
        with open(manifest_path, encoding="utf-8") as manifest_f:
            manifest = json.load(manifest_f)

        assert all("\n\n" not in sub.content for sub in srt.parse(strict_text))
        assert manifest["version"] == srt.MANIFEST_VERSION
        assert manifest["records"]

        restore = run_paragraphed(
            [
                "restore",
                "-i",
                strict_path,
                "-o",
                "-",
                "--manifest",
                manifest_path,
            ]
        )
        assert restore.returncode == 0, restore.stderr

        restored = restore.stdout.decode("utf-8")
        assert [sub.content for sub in srt.parse(restored)] == [
            sub.content for sub in srt.parse(PARAGRAPHED_SRT)
        ]
    finally:
        for path in (input_path, strict_path, manifest_path):
            os.remove(path)


def test_paragraphed_restore_keeps_unrelated_edits():
    input_path = write_temp(PARAGRAPHED_SRT)
    strict_path = write_temp("")
    manifest_path = write_temp("", suffix=".json")

    try:
        assert run_paragraphed(
            [
                "export",
                "-i",
                input_path,
                "-o",
                strict_path,
                "--manifest",
                manifest_path,
            ]
        ).returncode == 0

        with open(strict_path, encoding="utf-8") as strict_f:
            edited = strict_f.read().replace("你好。", "你好啊。")
        edited_path = write_temp(edited)

        restore = run_paragraphed(
            [
                "restore",
                "-i",
                edited_path,
                "-o",
                "-",
                "--manifest",
                manifest_path,
                "--on-conflict",
                "skip",
            ]
        )
        assert restore.returncode == 0, restore.stderr
        restored_subs = list(srt.parse(restore.stdout.decode("utf-8")))
        assert "你好啊。" in restored_subs[0].content
        assert "\n\n" in restored_subs[0].content
    finally:
        for path in (input_path, strict_path, manifest_path):
            os.remove(path)
        os.remove(edited_path)


def test_paragraphed_restore_raise_exits_with_hard_conflict_code():
    input_path = write_temp(PARAGRAPHED_SRT)
    strict_path = write_temp("")
    manifest_path = write_temp("", suffix=".json")

    try:
        assert run_paragraphed(
            [
                "export",
                "-i",
                input_path,
                "-o",
                strict_path,
                "--manifest",
                manifest_path,
            ]
        ).returncode == 0

        with open(strict_path, encoding="utf-8") as strict_f:
            broken = strict_f.read().replace("BOB:", "完全不同的一行", 1)
        broken_path = write_temp(broken)

        restore = run_paragraphed(
            [
                "restore",
                "-i",
                broken_path,
                "-o",
                "-",
                "--manifest",
                manifest_path,
            ]
        )
        assert restore.returncode == 4
        assert b"Cannot restore paragraph boundary" in restore.stderr
    finally:
        for path in (input_path, strict_path, manifest_path, broken_path):
            os.remove(path)


def test_paragraphed_restore_skip_exits_with_conflict_code_and_kept_output():
    input_path = write_temp(PARAGRAPHED_SRT)
    strict_path = write_temp("")
    manifest_path = write_temp("", suffix=".json")
    output_path = write_temp("")

    try:
        assert run_paragraphed(
            [
                "export",
                "-i",
                input_path,
                "-o",
                strict_path,
                "--manifest",
                manifest_path,
            ]
        ).returncode == 0

        with open(strict_path, encoding="utf-8") as strict_f:
            broken = strict_f.read().replace("BOB:", "完全不同的一行", 1)
        broken_path = write_temp(broken)

        restore = run_paragraphed(
            [
                "restore",
                "-i",
                broken_path,
                "-o",
                output_path,
                "--manifest",
                manifest_path,
                "--on-conflict",
                "skip",
            ]
        )
        assert restore.returncode == 3

        with open(output_path, encoding="utf-8") as output_f:
            restored_subs = list(srt.parse(output_f.read()))
        assert "\n\n" not in restored_subs[0].content
        assert restored_subs[1].content == "ALICE:\n再见。\n\n慢走。"
    finally:
        for path in (
            input_path,
            strict_path,
            manifest_path,
            output_path,
            broken_path,
        ):
            os.remove(path)


def test_paragraphed_export_supports_stdin_stdout():
    manifest_path = write_temp("", suffix=".json")
    try:
        export = run_paragraphed(
            ["export", "-i", "-", "-o", "-", "--manifest", manifest_path],
            stdin=PARAGRAPHED_SRT.encode("utf-8"),
        )
        assert export.returncode == 0, export.stderr
        strict_text = export.stdout.decode("utf-8")
        assert all("\n\n" not in sub.content for sub in srt.parse(strict_text))
        with open(manifest_path, encoding="utf-8") as manifest_f:
            assert json.load(manifest_f)["records"]
    finally:
        os.remove(manifest_path)
