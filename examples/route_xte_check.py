"""Route-fixed XTE cross-check on one shared message stream.

Run with::

    python examples/route_xte_check.py

The same XTE sentences are read against the route in its reference order
and against the same route defined in reverse.  Reversing the definition
may only negate the directed deviation; the magnitude and the total
over-limit duration must stay identical.  Every number is also derived by
hand in test/test_route_xte.py so the output can be re-computed.
"""

import pynmea2
from pynmea2 import RouteTrack


# (time, magnitude in NM, correction L/R, physical active travel leg)
STREAM = [
    (0,  0.10, 'L', [('A', 'B')]),
    (10, 0.30, 'L', [('A', 'B')]),
    (20, 0.30, 'L', [('A', 'B'), ('B', 'C')]),  # waypoint handoff
    (30, 0.30, 'R', [('B', 'C')]),
    (40, 0.05, 'R', [('B', 'C')]),
]
THRESHOLD = 0.20


def xte(magnitude, direction):
    return pynmea2.parse(str(pynmea2.XTE(
        'GP', 'XTE', ('V', 'N', '%.2f' % magnitude, direction, 'N'))))


def ingest(route):
    for time, magnitude, direction, active in STREAM:
        route.observe(xte(magnitude, direction), time=time, active=active)


def render(route):
    rows = []
    for obs in route.observations:
        if obs.ambiguous:
            signed = 'AMBIGUOUS(%s)' % ', '.join(
                '%s=%+.2f' % (candidate.leg.dest, candidate.signed)
                for candidate in obs.candidates)
        else:
            signed = '%+.2f' % obs.signed
        rows.append((obs.time, obs.magnitude, obs.correction_dir, signed))
    return rows


def main():
    forward = RouteTrack(['A', 'B', 'C'])
    reversed_route = forward.reversed()
    ingest(forward)
    ingest(reversed_route)

    header = '%4s %8s %8s | %-22s | %-22s' % (
        't', 'mag', 'corr', 'route A-B-C', 'route C-B-A')
    print(header)
    print('-' * len(header))
    for (t, mag, corr, fwd), (_, _, _, rev) in zip(
            render(forward), render(reversed_route)):
        print('%4d %8.2f %8s | %-22s | %-22s' % (t, mag, corr, fwd, rev))

    fwd_duration = forward.exceedance_duration(THRESHOLD)
    rev_duration = reversed_route.exceedance_duration(THRESHOLD)
    print()
    print('over-limit threshold     : %.2f NM' % THRESHOLD)
    print('over-limit intervals fwd : %s' %
          forward.exceedance_intervals(THRESHOLD))
    print('over-limit intervals rev : %s' %
          reversed_route.exceedance_intervals(THRESHOLD))
    print('total over-limit time    : %s == %s -> %s' % (
        fwd_duration, rev_duration,
        'INVARIANT HOLDS' if fwd_duration == rev_duration else 'MISMATCH'))


if __name__ == '__main__':
    main()
