"""
Compass deviation table audit example.

The deviation table is keyed by magnetic heading band (here 15 deg steps),
using signed angles: east deviation/variation is positive, west is negative.
The NMEA log carries HDG (magnetic heading + deviation E/W + variation E/W)
and HDT (reference true heading); ZDA/RMC supply the timestamps that bound
the reference time window.

Run:  python examples/compass_audit.py
"""
import functools
import operator

import pynmea2
from pynmea2 import compass


def checksum(body):
    return functools.reduce(operator.xor, map(ord, body), 0)


def sentence(body):
    return '$%s*%02X' % (body, checksum(body))


def zda(second):
    h, m, s = 12 + second // 3600, (second % 3600) // 60, second % 60
    return sentence('GPZDA,%02d%02d%02d,14,11,2026,00,00' % (h, m, s))


def hdg(magnetic, deviation, dev_dir, variation, var_dir):
    return sentence('HCHDG,%.1f,%.1f,%s,%.1f,%s' % (
        magnetic, abs(deviation), dev_dir, abs(variation), var_dir))


def hdt(true_heading):
    return sentence('HCHDT,%.1f,T' % true_heading)


def block(second, magnetic, deviation, dev_dir, variation, var_dir, true):
    return [zda(second),
            hdg(magnetic, deviation, dev_dir, variation, var_dir),
            hdt(true_heading=true)]


# variation fixed at 5W; true = magnetic + deviation + variation
log = (
    # 090 band: table value correct
    block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
    # 180 band: real deviation 13E, but the table was written 13W (sign flip)
    + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    # 000 band straddles the 359 -> 1 seam, two independent 1:1 samples
    + block(20, 359.5, 2.0, 'E', 0.0, 'E', 1.5)
    + block(30, 0.5, 2.0, 'E', 0.0, 'E', 2.5)
    # 270 band: turn in progress, two references inside the time window
    + [zda(40), hdg(270.0, 7.0, 'E', 5.0, 'W'), hdt(272.0), hdt(278.0)]
)

messages = [pynmea2.parse(line) for line in log]

# signed deviation table: {band centre: deviation}, E positive / W negative
table = {0: 2.0, 90: 7.0, 180: -13.0, 270: 7.0}

audit = compass.audit_sentences(
    messages, table, step=15,
    max_time_gap=5.0,     # references only valid within this time window
    tolerance=2.0,        # per-band pass/fail tolerance
    spread_tolerance=2.0) # turn-induced residual spread tolerance

print(compass.format_report(audit))
