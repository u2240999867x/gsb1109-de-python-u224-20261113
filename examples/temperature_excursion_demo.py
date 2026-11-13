#!/usr/bin/env python
'''
Worked example: bound the cumulative over-threshold duration of a thermal
plate controller log that contains two ULog dropout gaps and several
threshold crossings.

Units: time in seconds, temperature in deg C.

    experiment window : [0, 30]
    nominal sampling  : 1 s
    threshold         : 50 deg C (strictly above counts)
    rate limit        : 2 deg C/s on [0, 15), 1 deg C/s on [15, 30]
    dropout gaps      : [10, 13] (3 s), [20, 23] (3 s)

Run with:  python examples/temperature_excursion_demo.py
'''

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyulog.threshold_excursion import (  # noqa: E402
    Sample, RateSegment, Gap, analyze, envelope_paths)


THRESHOLD = 50.0
T_START, T_END = 0.0, 30.0
RATE_SEGMENTS = [
    RateSegment(0.0, 15.0, 2.0),
    RateSegment(15.0, None, 1.0),
]
DROPOUT_GAPS = [Gap(10.0, 13.0), Gap(20.0, 23.0)]

# (t, temperature) samples; the interiors of the two dropout gaps are simply
# absent. Every consecutive pair is feasible under the rate budget.
SAMPLES = [
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


def main():
    '''Run the analysis and print the bounded excursion report.'''
    result = analyze(SAMPLES, THRESHOLD, RATE_SEGMENTS,
                     T_START, T_END, DROPOUT_GAPS)

    print('Experiment window: [{}, {}] s, threshold: {} deg C'.format(
        T_START, T_END, THRESHOLD))
    print('Dropout gaps (kept, not deleted): {}'.format(
        [(g.t_start, g.t_end) for g in DROPOUT_GAPS]))
    print()
    print('Cumulative over-threshold duration bounds')
    print('  lower bound (certain): {:.3f} s'.format(result.certain_duration))
    print('  upper bound (possible): {:.3f} s'.format(
        result.possible_duration))
    print()
    print('Certainly over threshold (every admissible trajectory is above):')
    for iv in result.certain_intervals:
        print('  [{:5.2f}, {:5.2f}]  ({:.2f} s)'.format(
            iv.t_start, iv.t_end, iv.t_end - iv.t_start))
    print()
    print('Only possibly over threshold (ambiguous, gap-constrained):')
    for iv in result.ambiguous_intervals:
        print('  [{:5.2f}, {:5.2f}]  ({:.2f} s)'.format(
            iv.t_start, iv.t_end, iv.t_end - iv.t_start))
    print()
    print('Per-segment assessment:')
    for item in result.assessments:
        print('  [{:5.2f}, {:5.2f}] {:11s} budget={:4.1f} '
              'certain={:.2f}s possible={:.2f}s'.format(
                  item.t_start, item.t_end, item.kind, item.rate_budget,
                  sum(i.t_end - i.t_start for i in item.certain_intervals),
                  sum(i.t_end - i.t_start for i in item.possible_intervals)))

    # Extremal trajectories: both pass through every sample and respect the
    # rate limits, but realize the two different durations.
    path = envelope_paths(
        SAMPLES, RATE_SEGMENTS, T_START, T_END, num_points=300001)

    def measure(component):
        values = getattr(path, component)
        total = 0.0
        for i in range(len(path.t) - 1):
            if 0.5 * (values[i] + values[i + 1]) > THRESHOLD:
                total += path.t[i + 1] - path.t[i]
        return total

    lower_measured = measure('lower')
    upper_measured = measure('upper')
    print()
    print('Two admissible trajectories through the identical samples:')
    print('  lowest feasible path  -> {:.3f} s above threshold'.format(
        lower_measured))
    print('  highest feasible path -> {:.3f} s above threshold'.format(
        upper_measured))

    print()
    print('Gap [10, 13] explicitly: endpoints (10, 47.0) and (13, 52.0), '
          'rate <= 2 deg C/s')
    print('  early-crossing path: rise at 2 deg C/s, hit 50 at t=11.50, '
          'reach 52 at t=12.50 and hold -> 1.50 s above')
    print('  late-crossing path : rise at 1.5 deg C/s to t=12, hit 50 at '
          't=12.00, then rise to 52 -> 1.00 s above')
    print('  difference: 0.50 s, exactly the ambiguous slice [11.5, 12.0].')
    print()
    print('Gap [20, 23] explicitly: endpoints (20, 48.0) and (23, 50.5), '
          'rate <= 1 deg C/s')
    print('  early-crossing path: rise at 1 deg C/s from t=20, hit 50 at '
          't=22.00, hold at 50.50 -> 1.00 s above')
    print('  late-crossing path : hold 48 until t=20.50, then rise at 1 '
          'deg C/s, hit 50 at t=22.50 -> 0.50 s above')
    print('  difference: 0.50 s, exactly the ambiguous slice [22.0, 22.5].')


if __name__ == '__main__':
    main()
