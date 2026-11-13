import os

import numpy as np
import pytest

import lasio
from lasio import exceptions


def make_las(*columns, null=-999.25):
    las = lasio.LASFile()
    for mnemonic, values in columns:
        las.append_curve(mnemonic, np.asarray(values, dtype=float))
    las.well["NULL"].value = null
    las.well["STRT"].value = float(las.index[0])
    las.well["STOP"].value = float(las.index[-1])
    las.well["STEP"].value = 1.0
    return las


def depth_and_gr(depths, gr=None):
    if gr is None:
        gr = np.arange(len(depths), dtype=float) + 1.0
    return make_las(("DEPT", depths), ("GR", gr))


def test_roundtrip_clean_width_passes():
    las = depth_and_gr([1670.0, 1670.125, 1670.25])
    report = las.verify_roundtrip(
        column_width=10, tolerance=1e-4, identity="DEPT"
    )
    assert report.ok
    assert bool(report) is True
    assert report.total_rounding_collisions == 0
    assert report.source_duplicate_count == 0
    assert report.row_count_preserved
    assert report.precision["DEPT"] == 5


def test_roundtrip_rounding_collision_is_not_source_duplicate():
    las = depth_and_gr([1670.0, 1670.0004, 1670.0008])
    report = las.verify_roundtrip(
        column_width=10, tolerance=0.01, identity="DEPT", fmt="%.3f"
    )
    assert not report.ok
    assert report.source_duplicate_count == 0
    assert report.total_rounding_collisions == 1
    assert len(report.rounding_collisions) == 1
    evidence = report.rounding_collisions[0]
    assert evidence.kind == "rounding_collision"
    assert len(evidence.identities) == 2
    assert evidence.formatted_identities == ((1670.0,), (1670.0,))
    assert evidence.affected_rows == 2


def test_roundtrip_source_duplicate_is_reported_but_does_not_fail():
    las = depth_and_gr([1670.0, 1670.0, 1670.25])
    report = las.verify_roundtrip(
        column_width=10, tolerance=1e-4, identity="DEPT"
    )
    assert report.ok
    assert report.source_duplicate_count == 1
    assert report.total_source_duplicate_rows == 2
    assert report.source_duplicates[0].rows == (0, 1)
    assert report.total_rounding_collisions == 0


def test_roundtrip_mixed_source_duplicate_and_induced_collision():
    las = depth_and_gr([1.0, 1.00004, 1.00004])
    report = las.verify_roundtrip(
        column_width=10,
        tolerance=1e-5,
        identity="DEPT",
        precision={"DEPT": 4},
    )
    assert not report.ok
    assert report.source_duplicate_count == 1
    assert report.total_source_duplicate_rows == 2
    assert report.total_rounding_collisions == 1
    # The collision evidence only lists distinct source identities.
    assert len(report.rounding_collisions[0].identities) == 2
    assert report.rounding_collisions[0].affected_rows == 3


def test_roundtrip_tolerance_violation_reports_sample():
    las = depth_and_gr([1670.0, 1670.001])
    report = las.verify_roundtrip(
        column_width=5, tolerance=1e-6, identity="DEPT"
    )
    assert not report.ok
    assert report.total_tolerance_violations >= 1
    violation = report.tolerance_violations[0]
    assert violation.kind == "tolerance_exceeded"
    assert violation.curve == "DEPT"
    assert violation.row_index is not None
    assert violation.error > 1e-6


def test_roundtrip_width_overflow_is_evidence_not_dropped():
    las = depth_and_gr([1670.0, 1670.5])
    report = las.verify_roundtrip(
        column_width=4, tolerance=10.0, identity="DEPT", fmt="%.3f"
    )
    assert not report.ok
    assert report.total_width_overflows > 0
    assert report.width_overflows[0].kind == "width_overflow"
    assert report.source_row_count == report.formatted_row_count == 2


def test_roundtrip_evidence_is_bounded():
    las = depth_and_gr([1670.0] + list(1670.0 + np.arange(1, 40) * 1e-7))
    report = las.verify_roundtrip(
        column_width=10,
        tolerance=1e-9,
        identity="DEPT",
        fmt="%.3f",
        max_examples=3,
    )
    assert not report.ok
    assert len(report.rounding_collisions) <= 3
    assert report.total_rounding_collisions == 1
    assert len(report.rounding_collisions[0].rows) <= 3
    assert report.rounding_collisions[0].affected_rows == 40
    assert report.source_row_count == 40


def test_roundtrip_does_not_delete_or_modify_rows():
    depths = [1670.0, 1670.0004, 1670.0008]
    las = depth_and_gr(depths)
    before = las.data.copy()
    report = las.verify_roundtrip(
        column_width=10, tolerance=0.01, identity="DEPT", fmt="%.3f"
    )
    assert not report.ok
    assert report.source_row_count == len(depths)
    assert report.formatted_row_count == len(depths)
    assert las.data.shape == before.shape
    assert np.array_equal(las.data, before)


def test_roundtrip_scalar_aliases_and_width_forms():
    las = depth_and_gr([1670.0, 1670.125])
    a = las.verify_roundtrip(width=10, tol=1e-4, sample_identity="DEPT")
    b = las.verify_roundtrip(
        field_width=10, max_error=1e-4, key_columns=["DEPT"]
    )
    c = las.verify_roundtrip(
        column_width={"DEPT": 10, "GR": 10},
        tolerance={"DEPT": 1e-4, "GR": 1e-4},
        identity="DEPT",
    )
    d = las.verify_roundtrip(
        column_width=[10, 10],
        tolerance=[1e-4, 1e-4],
        identity=[0],
    )
    assert a.ok and b.ok and c.ok and d.ok


def test_roundtrip_multicolumn_identity():
    las = lasio.LASFile()
    las.append_curve("WELL", np.array([1.0, 1.0, 2.0, 2.0]))
    las.append_curve("DEPT", np.array([10.0, 10.0, 10.0, 10.0]))
    las.append_curve("GR", np.array([1.0, 1.0, 2.0, 2.0]))
    las.well["NULL"].value = -999.25
    report = las.verify_roundtrip(
        column_width=8, tolerance=1e-6, identity=["WELL", "DEPT"]
    )
    assert report.ok
    assert report.source_duplicate_count == 2
    assert report.total_source_duplicate_rows == 4
    assert report.total_rounding_collisions == 0


def test_roundtrip_nan_identity_rows_are_ignored():
    las = depth_and_gr([1670.0, np.nan, 1670.25])
    report = las.verify_roundtrip(
        column_width=10, tolerance=1e-4, identity="DEPT"
    )
    assert report.ok
    assert report.source_duplicate_count == 0


def test_roundtrip_explicit_format_overrides_precision():
    las = depth_and_gr([1670.0, 1670.0004])
    report = las.verify_roundtrip(
        column_width=20,
        tolerance=1e-6,
        identity="DEPT",
        fmt="%.3f",
    )
    assert not report.ok
    assert report.total_rounding_collisions == 1
    assert report.precision["DEPT"] == 3


def test_roundtrip_raise_on_failure():
    las = depth_and_gr([1670.0, 1670.0004])
    with pytest.raises(exceptions.LASRoundTripError) as excinfo:
        las.verify_roundtrip(
            column_width=10,
            tolerance=1e-6,
            identity="DEPT",
            fmt="%.3f",
            raise_on_failure=True,
        )
    report = excinfo.value.report
    assert not report.ok
    assert report.rounding_collisions


def test_roundtrip_requires_width_and_tolerance():
    las = depth_and_gr([1670.0])
    with pytest.raises(ValueError):
        las.verify_roundtrip(tolerance=1e-4, identity="DEPT")
    with pytest.raises(ValueError):
        las.verify_roundtrip(column_width=10, identity="DEPT")
    with pytest.raises(ValueError):
        las.verify_roundtrip(
            column_width=10, tolerance=1e-4, identity="NOT_A_CURVE"
        )
    with pytest.raises(ValueError):
        las.verify_roundtrip(
            column_width={"DEPT": 10}, tolerance=1e-4, identity="DEPT"
        )
    with pytest.raises(ValueError):
        las.verify_roundtrip(
            column_width=10, tolerance={"DEPT": 1e-4}, identity="DEPT"
        )


def test_roundtrip_rejects_unknown_keyword():
    las = depth_and_gr([1670.0])
    with pytest.raises(TypeError):
        las.verify_roundtrip(
            column_width=10, tolerance=1e-4, identity="DEPT", nonsense=True
        )


def test_roundtrip_non_numeric_curve_checked_for_width_only():
    las = lasio.LASFile()
    las.append_curve("DEPT", np.array([1670.0, 1670.5]))
    las.well["NULL"].value = -999.25
    las.curves.append(lasio.CurveItem("LABEL", unit="", descr="", value=""))
    las.curves["LABEL"].data = np.array(["LONG-LABEL", "B"])
    report = las.verify_roundtrip(
        column_width=5, tolerance=1e-4, identity="DEPT"
    )
    assert not report.ok
    assert report.precision["LABEL"] is None
    assert report.width_overflows[0].curve == "LABEL"


def test_roundtrip_report_serializes():
    las = depth_and_gr([1670.0, 1670.0004])
    report = las.verify_roundtrip(
        column_width=10, tolerance=1e-6, identity="DEPT", fmt="%.3f"
    )
    data = report.to_dict()
    assert data["ok"] is False
    assert data["counts"]["rounding_collisions"] == 1
    assert data["source_duplicates"] == []


def test_write_with_tolerance_runs_check_and_blocks_bad_export(tmp_path):
    las = depth_and_gr([1670.0, 1670.0004])
    path = str(tmp_path / "bad.las")
    with pytest.raises(exceptions.LASRoundTripError):
        las.write(path, tolerance=1e-6, identity="DEPT", fmt="%.3f")
    assert not os.path.exists(path)
    assert las.last_roundtrip_report is not None
    assert not las.last_roundtrip_report.ok


def test_write_with_tolerance_passes_and_returns_report(tmp_path):
    las = depth_and_gr([1670.0, 1670.125])
    path = str(tmp_path / "good.las")
    report = las.write(
        path, tolerance=1e-4, identity="DEPT", len_numeric_field=10
    )
    assert report is not None and report.ok
    assert os.path.exists(path)
    reread = lasio.read(path)
    assert np.allclose(reread.index, las.index)


def test_write_default_behaviour_is_unchanged():
    from lasio.reader import StringIO

    las = depth_and_gr([1670.0, 1670.125])
    s = StringIO()
    assert las.write(s) is None
    assert "1670.00000" in s.getvalue()


def test_write_can_disable_verification():
    from lasio.reader import StringIO

    las = depth_and_gr([1670.0, 1670.0004])
    s = StringIO()
    report = las.write(
        s, tolerance=1e-6, identity="DEPT", fmt="%.3f", verify=False
    )
    assert report is None
    assert s.getvalue()


def test_write_non_raising_returns_failing_report():
    from lasio.reader import StringIO

    las = depth_and_gr([1670.0, 1670.0004])
    s = StringIO()
    report = las.write(
        s,
        tolerance=1e-6,
        identity="DEPT",
        fmt="%.3f",
        raise_on_failure=False,
    )
    assert report is not None and not report.ok
    assert s.getvalue()


def test_write_column_width_kwarg_models_budget():
    from lasio.reader import StringIO

    las = depth_and_gr([1670.0, 1670.001, 1670.002])
    report = las.write(
        StringIO(),
        column_width=6,
        tolerance=1e-6,
        identity="DEPT",
        fmt="%.3f",
        raise_on_failure=False,
    )
    assert not report.ok
    assert report.column_width == 6


def test_verify_export_previews_without_writing():
    las = depth_and_gr([1670.0, 1670.0004])
    report = lasio.verify_export(
        las, tolerance=1e-6, identity="DEPT", fmt="%.3f"
    )
    assert not report.ok
    assert report.column_width == 10
    assert report.total_rounding_collisions == 1


def test_verify_export_respects_explicit_field_width():
    las = depth_and_gr([1670.0, 1670.125])
    report = lasio.verify_export(
        las,
        column_width=12,
        tolerance=1e-4,
        identity="DEPT",
        len_numeric_field=12,
    )
    assert report.ok
    assert report.column_width == 12


def test_write_failure_preserves_existing_file(tmp_path):
    path = tmp_path / "existing.las"
    path.write_text("ORIGINAL")
    las = depth_and_gr([1670.0, 1670.0004])
    with pytest.raises(exceptions.LASRoundTripError):
        las.write(str(path), tolerance=1e-6, identity="DEPT", fmt="%.3f")
    assert path.read_text() == "ORIGINAL"
    assert not list(tmp_path.glob(".lasio-*"))


def test_write_success_replaces_existing_file(tmp_path):
    path = tmp_path / "existing.las"
    path.write_text("ORIGINAL")
    las = depth_and_gr([1670.0, 1670.125])
    report = las.write(str(path), tolerance=1e-4, identity="DEPT")
    assert report.ok
    assert "ORIGINAL" not in path.read_text()
    assert np.allclose(lasio.read(str(path)).index, las.index)


def test_roundtrip_columns_limits_budget_but_keeps_identity():
    las = lasio.LASFile()
    las.append_curve("DEPT", np.array([1670.0, 1670.125]))
    las.append_curve("WIDE", np.array([123456789.0, 987654321.0]))
    las.well["NULL"].value = -999.25
    report = las.verify_roundtrip(
        column_width={"DEPT": 10},
        tolerance={"DEPT": 1e-4},
        identity="DEPT",
        columns=["DEPT"],
    )
    assert report.ok
    assert report.verified_curves == ("DEPT",)
    assert report.precision["DEPT"] == 5


def test_roundtrip_identity_outside_columns_uses_supplied_budget():
    las = lasio.LASFile()
    las.append_curve("DEPT", np.array([1670.0, 1670.0004]))
    las.append_curve("GR", np.array([1.0, 2.0]))
    las.well["NULL"].value = -999.25
    report = las.verify_roundtrip(
        column_width={"GR": 8},
        tolerance={"GR": 1e-4},
        identity="DEPT",
        columns=["GR"],
    )
    assert not report.ok
    assert report.total_rounding_collisions == 1
    assert report.precision["DEPT"] == 3
