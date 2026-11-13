"""Tests for the syncfit timing-model fitting module."""

import itertools
import os
import subprocess
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import pytest
from hypothesis import given, settings, assume, strategies as st

import syncfit


def make_anchors(points, tolerance):
    return [
        syncfit.Anchor(old, remastered, tolerance, label)
        for label, (old, remastered) in enumerate(points)
    ]


def brute_force_vertices(rows, n_vars):
    """Enumerate polyhedron vertices from constraint hyperplanes."""
    vertices = []
    for combo in itertools.combinations(range(len(rows)), n_vars):
        matrix = [rows[i][0] for i in combo]
        rhs = [rows[i][1] for i in combo]
        point = syncfit.solve_linear_system(matrix, rhs)
        if point is None:
            continue
        if all(
            sum(coef * value for coef, value in zip(row, point))
            <= bound + 1e-7
            for row, bound in rows
        ):
            vertices.append(point)
    return vertices


def test_linear_exact_recovery_contains_truth():
    true_slope, true_intercept = 1.04, 1500.0
    anchors = [
        syncfit.Anchor(
            old,
            true_slope * old + true_intercept,
            120.0,
            "anchor-%d" % index,
        )
        for index, old in enumerate(range(60000, 600001, 60000))
    ]
    result = syncfit.solve_linear(anchors, (0.95, 1.10))
    assert result.feasible
    assert result.slope_range[0] <= true_slope <= result.slope_range[1]
    assert (
        result.intercept_range[0]
        <= true_intercept
        <= result.intercept_range[1]
    )
    # Tight tolerance keeps the feasible slope band narrow.
    assert result.slope_range[1] - result.slope_range[0] < 0.01


def test_linear_infeasible_when_speed_range_excludes_truth():
    anchors = [
        syncfit.Anchor(old, 1.2 * old, 50.0)
        for old in range(60000, 360001, 60000)
    ]
    result = syncfit.solve_linear(anchors, (0.97, 1.03))
    assert not result.feasible
    with pytest.raises(ValueError):
        syncfit.prediction_interval(result, 100000.0)


def test_linear_tolerance_band_grows_with_measurement_error():
    anchors = [
        syncfit.Anchor(old, 1.0 * old, 0.0)
        for old in range(60000, 240001, 60000)
    ]
    exact = syncfit.solve_linear(anchors, (0.9, 1.1))
    loose_anchors = [
        syncfit.Anchor(anchor.old, anchor.remastered, 500.0)
        for anchor in anchors
    ]
    loose = syncfit.solve_linear(loose_anchors, (0.9, 1.1))
    assert (
        loose.slope_range[1] - loose.slope_range[0]
        > exact.slope_range[1] - exact.slope_range[0]
    )


def test_piecewise_detects_required_insertion():
    jump_position, jump_duration = 150000.0, 9000.0
    anchors = []
    for index, old in enumerate(range(30000, 300001, 30000)):
        remastered = old + (
            jump_duration if old > jump_position else 0.0
        )
        anchors.append(syncfit.Anchor(old, remastered, 150.0, "L%d" % index))

    linear = syncfit.solve_linear(anchors, (0.97, 1.03))
    assert not linear.feasible

    results = syncfit.solve_piecewise(anchors, (0.97, 1.03), 2)
    required = [result for result in results if result.inserts]
    assert len(required) == 1
    result = required[0]
    position, duration_range = result.inserts[0]
    assert 120000.0 < position < 180000.0
    assert duration_range[0] <= jump_duration <= duration_range[1]
    assert result.slope_range[0] <= 1.0 <= result.slope_range[1]
    # An insertion this large cannot be explained away as measurement noise.
    assert duration_range[0] > 0.0


def test_ambiguous_data_lets_both_explanations_pass():
    jump_position, jump_duration, tolerance = 150000.0, 250.0, 400.0
    anchors = []
    for index, old in enumerate(range(30000, 300001, 30000)):
        remastered = old + (
            jump_duration if old > jump_position else 0.0
        )
        anchors.append(syncfit.Anchor(old, remastered, tolerance, "L%d" % index))

    linear = syncfit.solve_linear(anchors, (0.97, 1.03))
    assert linear.feasible

    alternatives = syncfit.insertion_alternatives(
        anchors, (0.97, 1.03), 2, min_insert_ms=1.0
    )
    assert alternatives
    jump_gap = next(
        result
        for result in alternatives
        if any(jump_position < position < jump_position + 30000.0
               for position, _bounds in result.inserts)
    )

    subtitles = [
        (old, "subtitle-%d" % index)
        for index, old in enumerate(range(30000, 300001, 30000))
    ]
    diverging = list(
        syncfit.diverging_subtitles(linear, jump_gap, subtitles)
    )
    # Only subtitles after the insertion point can be sent elsewhere.
    affected = {row[0] for row in diverging}
    assert "subtitle-0" not in affected
    assert "subtitle-8" in affected
    for label, old_time, linear_interval, piece_interval, extra in diverging:
        assert old_time > jump_position
        assert extra[1] > 1.0
        assert linear_interval is not None


def test_prediction_intervals_bracket_measurements():
    anchors = make_anchors(
        [(60000.0, 63000.0), (120000.0, 127500.0), (180000.0, 192000.0)],
        300.0,
    )
    result = syncfit.solve_linear(anchors, (0.95, 1.10))
    for anchor in anchors:
        low, high = syncfit.prediction_interval(result, anchor.old)
        assert low <= anchor.remastered <= high
        residual = syncfit.anchor_residuals(result)
    for anchor, residual_low, residual_high in residual:
        assert abs(residual_low) <= anchor.tolerance + 1e-6
        assert abs(residual_high) <= anchor.tolerance + 1e-6


def test_simplex_matches_vertex_enumeration():
    rows = [
        ([1.0, 0.0], 2.0),
        ([-1.0, 0.0], 0.0),
        ([0.0, 1.0], 5.0),
        ([0.0, -1.0], 1.0),
        ([1.0, 1.0], 6.0),
        ([-2.0, 1.0], 1.0),
    ]
    vertices = brute_force_vertices(rows, 2)
    assert vertices
    for variable in range(2):
        objective = [0.0, 0.0]
        objective[variable] = 1.0
        low = syncfit.simplex(rows, objective, want_max=False)
        high = syncfit.simplex(rows, objective, want_max=True)
        assert low == pytest.approx(min(v[variable] for v in vertices))
        assert high == pytest.approx(max(v[variable] for v in vertices))


def test_simplex_detects_infeasibility():
    rows = [
        ([1.0], 1.0),
        ([-1.0], -2.0),
    ]
    assert syncfit.simplex(rows, [1.0], want_max=False) is None


def test_simplex_reports_unboundedness():
    # x >= 0 has no upper bound on x, but a finite minimum of zero.
    lower_bounded = [([-1.0], 0.0)]
    assert syncfit.simplex(lower_bounded, [1.0], want_max=True) == float("inf")
    assert syncfit.simplex(lower_bounded, [1.0], want_max=False) == 0.0
    # x <= 1 has no lower bound, so minimisation is unbounded.
    upper_bounded = [([1.0], 1.0)]
    assert syncfit.simplex(upper_bounded, [1.0], want_max=False) == float("-inf")
    assert syncfit.simplex(upper_bounded, [1.0], want_max=True) == 1.0


def test_input_validation():
    good = [
        syncfit.Anchor(1000.0, 2000.0, 10.0),
        syncfit.Anchor(2000.0, 3050.0, 10.0),
    ]
    with pytest.raises(ValueError):
        syncfit.solve_linear(good[:1], (0.9, 1.1))
    with pytest.raises(ValueError):
        syncfit.solve_linear(
            [syncfit.Anchor(1.0, 2.0, -1.0), good[1]], (0.9, 1.1)
        )
    with pytest.raises(ValueError):
        syncfit.solve_linear(
            [
                syncfit.Anchor(1000.0, 2000.0, 10.0),
                syncfit.Anchor(1000.0, 2100.0, 10.0),
            ],
            (0.9, 1.1),
        )
    with pytest.raises(ValueError):
        syncfit.solve_piecewise(good, (0.9, 1.1), -1)


def test_zero_tolerance_gives_point_estimate():
    anchors = [
        syncfit.Anchor(60000.0, 63000.0, 0.0),
        syncfit.Anchor(120000.0, 126000.0, 0.0),
        syncfit.Anchor(180000.0, 189000.0, 0.0),
    ]
    result = syncfit.solve_linear(anchors, (0.9, 1.1))
    assert result.feasible
    assert result.slope_range[0] == pytest.approx(1.05)
    assert result.slope_range[1] == pytest.approx(1.05)
    assert result.intercept_range[0] == pytest.approx(0.0, abs=1e-9)
    assert result.intercept_range[1] == pytest.approx(0.0, abs=1e-9)


def test_insertion_duration_is_non_negative():
    jump_position, jump_duration = 90000.0, 4000.0
    anchors = [
        syncfit.Anchor(
            old,
            old + (jump_duration if old > jump_position else 0.0),
            200.0,
        )
        for old in (30000.0, 60000.0, 120000.0, 180000.0)
    ]
    result = syncfit.solve_structure(
        anchors, (0.98, 1.02), [75000.0]
    )
    assert result is not None
    _position, duration_range = result.inserts[0]
    assert duration_range[0] >= -1e-9


def test_max_inserts_zero_reduces_to_uniform_model():
    anchors = make_anchors(
        [(30000.0, 31000.0), (90000.0, 92500.0), (150000.0, 154000.0)],
        200.0,
    )
    linear = syncfit.solve_linear(anchors, (0.95, 1.08))
    piecewise = syncfit.solve_piecewise(anchors, (0.95, 1.08), 0)
    assert len(piecewise) == 1
    assert piecewise[0].inserts == ()
    assert piecewise[0].slope_range == pytest.approx(linear.slope_range)


def test_insertion_displacement_is_zero_before_jump():
    anchors = [
        syncfit.Anchor(old, old + (3000.0 if old > 100000.0 else 0.0), 100.0)
        for old in (40000.0, 80000.0, 120000.0, 160000.0)
    ]
    result = syncfit.solve_structure(anchors, (0.99, 1.01), [100000.0])
    assert result is not None
    assert syncfit.insertion_displacement(result, 50000.0) == (0.0, 0.0)
    after_low, after_high = syncfit.insertion_displacement(result, 150000.0)
    assert after_low > 0.0 and after_high >= after_low


@st.composite
def feasible_scenarios(draw):
    slope = draw(st.floats(0.9995, 1.0005, allow_nan=False, allow_infinity=False))
    intercept = draw(st.floats(-1000.0, 1000.0))
    tolerance = draw(st.floats(25.0, 200.0))
    n_anchors = draw(st.integers(6, 9))
    spacing = 45000.0
    jitter = draw(
        st.lists(
            st.floats(-4000.0, 4000.0),
            min_size=n_anchors,
            max_size=n_anchors,
        )
    )
    old_times = [
        (index + 1) * spacing + offset
        for index, offset in enumerate(jitter)
    ]
    n_inserts = draw(st.integers(0, 2))
    gap_indices = draw(
        st.lists(
            st.integers(1, n_anchors - 2),
            min_size=n_inserts,
            max_size=n_inserts,
            unique=True,
        )
    )
    insert_durations = draw(
        st.lists(
            st.floats(500.0, 8000.0),
            min_size=n_inserts,
            max_size=n_inserts,
        )
    )
    insert_positions = [
        (old_times[gap - 1] + old_times[gap]) / 2 for gap in gap_indices
    ]
    anchors = []
    for index, old in enumerate(old_times):
        remastered = slope * old + intercept
        for position, duration in zip(insert_positions, insert_durations):
            if position < old:
                remastered += duration
        anchors.append(syncfit.Anchor(old, remastered, tolerance, "H%d" % index))
    return anchors, insert_positions, insert_durations, tolerance


@given(feasible_scenarios())
@settings(max_examples=120, deadline=None)
def test_piecewise_always_fits_feasible_truth(scenario):
    anchors, positions, insert_durations, tolerance = scenario
    results = syncfit.solve_piecewise(anchors, (0.999, 1.001), 2)
    assert results
    # Every reported fit keeps every anchor inside its tolerance band.
    for result in results:
        assert result.feasible
        for anchor in anchors:
            low, high = syncfit.prediction_interval(result, anchor.old)
            assert low >= anchor.remastered - anchor.tolerance - 1e-6
            assert high <= anchor.remastered + anchor.tolerance + 1e-6
    # The exact generating structure is always recoverable.
    truth_fit = syncfit.solve_structure(
        anchors, (0.999, 1.001), positions
    )
    assert truth_fit is not None
    for anchor in anchors:
        low, high = syncfit.prediction_interval(truth_fit, anchor.old)
        assert low <= anchor.remastered + 1e-6
        assert high >= anchor.remastered - 1e-6
    if not insert_durations:
        linear = syncfit.solve_linear(anchors, (0.999, 1.001))
        assert linear.feasible


@given(feasible_scenarios())
@settings(max_examples=100, deadline=None)
def test_large_insertion_forces_linear_infeasibility(scenario):
    anchors, positions, insert_durations, tolerance = scenario
    assume(insert_durations)
    assume(min(insert_durations) > max(5 * tolerance, 2500.0))
    # Anchors sit on both sides of every jump and the span is wide.  A jump
    # this large cannot be absorbed by the narrow allowed speed band, so the
    # uniform-speed model must fail while an insertion model fits.
    linear = syncfit.solve_linear(anchors, (0.999, 1.001))
    assert not linear.feasible
    alternatives = syncfit.insertion_alternatives(
        anchors, (0.999, 1.001), 2
    )
    assert alternatives


def test_parse_anchor_lines_seconds_and_comments():
    anchors = syncfit.parse_anchor_lines(
        [
            "# a comment",
            "",
            "60 62.7 200 opening",
            "120,123.9,200,scene one",
            "180\t185.1\t200\tscene two",
        ]
    )
    assert len(anchors) == 3
    assert anchors[0].old == 60000.0
    assert anchors[1].remastered == 123900.0
    assert anchors[2].tolerance == 200.0
    assert anchors[0].label == "opening"


def test_parse_anchor_lines_rejects_short_rows():
    with pytest.raises(ValueError):
        syncfit.parse_anchor_lines(["60 62.7"])


def test_parse_anchor_lines_accepts_srt_timestamps():
    anchors = syncfit.parse_anchor_lines(
        ["00:01:00,000 00:01:02,700 200 stamped"]
    )
    assert anchors[0].old == 60000.0
    assert anchors[0].remastered == 62700.0


def jump_anchors():
    anchors = []
    for old in range(30000, 300001, 30000):
        remastered = old + (9000.0 if old > 150000.0 else 0.0)
        anchors.append(syncfit.Anchor(old, remastered, 150.0, "L"))
    return anchors


def test_diagnose_flags_required_insertion():
    summary = syncfit.diagnose(
        jump_anchors(), (0.97, 1.03), 2, min_insert_ms=1000.0
    )
    assert not summary["linear"].feasible
    assert not summary["ambiguous"]
    assert summary["insertion_models"]
    assert summary["explanations"][0].inserts


def test_diagnose_flags_ambiguous_when_both_fit():
    anchors = []
    for old in range(30000, 300001, 30000):
        remastered = old + (250.0 if old > 150000.0 else 0.0)
        anchors.append(syncfit.Anchor(old, remastered, 400.0, "L"))
    summary = syncfit.diagnose(
        anchors, (0.97, 1.03), 2, min_insert_ms=100.0
    )
    assert summary["linear"].feasible
    assert summary["ambiguous"]
    subtitles = [(old, "sub-%d" % index)
                 for index, old in enumerate(range(30000, 300001, 30000))]
    with_subs = syncfit.diagnose(
        anchors, (0.97, 1.03), 2, subtitles, min_insert_ms=100.0
    )
    assert with_subs["divergence_blocks"]
    model, rows = with_subs["divergence_blocks"][0]
    assert rows
    for _label, old_time, _li, _pi, extra in rows:
        assert old_time > min(p for p, _ in model.inserts)
        assert extra[1] >= 100.0 - 1e-9


CLI = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "srt_tools",
    "srt-sync-diagnose",
)
EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
)


def run_cli(anchor_name, *extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    output = subprocess.check_output(
        [
            sys.executable,
            CLI,
            "-a",
            os.path.join(EXAMPLES, anchor_name),
        ]
        + list(extra),
        env=env,
    )
    return output.decode("utf-8")


def test_cli_reports_insertion_and_verdict():
    report = run_cli(
        "anchors_insert.tsv",
        "--speed-low", "0.97",
        "--speed-high", "1.03",
        "-n", "2",
        "-m", "1000",
    )
    assert "INSERTION REQUIRED" in report
    assert "00:02:30,000" in report
    assert "00:00:07,500" in report


def test_cli_reports_uniform_speed_verdict():
    report = run_cli(
        "anchors_speed.tsv",
        "--speed-low", "0.95",
        "--speed-high", "1.10",
        "-n", "2",
        "-m", "1000",
    )
    assert "UNIFORM SPEED" in report
    assert "FEASIBLE" in report


def test_cli_json_is_valid_and_consistent():
    import json
    report = run_cli(
        "anchors_ambiguous.tsv",
        "--speed-low", "0.97",
        "--speed-high", "1.03",
        "-n", "2",
        "-m", "100",
        "-s", os.path.join(EXAMPLES, "old_print.srt"),
        "--json",
    )
    payload = json.loads(report)
    assert payload["ambiguous"] is True
    assert payload["linear"] is not None
    assert payload["divergence_blocks"]
    block = payload["divergence_blocks"][0]
    assert block["affected"]
    assert block["model"]["inserts"]
