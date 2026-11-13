import numpy as np
import pytest

import lasio
from lasio.sentinel import (
    AMBIGUOUS,
    MEASURED,
    MISSING,
    CurveSentinelSpec,
    audit_sentinels,
    extract_specs,
)


LAS_TEXT = """~V
 VERS. 2.0 : CWLS LOG ASCII STANDARD
 WRAP. NO : one line per depth step
~W
 STRT.F 1000.0 : START DEPTH
 STOP.F 1004.0 : STOP DEPTH
 STEP.F 1.0 : STEP
 NULL. -99 : NULL VALUE
~C
 DEPT.F : 1 DEPTH
 T_MAIN.F : TEMPERATURE MAIN PROBE {RANGE=-120,120} {BACKUP=T_BACK} {ERROR=2}
 T_OTHER.F : TEMPERATURE OTHER PROBE {RANGE=20,120} {BACKUP=T_OTHER_BACK} {ERROR=2}
 T_BACK.F : BACKUP TEMPERATURE MAIN PROBE {RUN=1}
 T_OTHER_BACK.F : BACKUP TEMPERATURE OTHER PROBE {RUN=2}
~P
 T_MAIN_RUN. 1 : RUN NUMBER
 T_OTHER_RUN. 2 : RUN NUMBER
~A
1000 -99 -99 -99 30
1001 55.0 55.0 55.0 55.0
1002 -99 30 -80 30
1003 70.0 70.0 70.0 70.0
1004 -99 30 -98.5 30
"""


def make_las(**kwargs):
    return lasio.read(LAS_TEXT, null_policy="none", **kwargs)


def test_same_depth_different_curves_different_decisions():
    las = make_las()
    audit = audit_sentinels(las)

    main = [record for record in audit.for_curve("T_MAIN")]
    other = audit.for_curve("T_OTHER")
    assert len(main) == 3
    assert len(other) == 1

    assert main[0].decision == AMBIGUOUS
    assert other[0].decision == MISSING
    assert main[0].depth == other[0].depth == 1000.0


def test_only_excluded_measurements_become_nan():
    las = make_las()
    audit = audit_sentinels(las)

    assert las["T_MAIN"][0] == -99
    assert np.isnan(las["T_MAIN"][2])
    assert las["T_MAIN"][4] == -99
    assert np.isnan(las["T_OTHER"][0])
    assert las["T_OTHER"][1] == 55.0


def test_original_samples_remain_queryable():
    las = make_las()
    audit = audit_sentinels(las)

    original = audit.original_data("T_MAIN")
    assert original[0] == -99
    assert original[2] == -99
    assert np.isnan(original[1]) is np.False_ or not np.isnan(original[1])
    assert [record.raw_value for record in audit.for_curve("T_MAIN")] == [-99.0, -99.0, -99.0]

    records = audit.at_depth(1000.0)
    assert {record.curve for record in records} == {"T_MAIN", "T_OTHER"}
    assert {record.raw_value for record in records} == {-99.0}


def test_ambiguous_keeps_original_doubt_record():
    las = make_las()
    audit = audit_sentinels(las)

    doubts = audit.doubts
    assert len(doubts) == 1
    assert doubts[0].decision == AMBIGUOUS
    assert doubts[0].curve == "T_MAIN"
    assert "real measurement cannot be excluded" in " ".join(doubts[0].reasons)
    assert las["T_MAIN"][0] == doubts[0].raw_value == -99


def test_range_excludes_sentinel_and_no_backup_is_missing():
    las = make_las()
    audit = audit_sentinels(
        las,
        specs=[CurveSentinelSpec(curve="T_OTHER", calibration_range=(20, 120))],
    )
    record = audit.for_curve("T_OTHER")[0]
    assert record.decision == MISSING
    assert record.evidence["range_state"] == "excludes"
    assert record.evidence["backup_state"] == "unavailable"


def test_range_includes_sentinel_without_backup_is_ambiguous():
    las = make_las()
    audit = audit_sentinels(
        las,
        specs=[CurveSentinelSpec(curve="T_MAIN", calibration_range=(-120, 120))],
    )
    record = audit.for_curve("T_MAIN")[0]
    assert record.decision == AMBIGUOUS
    assert record.evidence["range_state"] == "includes"
    assert las["T_MAIN"][0] == -99


def test_backup_within_error_supports_measurement():
    las = make_las()
    audit = audit_sentinels(las)
    record = audit.for_curve("T_MAIN")[2]
    assert record.index == 4
    assert record.decision == MEASURED
    assert record.evidence["backup_state"] == "supports"
    assert las["T_MAIN"][4] == -99


def test_backup_outside_error_excludes_measurement():
    las = make_las()
    audit = audit_sentinels(las)
    record = audit.for_curve("T_MAIN")[1]
    assert record.index == 2
    assert record.decision == MISSING
    assert record.evidence["backup_state"] == "contradicts"
    assert np.isnan(las["T_MAIN"][2])


def test_backup_from_another_run_is_not_used():
    las = make_las()
    audit = audit_sentinels(
        las,
        specs=[
            CurveSentinelSpec(
                curve="T_MAIN",
                calibration_range=(-120, 120),
                backup="T_OTHER_BACK",
                backup_error=100.0,
                run="1",
            )
        ],
    )
    record = audit.for_curve("T_MAIN")[0]
    assert record.evidence["backup_state"] == "run_mismatch"
    assert record.decision == AMBIGUOUS
    assert las["T_MAIN"][0] == -99


def test_backup_at_sentinel_is_no_evidence():
    las = make_las()
    audit = audit_sentinels(las)
    record = audit.for_curve("T_MAIN")[0]
    assert record.evidence["backup_state"] == "sentinel"
    assert record.decision == AMBIGUOUS


def test_conflicting_range_and_backup_is_ambiguous():
    las = make_las()
    audit = audit_sentinels(
        las,
        specs=[
            CurveSentinelSpec(
                curve="T_MAIN",
                calibration_range=(20, 120),
                backup="T_BACK",
                backup_error=2.0,
            )
        ],
    )
    record = audit.for_curve("T_MAIN")[2]
    assert record.decision == AMBIGUOUS
    assert "conflicting evidence" in " ".join(record.reasons)


def test_null_equals_sentinel_restores_ambiguous_values():
    las = lasio.read(LAS_TEXT)
    assert np.isnan(las["T_MAIN"][0])

    audit = las.audit_sentinel_values(-99)
    assert las.sentinel_audit is audit
    assert las["T_MAIN"][0] == -99
    assert np.isnan(las["T_MAIN"][2])
    assert las["T_MAIN"][4] == -99
    assert audit.original_data("T_MAIN")[0] == -99
    assert las.null_originals["T_MAIN"][0] == -99


def test_apply_false_only_reports():
    las = make_las()
    audit = audit_sentinels(las, apply=False)
    assert las["T_OTHER"][0] == -99
    assert not np.isnan(las["T_OTHER"][0])
    assert audit.summary()["replaced"] == 0
    assert audit.resolved_missing


def test_extract_specs_reads_curve_description():
    las = make_las()
    specs = extract_specs(las)
    main = specs["T_MAIN"]
    assert main.calibration_range == (-120.0, 120.0)
    assert main.backup == "T_BACK"
    assert main.backup_error == 2.0
    assert main.declared == "temperature"


def test_extract_specs_reads_parameter_section():
    text = LAS_TEXT.replace(
        " T_MAIN.F : TEMPERATURE MAIN PROBE {RANGE=-120,120} {BACKUP=T_BACK} {ERROR=2}",
        " T_MAIN.F : TEMPERATURE MAIN PROBE",
    )
    las = lasio.read(text, null_policy="none")
    las.params.append(
        lasio.HeaderItem("T_MAIN_CAL", "F", "-120,120", "CALIBRATED RANGE")
    )
    las.params.append(
        lasio.HeaderItem("T_MAIN_BACKUP", "", "T_BACK", "SPARE PROBE")
    )
    las.params.append(
        lasio.HeaderItem("T_MAIN_ERR", "F", "2", "ACCURACY")
    )
    specs = extract_specs(las)
    assert specs["T_MAIN"].calibration_range == (-120.0, 120.0)
    assert specs["T_MAIN"].backup == "T_BACK"
    assert specs["T_MAIN"].backup_error == 2.0


def test_curve_sentinel_spec_object():
    las = make_las()
    audit = audit_sentinels(
        las,
        specs=[
            CurveSentinelSpec(
                curve="T_OTHER",
                calibration_range=(20, 120),
                backup="T_OTHER_BACK",
                backup_error=2,
                run="2",
            )
        ],
    )
    record = audit.for_curve("T_OTHER")[0]
    assert record.decision == MISSING
    assert record.evidence["backup_state"] == "contradicts"


def test_audit_json_serializable():
    las = make_las()
    audit = audit_sentinels(las)
    payload = audit.to_json()
    assert '"decision": "ambiguous"' in payload
    assert '"raw_value": -99.0' in payload


def test_no_sentinel_samples_is_empty():
    text = LAS_TEXT.replace("-99", "10")
    las = lasio.read(text, null_policy="none")
    audit = audit_sentinels(las)
    assert len(audit) == 0
    assert audit.summary()["replaced"] == 0


def test_read_with_sentinel_value_option():
    las = lasio.read(LAS_TEXT, sentinel_value=-99)
    assert las.sentinel_audit is not None
    assert las["T_MAIN"][0] == -99
    assert np.isnan(las["T_MAIN"][2])
    assert np.isnan(las["T_OTHER"][0])
    assert las["T_OTHER"][1] == 55.0


def test_read_with_sentinel_apply_false():
    las = lasio.read(
        LAS_TEXT, null_policy="none", sentinel_value=-99, sentinel_apply=False
    )
    assert las["T_OTHER"][0] == -99
    assert las.sentinel_audit.summary()["replaced"] == 0
    assert las.sentinel_audit.resolved_missing


def test_read_with_sentinel_specs_override():
    las = lasio.read(
        LAS_TEXT,
        null_policy="none",
        sentinel_value=-99,
        sentinel_specs=[
            CurveSentinelSpec(
                curve="T_OTHER",
                calibration_range=(20, 120),
                backup="T_OTHER_BACK",
                backup_error=2,
                run="2",
            )
        ],
    )
    record = las.sentinel_audit.for_curve("T_OTHER")[0]
    assert record.decision == MISSING
    assert np.isnan(las["T_OTHER"][0])


def test_default_files_are_untouched_by_import():
    las = lasio.read("tests/examples/sample.las")
    assert las.sentinel_audit is None
    assert las.null_replacements == {}


def test_write_and_original_audit_remain_queryable(tmp_path):
    las = make_las()
    audit = audit_sentinels(las)
    target = tmp_path / "out.las"
    las.write(str(target))
    text = target.read_text()
    data_lines = [
        line for line in text.splitlines()
        if line.strip() and line.strip()[0].isdigit() and line.strip().startswith("100")
    ]
    assert len(data_lines) == 5
    assert audit.original_data("T_MAIN")[2] == -99
    assert np.isnan(las["T_MAIN"][2])
