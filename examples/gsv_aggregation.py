"""
Reassemble GSV "satellites in view" rounds from two receivers on one bus.

Each GSV sentence is only one page of a receiver's visible-satellite list.
This example multiplexes two physical inputs (``A`` and ``B``), feeds every
sentence to a GSVAggregator and shows how the duty officer can tell a
complete round from an incomplete one and which page is missing.

Run with:  python examples/gsv_aggregation.py
"""
import pynmea2
from pynmea2.gsv import GSVAggregator


def gsv(talker, total_pages, page_num, sats_in_view, slots):
    data = [str(total_pages), str(page_num), str(sats_in_view)]
    for prn, elevation, azimuth, snr in slots:
        data.extend([prn, elevation, azimuth, snr])
    return str(pynmea2.GSV(talker, 'GSV', data))


# (physical input, raw NMEA sentence).  The two receivers are interleaved,
# receiver A loses page 2 of one round and later re-sends a changed page.
bus = [
    # receiver A, round 1 (two pages, five satellites)
    ('A', gsv('GP', 2, 1, 5, [
        ('01', '10', '100', '20'), ('02', '20', '200', '30'),
        ('03', '30', '300', '40'), ('04', '40', '040', '50'),
    ])),
    # receiver B, round 1 starts in between
    ('B', gsv('GL', 2, 1, 5, [
        ('65', '11', '110', '21'), ('66', '22', '220', '31'),
        ('67', '33', '330', '41'), ('68', '44', '044', '51'),
    ])),
    ('A', gsv('GP', 2, 2, 5, [('05', '50', '050', '60')])),
    ('B', gsv('GL', 2, 2, 5, [('69', '55', '055', '61')])),

    # receiver A, round 2 (three pages): only page 1 arrives, the rest is lost
    ('A', gsv('GP', 3, 1, 10, [
        ('01', '12', '100', '22'), ('02', '22', '200', '32'),
        ('03', '32', '300', '42'), ('04', '42', '040', '52'),
    ])),
    # the next page 1 starts round 3, closing round 2 with its pages missing
    ('A', gsv('GP', 3, 1, 6, [
        ('01', '13', '100', '23'), ('02', '23', '200', '33'),
    ])),
    # receiver A, round 3: page 2 arrives, then again with a new elevation
    ('A', gsv('GP', 3, 2, 6, [
        ('03', '33', '300', '43'), ('04', '43', '040', '53'),
    ])),
    ('A', gsv('GP', 3, 2, 6, [
        ('03', '37', '300', '43'), ('04', '43', '040', '53'),
    ])),
    ('A', gsv('GP', 3, 3, 6, [
        ('05', '53', '050', '63'), ('06', '63', '060', '73'),
    ])),
]


def show(report):
    status = 'complete' if report.complete else 'INCOMPLETE'
    print('[{status}] input={source} {constellation} ({signal}), '
          'pages {got}/{total}'.format(
              status=status, source=report.source,
              constellation=report.constellation, signal=report.signal,
              got=len(report.received_pages), total=report.total_pages))
    if report.missing_pages:
        print('  missing page(s): {}'.format(
            ', '.join(str(p) for p in report.missing_pages)))
    for conflict in report.conflicts:
        print('  conflict: {}'.format(conflict))
    if report.reason and not report.conflicts:
        print('  reason: {}'.format(report.reason))
    print('  satellites: {}'.format(
        ', '.join('{0}@{1}deg'.format(s.prn, s.elevation)
                  for s in report.satellites)))


aggregator = GSVAggregator()
for source, line in bus:
    try:
        msg = pynmea2.parse(line)
    except pynmea2.ParseError as exc:
        print('Parse error: {}'.format(exc))
        continue
    for report in aggregator.feed(msg, source=source):
        show(report)

# end of input: hand over any round that never finished
for report in aggregator.flush():
    show(report)
