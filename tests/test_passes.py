import os

import numpy as np
import pytest

import lasio
from lasio.passes import (
    DepthHistory,
    Pass,
    PassSegmentation,
    UnresolvedSegment,
    depth_history,
    detect_passes,
)


test_dir = os.path.dirname(__file__)


def egfn(fn):
    return os.path.join(test_dir, "examples", fn)


def test_detect_passes_down_then_up():
    segmentation = detect_passes([100, 99, 98, 97, 98, 99, 100])

    assert isinstance(segmentation, PassSegmentation)
    assert segmentation.directions == ["up", "down"]
    assert [segment.depths.tolist() for segment in segmentation.passes] == [
        [100.0, 99.0, 98.0, 97.0],
        [98.0, 99.0, 100.0],
    ]
    assert [segment.pass_index for segment in segmentation.passes] == [0, 1]
    assert segmentation.unresolved == ()


def test_detect_passes_preserves_every_sample_in_order():
    depths = [95, 96, 97, 97, 98, 99, 99.9, 99.95, 100, 99, 98, 97, 96, 95, 95.05]
    segmentation = detect_passes(depths, min_reversal=0.2)

    positions = [
        index
        for segment in segmentation.segments
        for index in range(segment.start_index, segment.end_index + 1)
    ]
    assert positions == list(range(len(depths)))
    assert np.array_equal(
        np.concatenate([segment.depths for segment in segmentation.segments]),
        np.array(depths, dtype=float),
    )


def test_small_wobble_stays_in_same_pass():
    depths = [100, 99, 98, 97.9, 98.05, 97.8, 96, 95]
    segmentation = detect_passes(depths, min_reversal=1.0)

    assert len(segmentation.passes) == 1
    assert segmentation.passes[0].depths.tolist() == [float(d) for d in depths]
    assert segmentation.unresolved == ()


def test_wobble_is_not_a_new_pass_but_large_reversal_is():
    depths = [100, 99, 98, 97.9, 98.05, 98.4, 98.6, 97.5, 96, 95]

    wobble = detect_passes(depths, min_reversal=1.0)
    reversal = detect_passes(depths, min_reversal=0.25)

    assert wobble.directions == ["up"]
    assert reversal.directions == ["up", "down", "up"]
    assert reversal.passes[1].depths.tolist() == [98.05, 98.4, 98.6]


def test_reversal_uses_distance_from_furthest_point():
    # The retreat from 97.9 exceeds 0.1, but the samples after the turnaround
    # only extend to 98.3 --- the reversal is confirmed and kept as a pass.
    depths = [100, 99, 98, 97.9, 98.05, 98.3]
    segmentation = detect_passes(depths, min_reversal=0.1)

    assert segmentation.directions == ["up", "down"]
    assert segmentation.passes[1].depths.tolist() == [98.05, 98.3]

    # With a threshold above the retreat, those same samples stay unresolved.
    unresolved = detect_passes(depths, min_reversal=0.5)
    assert unresolved.directions == ["up"]
    assert unresolved.unresolved[0].depths.tolist() == [98.05, 98.3]
    assert unresolved.unresolved[0].previous_direction == "up"
    assert unresolved.unresolved[0].candidate_direction == "down"


def test_threshold_is_configurable_by_alias():
    depths = [100, 99, 98, 97.9, 98.2, 98.4, 97.5, 96]

    for kwargs in (
        {"min_reversal": 0.1},
        {"reversal_threshold": 0.1},
        {"min_reversal_distance": 0.1},
        {"min_reversal_depth": 0.1},
        {"reversal_distance": 0.1},
        {"reversal": 0.1},
        {"threshold": 0.1},
    ):
        assert detect_passes(depths, **kwargs).directions == ["up", "down", "up"]


@pytest.mark.parametrize("value", [-1.0, np.inf, np.nan])
def test_invalid_threshold(value):
    with pytest.raises(ValueError):
        detect_passes([100, 99, 98], min_reversal=value)


def test_duplicate_depths_are_not_reversals():
    depths = [100, 99, 99, 98, 98, 98, 97, 96]
    segmentation = detect_passes(depths, min_reversal=0.5)

    assert len(segmentation.passes) == 1
    assert segmentation.passes[0].depths.tolist() == [
        100.0,
        99.0,
        99.0,
        98.0,
        98.0,
        98.0,
        97.0,
        96.0,
    ]


def test_duplicate_depths_at_turnaround_remain_in_outbound_pass():
    depths = [95, 96, 97, 98, 99, 100, 100, 100, 99, 98, 97, 96, 95]
    segmentation = detect_passes(depths, min_reversal=0.5)

    assert segmentation.directions == ["down", "up"]
    assert segmentation.passes[0].end_index == 7
    assert segmentation.passes[0].depths.tolist() == [
        95.0,
        96.0,
        97.0,
        98.0,
        99.0,
        100.0,
        100.0,
        100.0,
    ]


def test_flat_index_has_no_direction():
    segmentation = detect_passes([100, 100, 100], min_reversal=0.5)

    assert segmentation.passes == ()
    assert len(segmentation.unresolved) == 1
    assert segmentation.unresolved[0].direction is None
    assert segmentation.unresolved[0].depths.tolist() == [100.0, 100.0, 100.0]


def test_single_sample_index():
    segmentation = detect_passes([100.0], min_reversal=0.5)

    assert segmentation.passes == ()
    assert segmentation.unresolved[0].sample_indices.tolist() == [0]


def test_non_finite_depths_do_not_start_passes():
    depths = [100, 99, 98, np.nan, 97, 98]
    segmentation = detect_passes(depths, min_reversal=0.5)

    assert segmentation.directions == ["up", "down"]
    assert segmentation.passes[0].depths[[0, 1, 2, 4]].tolist() == [100.0, 99.0, 98.0, 97.0]


def test_empty_index():
    segmentation = detect_passes([], min_reversal=0.5)

    assert isinstance(segmentation, PassSegmentation)
    assert segmentation.passes == ()
    assert segmentation.unresolved == ()
    assert segmentation.directions == []


def test_depth_history_separates_passes_and_keeps_duplicates():
    las = lasio.read(egfn("depth_passes.las"))

    history = las.depth_history(97, min_reversal=0.2)

    assert isinstance(history, DepthHistory)
    assert history.directions == ["down", "up"]
    assert [group.direction for group in history.passes] == ["down", "up"]
    assert history.passes[0].curves["GR"].tolist() == [3.0, 4.0]
    assert history.passes[1].curves["GR"].tolist() == [12.0]
    assert history.passes[0].depths.tolist() == [97.0, 97.0]
    assert history.values("GR").tolist() == [3.0, 4.0, 12.0]
    assert len(history) == 3


def test_depth_history_separates_unresolved_wobble():
    las = lasio.read(egfn("depth_passes.las"))

    history = las.depth_history(95.03, min_reversal=0.2)

    assert history.passes == ()
    assert len(history.unresolved) == 1
    wobble = history.unresolved[0]
    assert wobble.depths.tolist() == [95.03]
    assert wobble.curves["GR"].tolist() == [16.0]
    assert wobble.previous_direction == "up"
    assert wobble.candidate_direction == "down"
    assert wobble.is_unresolved is True
    assert history.ambiguous is True


def test_depth_history_nearest_depth_when_tolerance_is_none():
    las = lasio.read(egfn("depth_passes.las"))

    history = las.depth_history(95.04, tolerance=None, min_reversal=0.2)

    assert history.tolerance is None
    assert [reading.depth for reading in history] == [95.05, 95.03]
    assert history.values("GR").tolist() == [15.0, 16.0]


def test_depth_history_tolerance_and_atol():
    las = lasio.read(egfn("depth_passes.las"))

    assert len(las.depth_history(95.04, tolerance=0.005, min_reversal=0.2)) == 0
    assert len(las.depth_history(95.04, tolerance=0.02, min_reversal=0.2)) == 2
    assert len(las.depth_history(95.04, atol=0.02, min_reversal=0.2)) == 2
    assert len(depth_history(las.index, 95.04, atol=0.02, min_reversal=0.2)) == 2


def test_depth_history_without_data():
    history = depth_history([100, 99, 98, 99, 100], 99)

    assert history.directions == ["up", "down"]
    assert history.passes[0].curves == {}
    assert history.values() == {}


def test_depth_history_empty_result_is_falsey():
    las = lasio.read(egfn("depth_passes.las"))

    history = las.depth_history(1234.5, min_reversal=0.2)

    assert len(history) == 0
    assert not history
    assert history.passes == ()
    assert history.unresolved == ()


def test_split_passes_returns_las_per_pass():
    las = lasio.read(egfn("depth_passes.las"))

    parts = las.split_passes(min_reversal=0.2)

    assert len(parts) == 2
    assert [part.direction for part in parts] == ["down", "up"]
    assert [len(part.index) for part in parts] == [9, 5]
    assert parts[0].well["STRT"].value == "95.00000"
    assert parts[0].well["STOP"].value == "100.00000"
    assert parts[1].index.tolist() == [99.0, 98.0, 97.0, 96.0, 95.0]
    assert "GR" in parts[1].keys()

    original = lasio.read(egfn("depth_passes.las"))
    assert np.array_equal(las.index, original.index)
    assert np.array_equal(las["GR"], original["GR"])


def test_split_passes_can_include_unresolved():
    las = lasio.read(egfn("depth_passes.las"))

    parts = las.split_passes(min_reversal=0.2, include_unresolved=True)

    assert len(parts) == 3
    assert parts[-1].direction is None
    assert parts[-1].index.tolist() == [95.05, 95.03]


def test_segmentation_split_from_array_only():
    segmentation = detect_passes([100, 99, 98, 99, 100])

    split = segmentation.split()

    assert len(split) == 2
    assert split[0].tolist() == [100.0, 99.0, 98.0]
    assert split[1].tolist() == [99.0, 100.0]


def test_segmentation_curves_and_metadata():
    segmentation = detect_passes(
        [100, 99, 98, 99, 100],
        data=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
        mnemonics=["GR", "RHOB"],
    )

    assert segmentation.passes[0].curves["GR"].tolist() == [1, 3, 5]
    assert segmentation.passes[0].curves["RHOB"].tolist() == [2, 4, 6]
    assert segmentation.pass_for_sample(2) is segmentation.passes[0]
    assert segmentation.segment_for_sample(3) is segmentation.passes[1]
    assert segmentation.passes[0]["GR"].tolist() == [1, 3, 5]


def test_detect_passes_validates_data_shape():
    with pytest.raises(ValueError):
        detect_passes([1, 2], data=[[1], [2], [3]])

    with pytest.raises(ValueError):
        detect_passes([1, 2], data=[[1, 2], [3, 4]], mnemonics=["GR"])

    with pytest.raises(ValueError):
        detect_passes([[1, 2], [3, 4]])


def test_lasfile_find_passes_alias():
    las = lasio.read(egfn("depth_passes.las"))

    assert las.find_passes(min_reversal=0.2).directions == ["down", "up"]
    assert las.detect_passes(min_reversal=0.2).directions == [
        segment.direction
        for segment in las.detect_passes(min_reversal=0.2).passes
    ]


def test_history_uses_supplied_traversal():
    las = lasio.read(egfn("depth_passes.las"))
    segmentation = las.detect_passes(min_reversal=0.2)

    history = depth_history(las.index, 97, traversal=segmentation, data=las.data, mnemonics=las.keys())

    assert history.directions == ["down", "up"]


def test_unresolved_segment_has_no_direction():
    segmentation = detect_passes([100, 99, 98, 97.9, 98.0], min_reversal=1.0)

    assert isinstance(segmentation.unresolved[0], UnresolvedSegment)
    assert segmentation.unresolved[0].direction is None
    assert isinstance(segmentation.passes[0], Pass)
    assert segmentation.passes[0].direction == "up"


def test_pass_samples_are_views_of_original_segmentation():
    segmentation = detect_passes([100, 99, 98, 99, 100])

    assert np.array_equal(segmentation.passes[0].depths, segmentation.depth[:3])
    assert segmentation.passes[0].depths is not segmentation.depth


def test_existing_reverse_sample_is_a_single_pass():
    las = lasio.read(egfn("sample_rev.las"))

    passes = las.detect_passes()

    assert passes.directions == ["up"]
    assert passes.passes[0].start_index == 0
    assert passes.passes[0].end_index == len(las.index) - 1
    assert passes.passes[0].start == 1670.0
    assert passes.passes[0].stop == 1660.0


def test_depth_history_preserves_acquisition_order_after_split():
    las = lasio.read(egfn("depth_passes.las"))
    history = las.depth_history(97, min_reversal=0.2)

    assert [reading.sample_index for reading in history] == [2, 3, 11]
    assert [reading.curves["GR"] for reading in history] == [3.0, 4.0, 12.0]
    assert [group.readings[0].sample_index for group in history.passes] == [2, 11]


def test_segmentation_is_callable_to_rerun_with_new_threshold():
    las = lasio.read(egfn("depth_passes.las"))

    default = las.passes
    assert default.min_reversal == 0.0
    assert len(default) == 4
    assert las.passes(min_reversal=0.2).directions == ["down", "up"]
    assert las.passes(min_reversal=0.2).min_reversal == 0.2
