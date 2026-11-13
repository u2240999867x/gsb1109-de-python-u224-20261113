'''
Bounds on the cumulative time a sampled signal spends above a threshold when
some samples are missing (ULog dropouts).

The signal is known exactly at sample points and unknown elsewhere. Between
two known points (t_a, x_a) and (t_b, x_b) it may evolve arbitrarily as long
as the absolute rate of change respects the piecewise constant limit
|dx/dt| <= r(t). Under this Lipschitz constraint the reachable value envelope
at time t in [t_a, t_b] is expressed with the rate budget

    R(t0, t1) = integral of r(s) ds from t0 to t1

    lower(t) = max(x_a - R(t_a, t), x_b - R(t, t_b))
    upper(t) = min(x_a + R(t_a, t), x_b + R(t, t_b))

An instant is certainly above the threshold when lower(t) > threshold and
possibly above when upper(t) >= threshold. Integrating these two sets over the
whole experiment gives a lower and an upper bound on the cumulative
over-threshold duration.

A gap therefore never invalidates the samples on either side: both endpoints
still constrain every value inside the gap through the rate budget, so the
gap is classified instead of being deleted.
'''

from collections import namedtuple
import math


Sample = namedtuple('Sample', ['t', 'x'])
RateSegment = namedtuple('RateSegment', ['t_start', 't_end', 'rate'])
Gap = namedtuple('Gap', ['t_start', 't_end'])
Interval = namedtuple('Interval', ['t_start', 't_end'])

IntervalAssessment = namedtuple('IntervalAssessment', [
    't_start', 't_end', 'x_start', 'x_end', 'rate_budget', 'kind',
    'certain_intervals', 'possible_intervals'])

ExcursionResult = namedtuple('ExcursionResult', [
    'threshold', 't_start', 't_end', 'certain_duration', 'possible_duration',
    'certain_intervals', 'possible_intervals', 'ambiguous_intervals',
    'assessments'])

EnvelopePath = namedtuple('EnvelopePath', ['t', 'lower', 'upper'])

_EPS = 1e-12


class RateBudget:
    '''Cumulative rate budget for piecewise constant rate limits.

    Segments are half-open [t_start, t_end) and must cover every queried
    span without overlap. A segment with t_end=None runs to infinity.
    Rates must be strictly positive so that the reachable envelope is
    genuinely two-sided; a zero-rate section would make the upper bound
    non-tight.
    '''

    def __init__(self, rate_segments):
        self._segments = sorted(rate_segments, key=lambda seg: seg.t_start)
        if not self._segments:
            raise ValueError('at least one rate segment is required')
        for prev, nxt in zip(self._segments, self._segments[1:]):
            if prev.t_end is None or nxt.t_start < prev.t_end - _EPS:
                raise ValueError('rate segments overlap or are not ordered')
            if nxt.t_start > prev.t_end + _EPS:
                raise ValueError('rate segments leave an uncovered gap')
            if prev.rate <= 0 or nxt.rate <= 0:
                raise ValueError('rate limits must be strictly positive')
        if self._segments[-1].rate <= 0:
            raise ValueError('rate limits must be strictly positive')

    def budget(self, t_start, t_end):
        '''Integral of r(t) over [t_start, t_end].'''
        if t_end < t_start - _EPS:
            raise ValueError('budget span ends before it starts')
        total = 0.0
        for segment in self._segments:
            seg_end = segment.t_end
            if seg_end is None:
                seg_end = math.inf
            lo = max(t_start, segment.t_start)
            hi = min(t_end, seg_end)
            if hi > lo:
                total += segment.rate * (hi - lo)
        if not math.isfinite(total):
            raise ValueError('open rate segment cannot cover the query')
        return total

    def advance(self, t, budget):
        '''Return the time reached while spending `budget` forward from t.'''
        return self._consume(t, budget, forward=True)

    def retreat(self, t, budget):
        '''Return the time reached while spending `budget` backward from t.'''
        return self._consume(t, budget, forward=False)

    def _consume(self, t, budget, forward):
        if budget < -_EPS:
            raise ValueError('budget must be non-negative')
        remaining = max(0.0, budget)
        current = t
        ordered = self._segments if forward else reversed(self._segments)
        for segment in ordered:
            seg_end = math.inf if segment.t_end is None else segment.t_end
            if forward:
                inside = segment.t_start <= current < seg_end
            else:
                inside = segment.t_start < current <= seg_end
            if not inside:
                continue
            boundary = seg_end if forward else segment.t_start
            span = abs(boundary - current)
            affordable = segment.rate * span
            if remaining <= affordable + _EPS:
                delta = remaining / segment.rate
                return current + delta if forward else current - delta
            remaining -= affordable
            current = boundary
        raise ValueError('rate segments do not cover the requested budget')


def _positive(intervals):
    '''Filter out zero-length intervals produced by touching the threshold.'''
    return [iv for iv in intervals if iv.t_end - iv.t_start > _EPS]


def _merge(intervals):
    '''Merge a sorted list of intervals into disjoint intervals.'''
    merged = []
    for interval in _positive(sorted(intervals)):
        if merged and interval.t_start <= merged[-1].t_end + _EPS:
            prev = merged[-1]
            merged[-1] = Interval(prev.t_start,
                                  max(prev.t_end, interval.t_end))
        else:
            merged.append(interval)
    return merged


def _subtract(intervals, blocked):
    '''Set difference between two lists of disjoint merged intervals.'''
    result = []
    for interval in intervals:
        pieces = [interval]
        for block in blocked:
            remaining = []
            for piece in pieces:
                if block.t_end <= piece.t_start or block.t_start >= piece.t_end:
                    remaining.append(piece)
                    continue
                if block.t_start > piece.t_start:
                    remaining.append(Interval(piece.t_start, block.t_start))
                if block.t_end < piece.t_end:
                    remaining.append(Interval(block.t_end, piece.t_end))
            pieces = remaining
        result.extend(pieces)
    return _merge(result)


def _classify_two_sided(t_a, x_a, t_b, x_b, rates, threshold):
    '''Classify an interval with known samples at both ends.'''
    total_budget = rates.budget(t_a, t_b)
    if abs(x_b - x_a) > total_budget + _EPS:
        raise ValueError(
            'samples at {} and {} violate the rate limit'.format(t_a, t_b))

    certain = []
    if x_a > threshold:
        reach = min(x_a - threshold, total_budget)
        certain.append(Interval(t_a, min(t_b, rates.advance(t_a, reach))))
    if x_b > threshold:
        reach = min(x_b - threshold, total_budget)
        certain.append(Interval(max(t_a, rates.retreat(t_b, reach)), t_b))

    possible = []
    need_a = max(0.0, threshold - x_a)
    need_b = max(0.0, threshold - x_b)
    # The two upward cones overlap only if their budgets fit together.
    if need_a + need_b <= total_budget + _EPS:
        t_lo = rates.advance(t_a, need_a)
        t_hi = rates.retreat(t_b, need_b)
        if t_hi - t_lo > _EPS:
            possible.append(Interval(max(t_a, t_lo), min(t_b, t_hi)))

    return _positive(certain), _merge(possible), total_budget


def _classify_left_boundary(t_outer, t_known, x_known, rates, threshold):
    '''Window boundary at t_outer, known sample at the right end t_known.'''
    span_budget = rates.budget(t_outer, t_known)
    certain = []
    if x_known > threshold:
        reach = min(x_known - threshold, span_budget)
        certain.append(Interval(
            max(t_outer, rates.retreat(t_known, reach)), t_known))

    possible = []
    if x_known >= threshold:
        possible.append(Interval(t_outer, t_known))
    else:
        need = threshold - x_known
        if need < span_budget - _EPS:
            possible.append(
                Interval(t_outer, rates.retreat(t_known, need)))
    return _positive(certain), _merge(possible)


def _classify_right_boundary(t_known, x_known, t_outer, rates, threshold):
    '''Known sample at the left end t_known, window boundary at t_outer.'''
    span_budget = rates.budget(t_known, t_outer)
    certain = []
    if x_known > threshold:
        reach = min(x_known - threshold, span_budget)
        certain.append(Interval(
            t_known, min(t_outer, rates.advance(t_known, reach))))

    possible = []
    if x_known >= threshold:
        possible.append(Interval(t_known, t_outer))
    else:
        need = threshold - x_known
        if need < span_budget - _EPS:
            possible.append(
                Interval(rates.advance(t_known, need), t_outer))
    return _positive(certain), _merge(possible)


def _interval_kind(t_start, t_end, gaps):
    '''Label an interval according to the dropout gaps overlapping it.'''
    for gap in gaps:
        if gap.t_start <= t_start + _EPS and gap.t_end >= t_end - _EPS:
            return 'gap'
        if gap.t_start < t_end and gap.t_end > t_start:
            return 'gap_partial'
    return 'sampled'


def analyze(samples, threshold, rate_segments, t_start=None, t_end=None,
            gaps=None):
    '''Compute lower and upper bounds on the over-threshold duration.

    `samples` is a sequence of Sample(t, x) covering the experiment, with gap
    interiors simply omitted. `rate_segments` are per-section RateSegment
    limits (t_end=None means open ended) that together cover the whole
    experiment window. `gaps` lists the known dropout intervals and is used
    for labelling; the rate limit alone constrains the signal inside them.
    '''
    samples = sorted(samples, key=lambda sample: sample.t)
    if len(samples) < 2:
        raise ValueError('at least two samples are required')
    for prev, nxt in zip(samples, samples[1:]):
        if nxt.t - prev.t <= _EPS:
            raise ValueError('duplicate sample timestamp at {}'.format(
                nxt.t))
    if t_start is None:
        t_start = samples[0].t
    if t_end is None:
        t_end = samples[-1].t
    if not t_start < t_end:
        raise ValueError('experiment window must have positive length')
    if samples[0].t < t_start - _EPS or samples[-1].t > t_end + _EPS:
        raise ValueError('samples lie outside the experiment window')
    gaps = sorted(gaps or [], key=lambda gap: gap.t_start)
    rates = RateBudget(rate_segments)

    anchors = []
    if t_start < samples[0].t - _EPS:
        anchors.append(('boundary_left', Sample(t_start, math.nan)))
    anchors.extend(('sample', sample) for sample in samples)
    if t_end > samples[-1].t + _EPS:
        anchors.append(('boundary_right', Sample(t_end, math.nan)))

    assessments = []
    certain_all = []
    possible_all = []

    for index in range(len(anchors) - 1):
        kind_a, sample_a = anchors[index]
        kind_b, sample_b = anchors[index + 1]
        kind = _interval_kind(sample_a.t, sample_b.t, gaps)
        x_a = math.nan if kind_a == 'boundary_left' else sample_a.x
        x_b = math.nan if kind_b == 'boundary_right' else sample_b.x

        if kind_a == 'boundary_left':
            certain, possible = _classify_left_boundary(
                sample_a.t, sample_b.t, sample_b.x, rates, threshold)
            budget = rates.budget(sample_a.t, sample_b.t)
        elif kind_b == 'boundary_right':
            certain, possible = _classify_right_boundary(
                sample_a.t, sample_a.x, sample_b.t, rates, threshold)
            budget = rates.budget(sample_a.t, sample_b.t)
        else:
            certain, possible, budget = _classify_two_sided(
                sample_a.t, sample_a.x, sample_b.t, sample_b.x,
                rates, threshold)

        certain = _merge(certain)
        possible = _merge(possible)
        certain_all.extend(certain)
        possible_all.extend(possible)
        assessments.append(IntervalAssessment(
            sample_a.t, sample_b.t, x_a, x_b, budget, kind,
            certain, possible))

    certain_intervals = _merge(certain_all)
    possible_intervals = _merge(possible_all)
    ambiguous_intervals = _subtract(possible_intervals, certain_intervals)
    certain_duration = sum(iv.t_end - iv.t_start
                           for iv in certain_intervals)
    possible_duration = sum(iv.t_end - iv.t_start
                            for iv in possible_intervals)

    return ExcursionResult(
        threshold, t_start, t_end, certain_duration, possible_duration,
        certain_intervals, possible_intervals, ambiguous_intervals,
        assessments)


def envelope_paths(samples, rate_segments, t_start, t_end, num_points=4001):
    '''Build the pointwise lowest and highest feasible trajectories.

    Both paths are piecewise linear, pass through every sample and respect
    the rate limits. The lower path exceeds the threshold exactly on the
    certainly over-threshold set and the upper path exactly on the possibly
    over-threshold set, which proves both bounds are attainable and tight.
    '''
    samples = sorted(samples, key=lambda sample: sample.t)
    rates = RateBudget(rate_segments)
    grid = [t_start + (t_end - t_start) * i / (num_points - 1)
            for i in range(num_points)]
    lower = [0.0] * num_points
    upper = [0.0] * num_points

    boundaries = [t_start] + [sample.t for sample in samples] + [t_end]
    values = [math.nan] + [sample.x for sample in samples] + [math.nan]

    for i, t in enumerate(grid):
        slot = 0
        while slot < len(boundaries) - 2 and t > boundaries[slot + 1] + _EPS:
            slot += 1
        t_a, t_b = boundaries[slot], boundaries[slot + 1]
        if slot == 0 and math.isnan(values[slot]):
            x_b = values[slot + 1]
            budget = rates.budget(t, t_b)
            lower[i] = x_b - budget
            upper[i] = x_b + budget
        elif slot == len(boundaries) - 2 and math.isnan(values[slot + 1]):
            x_a = values[slot]
            budget = rates.budget(t_a, t)
            lower[i] = x_a - budget
            upper[i] = x_a + budget
        else:
            x_a, x_b = values[slot], values[slot + 1]
            lower[i] = max(x_a - rates.budget(t_a, t),
                           x_b - rates.budget(t, t_b))
            upper[i] = min(x_a + rates.budget(t_a, t),
                           x_b + rates.budget(t, t_b))

    return EnvelopePath(grid, lower, upper)


def gaps_from_ulog(ulog):
    '''Convert ULog dropout messages into Gap objects (times in seconds).

    A dropout message carries the timestamp of the last message before the
    gap and its duration in milliseconds, so the missing region is
    [timestamp, timestamp + duration].
    '''
    gaps = []
    for dropout in ulog.dropouts:
        gap_start = dropout.timestamp / 1e6
        gap_end = (dropout.timestamp + dropout.duration * 1000) / 1e6
        if gap_end > gap_start:
            gaps.append(Gap(gap_start, gap_end))
    return gaps


def infer_sampling_gaps(timestamps, sample_period, tolerance=1.5):
    '''Flag gaps where consecutive samples are spaced beyond the normal period.

    `timestamps` are in seconds and `sample_period` in seconds as well.
    '''
    gaps = []
    for t_a, t_b in zip(timestamps, timestamps[1:]):
        if t_b - t_a > sample_period * tolerance:
            gaps.append(Gap(t_a, t_b))
    return gaps
