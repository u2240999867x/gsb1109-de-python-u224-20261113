import os

import numpy as np
import pytest

from lasio import read
from lasio.exceptions import WrappedDataError
from lasio.reader import StringIO

test_dir = os.path.dirname(__file__)


def egfn(fn):
    return os.path.join(test_dir, "examples", fn)


def stegfn(vers, fn):
    return os.path.join(test_dir, "examples", vers, fn)


WRAPPED_HEADER = """~Version
 VERS.   2.0:
 WRAP.   YES:
~Well
 STRT.M   {start}:
 STOP.M   {stop}:
 STEP.M   {step}:
 NULL.   -999.25:
~Curve
 DEPT.M:
 A.M:
 B.M:
 C.M:
~ASCII
"""


def make_wrapped(depths, rows):
    body = "".join(
        "{}\n{} {}\n{}\n".format(depth, row[0], row[1], row[2])
        for depth, row in zip(depths, rows)
    )
    step = depths[1] - depths[0]
    return (
        WRAPPED_HEADER.format(start=depths[0], stop=depths[-1], step=step) + body
    )


DEPTHS = [10.0, 20.0, 30.0, 40.0, 50.0]
ROWS = [
    [1.0, 2.0, 3.0],
    [4.0, 5.0, 6.0],
    [7.0, 8.0, 9.0],
    [10.0, 11.0, 12.0],
    [13.0, 14.0, 15.0],
]
EXPECTED = np.array([[depth] + row for depth, row in zip(DEPTHS, ROWS)])


def drop_line(text, predicate):
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if predicate(line):
            return "".join(lines[:index] + lines[index + 1:])
    raise RuntimeError("line not found")


def test_healthy_wrapped_file_needs_no_repair():
    las = read(stegfn("1.2", "sample_wrapped.las"), repair_wrapped_data=True)
    reference = read(stegfn("1.2", "sample_wrapped.las"))
    assert las.wrapped_data_repairs.activated is False
    assert np.allclose(
        las.data.astype(float),
        reference.data.astype(float),
        equal_nan=True,
    )


def test_missing_cell_strict_raises():
    las_text = make_wrapped(DEPTHS, ROWS).replace("7.0 8.0", "7.0", 1)
    with pytest.raises(WrappedDataError):
        read(StringIO(las_text))


def test_missing_cell_is_retained_and_realigned():
    las_text = make_wrapped(DEPTHS, ROWS).replace("7.0 8.0", "7.0", 1)
    las = read(StringIO(las_text), repair_wrapped_data=True)

    report = las.wrapped_data_repairs
    assert report.activated is True
    assert len(report) == 1
    frame = report.frames[0]
    assert frame["frame_index"] == 2
    assert frame["kind"] == "padded_missing_cells"
    assert frame["missing_cells"] == 1
    assert frame["depth"] == 30.0

    assert np.array_equal(las.index, np.array(DEPTHS))
    assert np.allclose(las.data[[0, 1, 3, 4]], EXPECTED[[0, 1, 3, 4]])
    assert las.data[2, 0] == 30.0
    assert np.isnan(las.data[2, 1:].astype(float)).all()


def test_fixture_missing_cell_reports_raw_line_range():
    las = read(egfn("wrapped_missing_cell.las"), repair_wrapped_data=True)
    assert las.wrapped_data_repairs.raw_line_range == (73, 77)
    reference = read(stegfn("1.2", "sample_wrapped.las"))
    assert np.array_equal(las.index, reference.index)
    assert np.allclose(
        las.data[[0, 1, 3, 4]].astype(float),
        reference.data[[0, 1, 3, 4]].astype(float),
        equal_nan=True,
    )
    assert np.isnan(las.data[2, 1:].astype(float)).all()


def test_missing_depth_anchor_is_lossless():
    las_text = drop_line(
        make_wrapped(DEPTHS, ROWS), lambda line: line.strip() == "30.0"
    )
    with pytest.raises(WrappedDataError):
        read(StringIO(las_text))
    las = read(StringIO(las_text), repair_wrapped_data=True)
    assert las.wrapped_data_repairs.frames[0]["kind"] == "missing_depth_anchor"
    assert np.allclose(las.data.astype(float), EXPECTED)


def test_fixture_missing_depth_anchor_is_lossless():
    reference = read(stegfn("1.2", "sample_wrapped.las"))
    las = read(egfn("wrapped_missing_depth.las"), repair_wrapped_data=True)
    assert las.wrapped_data_repairs.raw_line_range == (72, 76)
    assert np.allclose(
        las.data.astype(float),
        reference.data.astype(float),
        equal_nan=True,
    )


def test_deleted_frame_is_inserted_as_gap():
    las_text = drop_line(
        make_wrapped(DEPTHS, ROWS), lambda line: line.strip() == "30.0"
    )
    # remove the two value continuation lines of the same depth step too
    lines = las_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == "7.0 8.0":
            start = index
            break
    gap_text = "".join(lines[:start] + lines[start + 2:])

    with pytest.raises(WrappedDataError):
        read(StringIO(gap_text))
    las = read(StringIO(gap_text), repair_wrapped_data=True)

    kinds = [frame["kind"] for frame in las.wrapped_data_repairs.frames]
    assert kinds == ["inserted_gap"]
    assert np.array_equal(las.index, np.array(DEPTHS))
    assert las.data[2, 0] == 30.0
    assert np.isnan(las.data[2, 1:].astype(float)).all()
    assert np.allclose(las.data[[0, 1, 3, 4]], EXPECTED[[0, 1, 3, 4]])


def test_ambiguous_realignment_is_not_guessed():
    las_text = make_wrapped(DEPTHS, ROWS).replace("7.0 8.0", "7.0", 1)
    with pytest.raises(WrappedDataError):
        read(
            StringIO(las_text),
            repair_wrapped_data=True,
            wrapped_step_tolerance=100.0,
        )


def test_truncated_final_frame_is_retained():
    lines = make_wrapped(DEPTHS, ROWS).splitlines(keepends=True)
    truncated = "".join(lines[:-1])
    las = read(StringIO(truncated), repair_wrapped_data=True)
    frame = las.wrapped_data_repairs.frames[0]
    assert frame["kind"] == "short_final_frame"
    assert len(las.index) == len(DEPTHS)
    assert las.index[-1] == 50.0
    assert np.isnan(las.data[-1, 1:].astype(float)).all()
    assert np.allclose(las.data[:4], EXPECTED[:4])


def test_step_tolerance_is_respected():
    noisy = (
        WRAPPED_HEADER.format(start=10.0, stop=40.002, step=10.0)
        + "10.001\n1 2\n20.001\n3 4\n30.002\n5 6\n40.002\n7 8\n"
    )
    damaged = noisy.replace("5 6\n", "5\n", 1)
    with pytest.raises(WrappedDataError):
        read(
            StringIO(damaged),
            repair_wrapped_data=True,
            wrapped_step_tolerance=1e-6,
        )
    las = read(StringIO(damaged), repair_wrapped_data=True)
    assert len(las.index) == 4


def test_positive_step_direction():
    depths = [100.0, 101.0, 102.0, 103.0]
    rows = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0], [10.0, 11.0, 12.0]]
    las_text = make_wrapped(depths, rows).replace("7.0 8.0", "7.0", 1)
    las = read(StringIO(las_text), repair_wrapped_data=True)
    assert np.array_equal(las.index, np.array(depths))
    assert las.wrapped_data_repairs.frames[0]["kind"] == "padded_missing_cells"


def test_empty_final_frame_at_end_of_section():
    three_column_header = WRAPPED_HEADER.replace(" C.M:\n", "")
    las_text = (
        three_column_header.format(start=10.0, stop=30.0, step=10.0)
        + "10.0\n1 2\n20.0\n3 4\n30.0\n"
    )
    with pytest.raises(WrappedDataError):
        read(StringIO(las_text))
    las = read(StringIO(las_text), repair_wrapped_data=True)
    frame = las.wrapped_data_repairs.frames[-1]
    assert frame["kind"] == "short_final_frame"
    assert frame["missing_cells"] == 2
    assert las.index[-1] == 30.0
    assert np.isnan(las.data[-1, 1:].astype(float)).all()
    assert np.allclose(las.data[:2], [[10.0, 1.0, 2.0], [20.0, 3.0, 4.0]])
