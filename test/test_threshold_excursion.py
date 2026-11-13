'''
Tests for the threshold excursion duration bounds with dropout gaps.
'''

import os
import random

import pytest

from pyulog import ULog
from pyulog.threshold_excursion import (
    Gap, Interval, RateBudget, RateSegment, Sample, analyze,
    envelope_paths, gaps_from_ulog, infer_sampling_gaps)

TEST_PATH = os.path.dirname(os.path.abspath(__file__))


SEGMENTS = [
    RateSegment(0.0, 15.0, 2.0),
    RateSegment(15.0, None, 1.0),
]

DEMO_SAMPLES = [
    Sample(0, 40.0), Sample(1, 42.0), Sample(2, 44.0), Sample(3, 46.0),
    Sample(4, 48.0), Sample(5, 50.0), Sample(6, 52.0), Sample(7, 51.0),
    Sample(8, 49.0), Sample(9, 48.0), Sample(10, 47.0),
    Sample(13, 52.0), Sample(14, 52.0),
    Sample(15, 51.0), Sample(16, 50.0), Sample(17, 49.0),
    Sample(18, 48.0), Sample(19, 47.0), Sample(20, 48.0),
    Sample(23, 50.5), Sample(24, 51.0), Sample(25, 51.0),
    Sample(26, 50.0), Sample(27, 49.0), Sample(28, 48.0),
    Sample(29, 47.0), Sample(30, 46.0),
]
DEMO_GAPS = [Gap(10.0, 13.0), Gap(20.0, 23.0)]
THRESHOLD = 50.0


def measure(path, component, threshold):
    '''Duration of a piecewise linear path strictly above the threshold.'''
    values = getattr(path, component)
    total = 0.0
    for i in range(len(path.t) - 1):
        if 0.5 * (values[i] + values[i + 1]) > threshold:
            total += path.t[i + 1] - path.t[i]
    return total


class TestRateBudget:
    '''Rate budget integration and bidirectional consumption.'''

    def setup_method(self):
        self.rates = RateBudget(SEGMENTS)

    def test_budget_within_and_across_segments(self):
        assert self.rates.budget(0.0, 10.0) == pytest.approx(20.0)
        assert self.rates.budget(14.0, 16.0) == pytest.approx(3.0)
        assert self.rates.budget(15.0, 30.0) == pytest.approx(15.0)

    def test_advance_and_retreat(self):
        assert self.rates.advance(10.0, 3.0) == pytest.approx(11.5)
        assert self.rates.retreat(18.0, 4.0) == pytest.approx(14.5)
        assert self.rates.advance(14.0, 3.0) == pytest.approx(16.0)
        assert self.rates.retreat(16.0, 3.0) == pytest.approx(14.0)

    def test_invalid_segment_configurations(self):
        with pytest.raises(ValueError):
            RateBudget([RateSegment(0.0, 5.0, 1.0),
                        RateSegment(6.0, None, 1.0)])
        with pytest.raises(ValueError):
            RateBudget([RateSegment(0.0, 6.0, 1.0),
                        RateSegment(5.0, None, 1.0)])
        with pytest.raises(ValueError):
            RateBudget([RateSegment(0.0, None, 0.0)])
        with pytest.raises(ValueError):
            RateBudget([])


class TestClassification:
    '''Interval classification under the rate envelope.'''

    def test_simple_two_sided_interval(self):
        result = analyze(
            [Sample(0.0, 40.0), Sample(10.0, 52.0)],
            THRESHOLD, [RateSegment(0.0, None, 2.0)])
        assert result.certain_duration == pytest.approx(1.0)
        assert result.possible_duration == pytest.approx(5.0)
        assert result.ambiguous_intervals == [Interval(5.0, 9.0)]

    def test_threshold_is_strict(self):
        result = analyze(
            [Sample(0.0, 50.0), Sample(10.0, 50.0)],
            THRESHOLD, [RateSegment(0.0, None, 2.0)])
        assert result.certain_duration == pytest.approx(0.0)
        assert result.possible_duration == pytest.approx(10.0)

    def test_infeasible_samples_raise(self):
        with pytest.raises(ValueError):
            analyze(
                [Sample(0.0, 40.0), Sample(10.0, 61.0)],
                THRESHOLD, [RateSegment(0.0, None, 2.0)])

    def test_duplicate_timestamps_raise(self):
        with pytest.raises(ValueError):
            analyze(
                [Sample(0.0, 40.0), Sample(0.0, 41.0)],
                THRESHOLD, [RateSegment(0.0, None, 2.0)])

    def test_window_boundary_extension(self):
        # From (2, 40) and (10, 40) with r=2 the threshold needs a budget of
        # 5, which neither 2 s boundary extension can provide.
        blocked = analyze(
            [Sample(2.0, 40.0), Sample(10.0, 40.0)],
            THRESHOLD, [RateSegment(0.0, None, 2.0)],
            t_start=0.0, t_end=12.0)
        assert blocked.certain_duration == pytest.approx(0.0)
        assert blocked.possible_duration == pytest.approx(0.0)

        # (10, 48) needs a budget of 1 to reach 50, so [11, 12] on the right
        # boundary is possibly over; the interior [2, 10] itself can reach
        # the threshold between t=7 and t=9 as well.
        reachable = analyze(
            [Sample(2.0, 40.0), Sample(10.0, 48.0)],
            THRESHOLD, [RateSegment(0.0, None, 2.0)],
            t_start=0.0, t_end=12.0)
        assert reachable.certain_duration == pytest.approx(0.0)
        assert reachable.possible_duration == pytest.approx(3.0)
        assert reachable.possible_intervals == \
            [Interval(7.0, 9.0), Interval(11.0, 12.0)]


class TestGapHandling:
    '''Dropout gaps constrain rather than delete their surroundings.'''

    def test_gap_keeps_endpoint_constraints(self):
        result = analyze(
            [Sample(0.0, 40.0), Sample(10.0, 47.0), Sample(13.0, 52.0),
             Sample(20.0, 40.0)],
            THRESHOLD, [RateSegment(0.0, None, 2.0)],
            t_start=0.0, t_end=20.0, gaps=[Gap(10.0, 13.0)])
        gap_assessment = next(
            item for item in result.assessments if item.kind == 'gap')
        assert gap_assessment.t_start == 10.0
        assert gap_assessment.t_end == 13.0
        # the (13, 52) endpoint makes the last second certainly over
        assert gap_assessment.certain_intervals == [Interval(12.0, 13.0)]
        possible = gap_assessment.possible_intervals
        assert len(possible) == 1
        assert possible[0].t_start == pytest.approx(11.5)
        assert possible[0].t_end == pytest.approx(13.0)

    def test_gap_with_unreachable_threshold_is_not_overcounted(self):
        result = analyze(
            [Sample(0.0, 20.0), Sample(10.0, 30.0), Sample(12.0, 30.0),
             Sample(20.0, 20.0)],
            THRESHOLD, [RateSegment(0.0, None, 2.0)],
            t_start=0.0, t_end=20.0, gaps=[Gap(10.0, 12.0)])
        assert result.possible_duration == pytest.approx(0.0)
        assert result.certain_duration == pytest.approx(0.0)

    def test_demo_bounds_and_partition(self):
        result = analyze(DEMO_SAMPLES, THRESHOLD, SEGMENTS,
                         0.0, 30.0, DEMO_GAPS)
        assert result.certain_duration == pytest.approx(10.0)
        assert result.possible_duration == pytest.approx(11.0)
        assert [(iv.t_start, iv.t_end)
                for iv in result.ambiguous_intervals] == \
            [(11.5, 12.0), (22.0, 22.5)]
        # certain and ambiguous are disjoint and together span the possible set
        assert all(iv.t_end <= other.t_start + 1e-12 or
                   other.t_end <= iv.t_start + 1e-12
                   for iv in result.ambiguous_intervals
                   for other in result.certain_intervals)
        span = (sum(iv.t_end - iv.t_start
                    for iv in result.certain_intervals) +
                sum(iv.t_end - iv.t_start
                    for iv in result.ambiguous_intervals))
        assert span == pytest.approx(result.possible_duration)
        assert [item.kind for item in result.assessments
                if item.kind == 'gap'] == ['gap', 'gap']


class TestEnvelope:
    '''Extremal trajectories are feasible and attain both bounds.'''

    def setup_method(self):
        self.path = envelope_paths(
            DEMO_SAMPLES, SEGMENTS, 0.0, 30.0, num_points=30001)

    def test_envelope_ordering_and_sample_pinning(self):
        for low, high in zip(self.path.lower, self.path.upper):
            assert low <= high + 1e-9
        sample_map = {sample.t: sample.x for sample in DEMO_SAMPLES}
        for i, t in enumerate(self.path.t):
            if t in sample_map:
                assert self.path.lower[i] == pytest.approx(sample_map[t])
                assert self.path.upper[i] == pytest.approx(sample_map[t])

    def test_envelope_slopes_respect_rate_limits(self):
        for component in ('lower', 'upper'):
            values = getattr(self.path, component)
            for i in range(len(self.path.t) - 1):
                dt = self.path.t[i + 1] - self.path.t[i]
                slope = abs(values[i + 1] - values[i]) / dt
                midpoint = 0.5 * (self.path.t[i] + self.path.t[i + 1])
                limit = 2.0 if midpoint < 15.0 else 1.0
                assert slope <= limit + 1e-9

    def test_extremal_paths_realize_the_two_bounds(self):
        assert measure(self.path, 'lower', THRESHOLD) == \
            pytest.approx(10.0, abs=1e-3)
        assert measure(self.path, 'upper', THRESHOLD) == \
            pytest.approx(11.0, abs=1e-3)


class TestRandomizedTightness:
    '''Bounds stay tight for randomized feasible samples and gaps.'''

    @pytest.mark.parametrize('seed', range(10))
    def test_random_instance(self, seed):
        rng = random.Random(seed)
        rate = 2.0
        values = [45.0]
        for _ in range(20):
            values.append(values[-1] + rng.uniform(-rate, rate))
        full_samples = [Sample(float(t), values[t]) for t in range(21)]
        gaps = [Gap(7.0, 9.0), Gap(14.0, 16.0)]
        samples = [sample for sample in full_samples
                   if not any(gap.t_start < sample.t < gap.t_end
                              for gap in gaps)]
        result = analyze(samples, THRESHOLD,
                         [RateSegment(0.0, None, rate)],
                         0.0, 20.0, gaps)
        path = envelope_paths(samples,
                              [RateSegment(0.0, None, rate)],
                              0.0, 20.0, num_points=20001)
        assert measure(path, 'lower', THRESHOLD) == \
            pytest.approx(result.certain_duration, abs=2e-3)
        assert measure(path, 'upper', THRESHOLD) == \
            pytest.approx(result.possible_duration, abs=2e-3)
        assert result.certain_duration <= result.possible_duration + 1e-12


class TestUlogIntegration:
    '''Helpers converting real ULog dropout messages.'''

    def test_gaps_from_sample_log(self):
        ulog = ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        gaps = gaps_from_ulog(ulog)
        # zero-duration dropout is filtered; remaining gaps are in seconds
        assert gaps == [
            Gap(112.574774, 112.600774),
            Gap(112.614307, 112.645307),
            Gap(153.855108, 153.917108),
        ]

    def test_infer_sampling_gaps(self):
        timestamps = [0.0, 1.0, 2.0, 5.0, 6.0, 10.0]
        assert infer_sampling_gaps(timestamps, sample_period=1.0) == \
            [Gap(2.0, 5.0), Gap(6.0, 10.0)]
