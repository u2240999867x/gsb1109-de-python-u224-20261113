import json
import math

import numpy as np
import pytest

from lasio.bed_compatibility import (
    BedCompatibilityError,
    Observation,
    WindowSpec,
    check_bed_compatibility,
    main,
    observations_from_las,
)

BEDS = [
    (100.0, 104.5, "A"),
    (104.5, 105.5, "B"),
    (105.5, 110.0, "C"),
]
TRUE_VALUES = {"A": 10.0, "B": 50.0, "C": 10.0}

THIN = WindowSpec.symmetric("thin", half_height=0.5, tolerance=0.5)
THICK = WindowSpec.symmetric("thick", half_height=2.0, tolerance=0.5)
WINDOWS = {"thin": THIN, "thick": THICK}


def weighted_observation(probe, depth, spec, values):
    top, bottom = spec.span(depth)
    total = 0.0
    thickness = bottom - top
    for bed_top, bed_bottom, bed_id in BEDS:
        overlap = min(bed_bottom, bottom) - max(bed_top, top)
        if overlap > 0:
            total += overlap * values[bed_id]
    return Observation(probe, depth, total / thickness, spec.tolerance)


def compatible_observations():
    observations = []
    for depth in (102.0, 105.0, 108.0):
        observations.append(
            weighted_observation("thin", depth, THIN, TRUE_VALUES)
        )
    for depth in (102.0, 105.0, 108.0):
        observations.append(
            weighted_observation("thick", depth, THICK, TRUE_VALUES)
        )
    return observations


def test_window_weights_are_overlap_thicknesses():
    report = check_bed_compatibility(
        BEDS, WINDOWS, compatible_observations(), value_bounds=(0.0, 100.0)
    )
    row = next(
        row for row in report.coverage
        if row.observation == "thick@105"
    )
    assert row.status == "valid"
    assert row.weights == pytest.approx({"A": 1.5, "B": 1.0, "C": 1.5})
    assert row.supported_thickness == pytest.approx(4.0)


def test_narrow_peak_and_flat_wide_log_are_compatible():
    report = check_bed_compatibility(
        BEDS, WINDOWS, compatible_observations(), value_bounds=(0.0, 100.0)
    )
    assert report.feasible
    assert report.missing == []
    assert report.out_of_bounds == []
    assert report.conflicts == []

    values = report.bed_values
    for bed_id, true_value in TRUE_VALUES.items():
        low, high = report.bed_value_ranges[bed_id]
        assert low - 1e-6 <= true_value <= high + 1e-6
        assert low - 1e-6 <= values[bed_id] <= high + 1e-6

    for prediction in report.predictions:
        assert prediction["within_tolerance"]
        assert abs(prediction["residual"]) <= prediction["tolerance"] + 1e-9

    peak = next(
        prediction
        for prediction in report.predictions
        if prediction["observation"] == "thin@105"
    )
    wide = next(
        prediction
        for prediction in report.predictions
        if prediction["observation"] == "thick@105"
    )
    assert peak["observed"] == 50.0
    assert wide["observed"] == 20.0
    assert peak["predicted"] == pytest.approx(50.0, abs=0.5)
    assert wide["predicted"] == pytest.approx(20.0, abs=0.5)


def test_bed_value_ranges_span_the_feasible_set():
    report = check_bed_compatibility(
        BEDS, WINDOWS, compatible_observations(), value_bounds=(0.0, 100.0)
    )
    low_b, high_b = report.bed_value_ranges["B"]
    assert low_b == pytest.approx(49.5, abs=1e-6)
    assert high_b == pytest.approx(50.5, abs=1e-6)
    low_a, high_a = report.bed_value_ranges["A"]
    assert low_a == pytest.approx(9.5, abs=1e-6)
    assert high_a == pytest.approx(10.5, abs=1e-6)


def test_conflicting_observations_are_flagged_not_resampled():
    observations = compatible_observations()
    observations.append(
        Observation("thick", 105.0, 10.0, 0.5)
    )
    report = check_bed_compatibility(
        BEDS,
        WINDOWS,
        observations,
        value_bounds=(0.0, 100.0),
        compute_ranges=False,
    )
    assert not report.feasible
    assert report.bed_values is None
    assert report.predictions == []
    conflict_ids = {entry["observation"] for entry in report.conflicts}
    assert conflict_ids == {"thick@105", "thick@105#2"}
    conflict_values = [
        observation
        for observation in observations
        if observation.probe == "thick" and observation.depth == 105.0
    ]
    assert {observation.value for observation in conflict_values} == {
        20.0,
        10.0,
    }


def test_missing_samples_are_listed_separately():
    observations = compatible_observations()
    observations.append(Observation("thin", 103.0, None))
    observations.append(Observation("thin", 107.0, float("nan")))
    report = check_bed_compatibility(
        BEDS, WINDOWS, observations, value_bounds=(0.0, 100.0)
    )
    assert report.feasible
    assert {entry["observation"] for entry in report.missing} == {
        "thin@103",
        "thin@107",
    }


def test_window_crossing_known_bed_boundary_is_excluded_then_clipped():
    observations = compatible_observations()
    observations.append(
        weighted_observation("thin", 100.1, THIN, TRUE_VALUES)
    )
    strict = check_bed_compatibility(
        BEDS, WINDOWS, observations, value_bounds=(0.0, 100.0)
    )
    assert strict.feasible
    assert len(strict.out_of_bounds) == 1
    record = strict.out_of_bounds[0]
    assert record["observation"] == "thin@100.1"
    assert record["window_top"] == pytest.approx(99.6)
    assert record["supported_thickness"] == pytest.approx(0.6)
    assert record["window_thickness"] == pytest.approx(1.0)
    assert all(
        row.observation != "thin@100.1"
        for row in strict.coverage
        if row.status == "valid"
    )

    clipped = check_bed_compatibility(
        BEDS,
        WINDOWS,
        observations,
        value_bounds=(0.0, 100.0),
        strict_support=False,
    )
    assert clipped.out_of_bounds == []
    row = next(
        row for row in clipped.coverage
        if row.observation == "thin@100.1"
    )
    assert row.status == "valid"
    assert row.weights == pytest.approx({"A": 0.6})


def test_per_observation_tolerance_overrides_window_tolerance():
    observations = [
        Observation("thin", 105.0, 50.0, tolerance=0.0),
        Observation("thin", 102.0, 10.0, tolerance=0.0),
        Observation("thin", 108.0, 10.0, tolerance=0.0),
        Observation("thick", 102.0, 10.0, tolerance=0.0),
        Observation("thick", 108.0, 10.0, tolerance=0.0),
        Observation("thick", 105.0, 20.0, tolerance=0.0),
    ]
    report = check_bed_compatibility(
        BEDS, WINDOWS, observations, value_bounds=(0.0, 100.0)
    )
    assert report.feasible
    assert report.bed_values["B"] == pytest.approx(50.0)


def test_invalid_geometry_is_rejected():
    with pytest.raises(BedCompatibilityError):
        WindowSpec("bad", top_offset=0.5, bottom_offset=1.0, tolerance=0.1)
    with pytest.raises(BedCompatibilityError):
        check_bed_compatibility(
            [(100.0, 99.0, "A")], WINDOWS, compatible_observations()
        )
    with pytest.raises(BedCompatibilityError):
        check_bed_compatibility(
            [(100.0, 104.0, "A"), (105.0, 110.0, "B")],
            WINDOWS,
            compatible_observations(),
        )


def test_unknown_probe_is_rejected():
    with pytest.raises(BedCompatibilityError):
        check_bed_compatibility(
            BEDS,
            WINDOWS,
            [Observation("mystery", 105.0, 1.0)],
        )


def test_observations_from_las_detects_null_values():
    lasio = pytest.importorskip("lasio")
    log = lasio.LASFile()
    depths = np.arange(102.0, 109.0, 1.0)
    log.append_curve("DEPT", depths)
    log.append_curve("thin", np.full_like(depths, 12.5))
    log.append_curve("thick", np.full_like(depths, 11.0))
    log["thin"][2] = np.nan

    grouped = observations_from_las(log, WINDOWS)
    assert set(grouped) == {"thin", "thick"}
    missing = [
        observation
        for observation in grouped["thin"]
        if observation.value is None
    ]
    assert len(missing) == 1
    assert missing[0].depth == 104.0
    assert all(
        isinstance(observation.value, float)
        for observation in grouped["thick"]
    )


def test_cli_main_round_trip(tmp_path, capsys):

    config = {
        "beds": [list(bed) for bed in BEDS],
        "probes": {
            "thin": {"half_height": 0.5, "tolerance": 0.5},
            "thick": {"half_height": 2.0, "tolerance": 0.5},
        },
        "observations": [
            {
                "probe": observation.probe,
                "depth": observation.depth,
                "value": observation.value,
            }
            for observation in compatible_observations()
        ],
        "value_bounds": [0.0, 100.0],
    }
    config_path = tmp_path / "case.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    exit_code = main([str(config_path)])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["feasible"] is True
    assert len(payload["predictions"]) == 6
    assert math.isfinite(payload["bed_values"]["B"])


def test_cli_main_reports_infeasible_exit_code(tmp_path):
    observations = compatible_observations()
    observations.append(Observation("thick", 105.0, 10.0, 0.5))
    config = {
        "beds": [list(bed) for bed in BEDS],
        "probes": {
            "thin": {"half_height": 0.5, "tolerance": 0.5},
            "thick": {"half_height": 2.0, "tolerance": 0.5},
        },
        "observations": [
            {
                "probe": observation.probe,
                "depth": observation.depth,
                "value": observation.value,
            }
            for observation in observations
        ],
        "value_bounds": [0.0, 100.0],
    }
    config_path = tmp_path / "infeasible.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert main([str(config_path)]) == 1


def test_unbounded_ranges_serialise_as_null():
    observations = [
        Observation("thin", 105.0, 50.0, 0.0),
        Observation("thick", 105.0, 20.0, 0.0),
    ]
    report = check_bed_compatibility(
        BEDS, WINDOWS, observations, compute_ranges=True
    )
    assert report.feasible
    payload = json.loads(report.to_json())
    assert payload["bed_value_ranges"]["A"] == [None, None]
    assert payload["bed_value_ranges"]["C"] == [None, None]
    finite_bed = payload["bed_value_ranges"]["B"]
    assert all(math.isfinite(value) for value in finite_bed)


def test_directly_contradictory_observation_pair_is_infeasible():
    observations = [
        Observation("thin", 105.0, 50.0, 0.1),
        Observation("thin", 105.0, 12.0, 0.1),
    ]
    report = check_bed_compatibility(
        BEDS,
        WINDOWS,
        observations,
        value_bounds=(0.0, 100.0),
        compute_ranges=False,
    )
    assert report.status == "infeasible"
    assert not report.feasible
    assert {
        entry["observation"] for entry in report.conflicts
    } == {"thin@105", "thin@105#2"}


def test_screening_out_everything_is_indeterminate_not_infeasible():
    observations = [
        Observation("thin", 103.0, None),
        Observation("thin", 100.1, 10.0, 0.5),
    ]
    report = check_bed_compatibility(BEDS, WINDOWS, observations)
    assert report.status == "indeterminate"
    assert not report.feasible
    assert report.conflicts == []
    assert len(report.missing) == 1
    assert len(report.out_of_bounds) == 1
