import pytest

import pynmea2
from pynmea2.gsv import GSVAggregator, Satellite


def make_gsv(talker, total_pages, page_num, sats_in_view, slots):
    """Build a checksum-valid GSV sentence.

    ``slots`` is a list of up to four (prn, elevation, azimuth, snr) tuples
    of strings.
    """
    data = [str(total_pages), str(page_num), str(sats_in_view)]
    for slot in slots:
        data.extend(slot)
    return pynmea2.parse(str(pynmea2.GSV(talker, 'GSV', data)))


def feed(agg, msg, source=None):
    return agg.feed(msg, source=source)


def test_complete_three_page_cycle_out_of_order():
    agg = GSVAggregator()
    p1 = make_gsv('GP', 3, 1, 9, [
        ('12', '28', '063', '33'),
        ('14', '63', '000', '32'),
        ('22', '68', '150', '26'),
        ('25', '40', '109', '23'),
    ])
    p3 = make_gsv('GP', 3, 3, 9, [
        ('24', '03', '046', ''),
    ])
    p2 = make_gsv('GP', 3, 2, 9, [
        ('31', '42', '227', '19'),
        ('32', '17', '313', '20'),
        ('01', '09', '316', ''),
        ('11', '08', '292', ''),
    ])

    # page 1 anchors the round, then pages 3 and 2 arrive scrambled
    assert feed(agg, p1) == []
    assert feed(agg, p3) == []
    reports = feed(agg, p2)

    assert len(reports) == 1
    report = reports[0]
    assert report.complete is True
    assert report.reason is None
    assert report.missing_pages == ()
    assert report.received_pages == (1, 2, 3)
    assert report.total_pages == 3
    assert report.num_sv_in_view == 9
    assert len(report.satellites) == 9
    # pages are stitched back in page order, regardless of arrival order
    assert [s.prn for s in report.satellites] == [
        '12', '14', '22', '25', '31', '32', '01', '11', '24']
    first = report.satellites[0]
    assert first == Satellite('12', 28, 63, 33, 1, 1)
    assert report.satellites[-1].snr is None
    assert report.talker == 'GP'
    assert report.constellation == 'GPS'
    assert report.signal == 'L1 C/A'
    assert agg.reports == [report]


def test_pages_before_first_page_cannot_be_attributed_to_new_round():
    agg = GSVAggregator()
    # stream is joined mid-cycle: a page 3 shows up before any page 1
    assert feed(
        agg, make_gsv('GP', 3, 3, 9, [('24', '03', '046', '')]), 'rx') == []

    # page 1 proves a new round started; the orphan page 3 is not borrowed
    finalized = feed(
        agg, make_gsv('GP', 3, 1, 9, [('01', '10', '100', '10')]), 'rx')
    assert len(finalized) == 1
    orphan = finalized[0]
    assert orphan.complete is False
    assert orphan.received_pages == (3,)
    assert orphan.missing_pages == (1, 2)
    assert 'next round started' in orphan.reason


def test_two_receivers_and_constellations_are_kept_apart():
    agg = GSVAggregator()
    sentences = [
        ('receiver-a', make_gsv('GP', 1, 1, 1, [('01', '10', '100', '20')])),
        ('receiver-b', make_gsv('GP', 1, 1, 1, [('02', '20', '200', '30')])),
        ('receiver-a', make_gsv('GL', 1, 1, 1, [('65', '30', '300', '40')])),
    ]
    reports = []
    for source, msg in sentences:
        reports.extend(feed(agg, msg, source=source))

    assert len(reports) == 3
    keys = sorted((r.source, r.talker) for r in reports)
    assert keys == [
        ('receiver-a', 'GL'),
        ('receiver-a', 'GP'),
        ('receiver-b', 'GP'),
    ]
    by_key = {(r.source, r.talker): r for r in reports}
    assert by_key[('receiver-a', 'GP')].satellites[0].prn == '01'
    assert by_key[('receiver-b', 'GP')].satellites[0].prn == '02'
    glonass = by_key[('receiver-a', 'GL')]
    assert glonass.constellation == 'GLONASS'
    assert glonass.signal == 'L1 OF'
    assert all(r.complete for r in reports)


def test_new_round_does_not_reuse_pages_from_previous_round():
    agg = GSVAggregator()

    # round A: pages 1 and 2 of 3, then the next round starts with page 1
    feed(agg, make_gsv('GP', 3, 1, 9, [('01', '11', '100', '10')]), 'rx')
    feed(agg, make_gsv('GP', 3, 2, 9, [('02', '22', '200', '20')]), 'rx')

    round_b_p1 = make_gsv('GP', 3, 1, 9, [('11', '44', '110', '11')])
    finalized = feed(agg, round_b_p1, 'rx')

    assert len(finalized) == 1
    abandoned = finalized[0]
    assert abandoned.complete is False
    assert abandoned.missing_pages == (3,)
    assert abandoned.received_pages == (1, 2)
    assert 'next round started' in abandoned.reason

    # complete round B with its own pages; nothing is borrowed from round A
    feed(agg, make_gsv('GP', 3, 2, 3, [('12', '55', '220', '22')]), 'rx')
    reports = feed(
        agg, make_gsv('GP', 3, 3, 3, [('13', '66', '330', '33')]), 'rx')

    assert len(reports) == 1
    round_b = reports[0]
    assert round_b.complete is True
    assert [s.prn for s in round_b.satellites] == ['11', '12', '13']
    assert [s.elevation for s in round_b.satellites] == [44, 55, 66]
    assert len(agg.reports) == 2


def test_changed_retransmission_is_conflict_not_silent_merge():
    agg = GSVAggregator()
    feed(agg, make_gsv('GP', 3, 1, 5, [
        ('01', '10', '100', '10'),
        ('02', '20', '200', '20'),
    ]), 'rx')
    feed(agg, make_gsv('GP', 3, 2, 5, [
        ('03', '30', '300', '30'),
        ('04', '40', '400', '40'),
    ]), 'rx')
    # page 2 re-sent mid-round with a changed elevation for satellite 03
    feed(agg, make_gsv('GP', 3, 2, 5, [
        ('03', '35', '300', '30'),
        ('04', '40', '400', '40'),
    ]), 'rx')
    reports = feed(agg, make_gsv('GP', 3, 3, 5, [
        ('05', '50', '500', '50'),
    ]), 'rx')

    assert len(reports) == 1
    report = reports[0]
    # all pages physically arrived, but the conflicting retransmission means
    # there is not enough consistent evidence to call the round complete
    assert report.complete is False
    assert report.missing_pages == ()
    assert len(report.conflicts) == 1
    assert 'page 2' in report.conflicts[0]
    assert report.reason == report.conflicts[0]
    # the latest copy of page 2 wins
    sats = {s.prn: s for s in report.satellites}
    assert sats['03'].elevation == 35
    assert sats['04'].elevation == 40
    assert sats['05'].elevation == 50


def test_identical_retransmission_is_not_a_conflict():
    agg = GSVAggregator()
    feed(agg, make_gsv('GP', 3, 1, 3, [('01', '10', '100', '10')]), 'rx')
    page2 = make_gsv('GP', 3, 2, 3, [('02', '20', '200', '20')])
    assert feed(agg, page2, 'rx') == []
    assert feed(agg, page2, 'rx') == []
    reports = feed(
        agg, make_gsv('GP', 3, 3, 3, [('03', '30', '300', '30')]), 'rx')

    assert len(reports) == 1
    assert reports[0].complete is True
    assert reports[0].conflicts == ()
    assert len(reports[0].satellites) == 3


def test_flush_reports_incomplete_round_with_missing_pages():
    agg = GSVAggregator()
    feed(agg, make_gsv('GP', 3, 1, 9, [('01', '10', '100', '10')]), 'rx')
    feed(agg, make_gsv('GP', 3, 3, 9, [('09', '90', '900', '90')]), 'rx')

    flushed = agg.flush()
    assert len(flushed) == 1
    report = flushed[0]
    assert report.complete is False
    assert report.received_pages == (1, 3)
    assert report.missing_pages == (2,)
    assert 'input ended' in report.reason
    # the duty officer can still inspect whatever satellites were received
    assert sorted(s.prn for s in report.satellites) == ['01', '09']
    # a second flush does not resurrect the round
    assert agg.flush() == []
    assert agg.reports == [report]


def test_page_count_change_abandons_old_round():
    agg = GSVAggregator()
    feed(agg, make_gsv('GP', 3, 1, 9, [('01', '10', '100', '10')]), 'rx')
    feed(agg, make_gsv('GP', 3, 2, 9, [('02', '20', '200', '20')]), 'rx')

    # receiver now advertises a two-page round instead of three
    finalized = feed(
        agg, make_gsv('GP', 2, 1, 2, [('11', '11', '110', '11')]), 'rx')
    assert len(finalized) == 1
    assert finalized[0].complete is False
    assert finalized[0].missing_pages == (3,)
    assert 'page count changed' in finalized[0].reason

    reports = feed(
        agg, make_gsv('GP', 2, 2, 2, [('12', '22', '220', '22')]), 'rx')
    assert len(reports) == 1
    assert reports[0].complete is True
    assert [s.prn for s in reports[0].satellites] == ['11', '12']


def test_satellite_count_mismatch_is_not_complete():
    agg = GSVAggregator()
    reports = feed(agg, make_gsv('GP', 1, 1, 4, [
        ('01', '10', '100', '10'),
        ('02', '20', '200', '20'),
    ]), 'rx')

    assert len(reports) == 1
    report = reports[0]
    assert report.complete is False
    assert report.missing_pages == ()
    assert 'num_sv_in_view' in report.reason


def test_non_gsv_sentences_are_ignored():
    agg = GSVAggregator()
    gga = pynmea2.parse(
        '$GPGGA,184353.07,1929.045,S,02410.506,E,1,04,2.6,100.00,M,'
        '-33.9,M,,0000*6D')
    assert feed(agg, gga, 'rx') == []
    assert agg.flush() == []


def test_invalid_page_numbers_raise():
    agg = GSVAggregator()
    bad_count = pynmea2.parse(str(pynmea2.GSV('GP', 'GSV', ['0', '1', '1'])))
    with pytest.raises(ValueError):
        feed(agg, bad_count, 'rx')

    bad_page = pynmea2.parse(str(pynmea2.GSV('GP', 'GSV', ['2', '3', '1'])))
    with pytest.raises(ValueError):
        feed(agg, bad_page, 'rx')

    not_a_number = pynmea2.parse(str(pynmea2.GSV('GP', 'GSV', ['x', '1', '1'])))
    with pytest.raises(ValueError):
        feed(agg, not_a_number, 'rx')


def test_missing_first_page_round_is_reported_on_flush():
    agg = GSVAggregator()
    # stream joined mid-cycle: only pages 2 and 3 of 3 ever arrive
    feed(agg, make_gsv('GP', 3, 2, 9, [('02', '20', '200', '20')]), 'rx')
    flushed = agg.flush()
    assert len(flushed) == 1
    assert flushed[0].complete is False
    assert flushed[0].missing_pages == (1, 3)
    assert flushed[0].received_pages == (2,)


def test_signal_type_part_of_grouping_key():
    agg = GSVAggregator()
    gps = feed(agg, make_gsv('GP', 1, 1, 1, [('01', '10', '100', '10')]), 'rx')
    qzss = feed(agg, make_gsv('QZ', 1, 1, 1, [('193', '40', '040', '40')]), 'rx')

    assert gps[0].signal == 'L1 C/A'
    assert qzss[0].constellation == 'QZSS'
    assert qzss[0].signal == 'L1 C/A'
    assert gps[0].key != qzss[0].key
    assert len(agg.reports) == 2
