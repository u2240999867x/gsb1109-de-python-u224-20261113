"""Tests for the order-preserving two-well depth correlation engine."""

import numpy as np
import pytest

import lasio
from lasio.well_correlation import (
    Anchor,
    CorrelationConflictError,
    CorrelationEngine,
    Marker,
    MarkerSpecificationError,
    StretchLimits,
    correlate_wells,
)


def make_engine(master_markers, control_markers, **kwargs):
    master_depths = np.arange(0.0, 401.0, 1.0)
    control_depths = np.arange(0.0, 101.0, 1.0)
    kwargs.setdefault("stretch", (0.1, 10.0))
    return CorrelationEngine(
        master_depths, control_depths,
        master_markers, control_markers, **kwargs
    )


def test_identity_tie_maps_marker_depths_exactly():
    markers = [("A", 100.0), ("B", 200.0), ("C", 300.0)]
    engine = make_engine(markers, markers)
    result = engine.map_samples(np.array([100.0, 200.0, 300.0]))
    assert result["status"].tolist() == ["unique", "unique", "unique"]
    np.testing.assert_allclose(result["nominal"], [100.0, 200.0, 300.0])
    np.testing.assert_allclose(result["lower"], result["nominal"])
    np.testing.assert_allclose(result["upper"], result["nominal"])


def test_piecewise_linear_nominal_mapping_preserves_order():
    master_markers = [("A", 100.0), ("B", 200.0), ("C", 300.0)]
    control_markers = [("A", 10.0), ("B", 30.0), ("C", 60.0)]
    engine = make_engine(master_markers, control_markers)
    result = engine.map_samples(np.array([10.0, 20.0, 30.0, 45.0, 60.0]))
    np.testing.assert_allclose(
        result["nominal"], [100.0, 150.0, 200.0, 250.0, 300.0]
    )
    assert np.all(np.diff(result["nominal"]) > 0)


def test_anchor_tolerance_produces_feasible_interval():
    anchors = [
        Anchor("A", master_depth=100.0, control_depth=10.0),
        Anchor("B", master_depth=120.0, control_depth=30.0),
    ]
    engine = make_engine(
        [("A", 100.0), ("B", 120.0)],
        [("A", 10.0), ("B", 30.0)],
        anchors=anchors,
        anchor_tolerance=2.0,
        stretch=(1.0, 1.0),
    )
    result = engine.map_samples(np.array([20.0]))
    assert result["status"][0] == "interval"
    assert not result["unique"][0]
    assert result["lower"][0] == pytest.approx(108.0)
    assert result["upper"][0] == pytest.approx(112.0)
    assert result["nominal"][0] == pytest.approx(110.0)

    tight = make_engine(
        [("A", 100.0), ("B", 120.0)],
        [("A", 10.0), ("B", 30.0)],
        anchors=anchors,
        anchor_tolerance=2.0,
        stretch=(1.0, 1.0),
        unique_tolerance=5.0,
    )
    assert tight.map_samples(np.array([20.0]))["unique"][0]


def test_repeated_formation_is_paired_by_depth_order():
    master_markers = [
        ("A", 100.0), ("S", 120.0), ("S", 150.0), ("B", 180.0)
    ]
    control_markers = [
        ("A", 10.0), ("S", 25.0), ("S", 40.0), ("B", 55.0)
    ]
    engine = make_engine(master_markers, control_markers)
    labels = [node.label for node in engine.nodes]
    assert labels == ["A", "S", "S#m2/c2", "B"]
    result = engine.map_samples(np.array([25.0, 40.0]))
    np.testing.assert_allclose(result["nominal"], [120.0, 150.0])


def test_unequal_repeated_formation_requires_explicit_anchors():
    master_markers = [("A", 100.0), ("S", 120.0), ("S", 150.0)]
    control_markers = [("A", 10.0), ("S", 25.0)]
    with pytest.raises(MarkerSpecificationError):
        make_engine(master_markers, control_markers)

    anchors = [
        Anchor("A", 100.0, 10.0),
        Anchor("S", 150.0, 25.0,
               master_occurrence=1, control_occurrence=0),
    ]
    engine = make_engine(
        master_markers, control_markers, anchors=anchors
    )
    names = [node.name for node in engine.nodes]
    assert names == ["A", "S"]
    result = engine.map_samples(np.array([25.0]))
    assert result["nominal"][0] == pytest.approx(150.0)


def test_anchor_attempting_to_cross_confirmed_interfaces_conflicts():
    anchors = [
        Anchor("X", master_depth=200.0, control_depth=10.0),
        Anchor("Y", master_depth=100.0, control_depth=20.0),
    ]
    engine = make_engine(
        [("X", 200.0), ("Y", 100.0)],
        [("X", 10.0), ("Y", 20.0)],
        anchors=anchors,
    )
    assert engine.feasible is False
    kinds = {conflict.kind for conflict in engine.assess()}
    assert "order" in kinds
    with pytest.raises(CorrelationConflictError) as exc_info:
        engine.map_samples()
    assert exc_info.value.conflicts


def test_thickness_stretch_violation_is_reported_as_conflict():
    engine = make_engine(
        [("A", 100.0), ("B", 130.0)],
        [("A", 10.0), ("B", 30.0)],
        stretch=(0.9, 1.1),
    )
    assert engine.feasible is False
    conflict = engine.assess()[0]
    assert conflict.kind == "thickness"
    assert conflict.markers == ("A", "B")
    with pytest.raises(CorrelationConflictError):
        engine.map_samples()


def test_anchor_outside_tolerance_rejected_upfront():
    anchors = [
        Anchor("A", master_depth=100.0, control_depth=10.0),
        Anchor("B", master_depth=120.0, control_depth=30.0),
    ]
    with pytest.raises(MarkerSpecificationError):
        make_engine(
            [("A", 100.0), ("B", 125.0)],
            [("A", 10.0), ("B", 30.0)],
            anchors=anchors,
            anchor_tolerance=2.0,
        )


def test_duplicate_and_unknown_anchors_are_rejected():
    with pytest.raises(MarkerSpecificationError):
        make_engine(
            [("A", 100.0), ("B", 120.0)],
            [("A", 10.0), ("B", 30.0)],
            anchors=[
                Anchor("A", 100.0, 10.0),
                Anchor("A", 100.0, 10.0),
            ],
        )
    with pytest.raises(MarkerSpecificationError):
        make_engine(
            [("A", 100.0), ("B", 120.0)],
            [("A", 10.0), ("B", 30.0)],
            anchors=[Anchor("Z", 100.0, 10.0)],
        )


def test_extrapolation_is_unresolved_then_bounded():
    master_markers = [("A", 100.0), ("B", 120.0)]
    control_markers = [("A", 10.0), ("B", 30.0)]
    engine = make_engine(
        master_markers, control_markers, stretch=(1.0, 1.0)
    )
    result = engine.map_samples(np.array([0.0, 40.0]))
    assert result["status"].tolist() == ["unresolved", "unresolved"]
    assert np.isnan(result["nominal"]).all()
    assert result["reasons"][0]

    bounded = make_engine(
        master_markers, control_markers,
        stretch=(1.0, 1.0), extrapolate=(0.5, 1.5),
    )
    result = bounded.map_samples(np.array([0.0, 40.0]))
    # 10 control units beyond the anchors -> 5 to 15 master units.
    assert result["lower"][0] == pytest.approx(85.0)
    assert result["upper"][0] == pytest.approx(95.0)
    assert result["lower"][1] == pytest.approx(125.0)
    assert result["upper"][1] == pytest.approx(135.0)


def test_samples_exactly_on_outer_anchors_are_resolved():
    engine = make_engine(
        [("A", 100.0), ("B", 120.0)],
        [("A", 10.0), ("B", 30.0)],
        stretch=(1.0, 1.0),
    )
    result = engine.map_samples(np.array([10.0, 30.0]))
    np.testing.assert_allclose(result["nominal"], [100.0, 120.0])
    assert result["status"].tolist() == ["unique", "unique"]


def test_per_layer_stretch_limits():
    limits = StretchLimits(
        default=(0.5, 2.0),
        by_name={"B": (0.9, 1.1), ("A", "C"): (0.2, 5.0)},
    )
    assert limits.for_layer("X", "B") == (0.9, 1.1)
    assert limits.for_layer("A", "C") == (0.2, 5.0)
    assert limits.for_layer("X", "Y") == (0.5, 2.0)
    with pytest.raises(MarkerSpecificationError):
        StretchLimits(default=(1.2, 0.8))


def test_invalid_depth_frames_rejected():
    good = [("A", 100.0), ("B", 120.0)]
    with pytest.raises(MarkerSpecificationError):
        CorrelationEngine(
            np.array([1.0, 1.0, 2.0]), np.arange(3.0), good, good
        )
    with pytest.raises(MarkerSpecificationError):
        CorrelationEngine(
            np.arange(1.0), np.arange(10.0), good, good
        )


def test_interval_result_objects_and_marker_objects():
    engine = make_engine(
        [Marker("A", 100.0), Marker("B", 120.0)],
        [Marker("A", 10.0), Marker("B", 30.0)],
        stretch=(1.0, 1.0),
    )
    intervals = engine.intervals(np.array([10.0, 20.0]))
    assert intervals[0].unique is True
    assert intervals[1].control_depth == 20.0
    assert intervals[1].nominal == pytest.approx(110.0)


def test_correlate_wells_accepts_lasfile_objects():
    master = lasio.LASFile()
    master.append_curve("DEPT", data=np.arange(100.0, 131.0, 1.0))
    control = lasio.LASFile()
    control.append_curve("DEPT", data=np.arange(10.0, 31.0, 1.0))
    result = correlate_wells(
        master, control,
        [("A", 100.0), ("B", 120.0)],
        [("A", 10.0), ("B", 30.0)],
        stretch=(1.0, 1.0),
    )
    assert result["nominal"].shape == control.index.shape
    assert result["status"][0] == "unique"
    assert result["nominal"][0] == pytest.approx(100.0)


def test_feasible_engine_reports_no_conflicts():
    engine = make_engine(
        [("A", 100.0), ("B", 120.0), ("C", 140.0)],
        [("A", 10.0), ("B", 30.0), ("C", 50.0)],
        stretch=(1.0, 1.0),
    )
    assert engine.feasible is True
    assert engine.assess() == []


def test_middle_anchor_tolerance_propagates_through_both_layers():
    anchors = [
        Anchor("B", master_depth=120.0, control_depth=30.0),
    ]
    engine = make_engine(
        [("A", 100.0), ("B", 120.0), ("C", 140.0)],
        [("A", 10.0), ("B", 30.0), ("C", 50.0)],
        anchors=anchors,
        anchor_tolerance=2.0,
        stretch=(0.5, 1.5),
    )
    result = engine.map_samples(np.array([20.0, 30.0, 40.0]))
    np.testing.assert_allclose(result["lower"], [109.0, 118.0, 129.0])
    np.testing.assert_allclose(result["upper"], [111.0, 122.0, 131.0])
    np.testing.assert_allclose(result["nominal"], [110.0, 120.0, 130.0])


def test_crossing_conflict_names_both_contradictory_anchors():
    anchors = [
        Anchor("X", master_depth=200.0, control_depth=10.0),
        Anchor("Y", master_depth=100.0, control_depth=20.0),
    ]
    engine = make_engine(
        [("X", 200.0), ("Y", 100.0)],
        [("X", 10.0), ("Y", 20.0)],
        anchors=anchors,
    )
    conflict = engine.assess()[0]
    assert "X" in conflict.anchors[0] or "X" in conflict.markers[0]
    assert any("Y" in marker for marker in conflict.markers)


def test_stretch_band_widens_towards_uncertain_anchor():
    # A is a confirmed (pinned) interface; only B carries anchor tolerance.
    anchors = [
        Anchor("B", master_depth=120.0, control_depth=30.0),
    ]
    engine = make_engine(
        [("A", 100.0), ("B", 120.0)],
        [("A", 10.0), ("B", 30.0)],
        anchors=anchors,
        anchor_tolerance=2.0,
        stretch=(0.5, 1.5),
    )
    result = engine.map_samples(np.array([10.0, 20.0, 30.0]))
    widths = result["upper"] - result["lower"]
    assert widths[0] == pytest.approx(0.0)
    assert widths[1] == pytest.approx(2.0)
    assert widths[2] == pytest.approx(4.0)
    np.testing.assert_allclose(result["lower"], [100.0, 109.0, 118.0])
    np.testing.assert_allclose(result["upper"], [100.0, 111.0, 122.0])
    assert result["nominal"][1] == pytest.approx(110.0)
