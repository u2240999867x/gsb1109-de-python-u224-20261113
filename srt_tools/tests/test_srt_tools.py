#!/usr/bin/env python

import os
import subprocess
import sys
import tempfile

try:
    from shlex import quote
except ImportError:  # <3.3 fallback
    from pipes import quote


sample_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "files")


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


AMBIGUOUS_EXCERPT = (
    "7\n"
    "00:00:10,000 --> 00:00:12,000\n"
    "证人引用了另一份字幕：\n"
    "\n"
    "2\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "这是被引用的原话\n"
    "\n"
    "8\n"
    "00:00:13,000 --> 00:00:15,000\n"
    "后文证词\n"
)


def _write_tmp(content):
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as input_f:
        input_f.write(content)
    return path


def _run_ambiguous_split(cmd, stdin=None):
    full_cmd = [sys.executable, "srt_tools/srt-ambiguous-split"] + cmd
    proc = subprocess.Popen(
        full_cmd,
        env={"PYTHONPATH": ".", "SystemRoot": r"C:\Windows"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    raw_out, _ = proc.communicate(stdin)
    return proc.returncode, raw_out.decode("utf-8-sig")


def test_ambiguous_split_reports_span_without_anchors():
    excerpt_path = _write_tmp(AMBIGUOUS_EXCERPT)
    try:
        returncode, output = _run_ambiguous_split(["-i", excerpt_path])
    finally:
        os.remove(excerpt_path)

    assert returncode == 3
    assert "Still ambiguous" in output
    assert "Shortest ambiguous span" in output
    assert "00:00:01,000 --> 00:00:02,000" in output


def test_ambiguous_split_unique_with_verified_anchors():
    excerpt_path = _write_tmp(AMBIGUOUS_EXCERPT)
    preceding_path = _write_tmp(
        "6\n00:00:08,000 --> 00:00:09,000\n前文\n"
    )
    following_path = _write_tmp(
        "9\n00:00:16,000 --> 00:00:17,000\n再后文\n"
    )
    try:
        returncode, output = _run_ambiguous_split(
            [
                "-i",
                excerpt_path,
                "--preceding",
                preceding_path,
                "--following",
                following_path,
                "--contiguous-indices",
                "yes",
            ]
        )
    finally:
        for path in (excerpt_path, preceding_path, following_path):
            os.remove(path)

    assert returncode == 0
    assert "Unique split found: 2" in output
    # The quoted subtitle survives intact inside block 7.
    assert "这是被引用的原话" in output
    assert "block 2 (index 8)" in output


def test_ambiguous_split_reads_stdin():
    returncode, output = _run_ambiguous_split(
        ["--contiguous-indices", "no"],
        stdin=AMBIGUOUS_EXCERPT.encode("utf-8"),
    )

    assert returncode == 3
    assert "interpretation 1: indices [7, 2]" in output


def test_ambiguous_split_errors_on_unparseable_input():
    bad_path = _write_tmp("not an srt file at all\n")
    try:
        returncode, output = _run_ambiguous_split(["-i", bad_path])
    finally:
        os.remove(bad_path)

    assert returncode == 1
