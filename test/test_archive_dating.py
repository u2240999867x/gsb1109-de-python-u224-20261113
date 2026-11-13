import datetime

import pytest

import pynmea2
from pynmea2.archive import (
    DateConflict,
    DateResolution,
    DateResolver,
    VoyageResult,
)

D = datetime.date(2026, 3, 10)
DAY = datetime.timedelta(days=1)
FIVE_MIN = datetime.timedelta(minutes=5)


def gga(time_text):
    return pynmea2.parse(
        '$GPGGA,%s,3926.276,N,07739.361,W,1,04,2.6,100.0,M,-33.9,M,,'
        % time_text)


def rmc(time_text, date_text):
    return pynmea2.parse(
        '$GPRMC,%s,A,3926.276,N,07739.361,W,0.0,0.0,%s,,E'
        % (time_text, date_text))


def zda(time_text, date_text):
    day, month, year = date_text[:2], date_text[2:4], '20' + date_text[4:6]
    return pynmea2.parse(
        '$GPZDA,%s,%s,%s,%s,00,00' % (time_text, day, month, year))


def dates(resolutions):
    return [list(r.possible_dates) for r in resolutions]


def test_late_evening_record_is_not_pushed_to_next_day():
    # 00:01 arrives first, the 23:59 record is late; with strict ordering
    # both must stay on the day before the RMC anchor.
    voyage = [gga('000100'), gga('235900'), rmc('000200', '100326')]
    resolutions = DateResolver().resolve(voyage)

    assert dates(resolutions) == [
        [D - DAY],
        [D - DAY],
        [D],
    ]
    assert resolutions[1].date == D - DAY
    assert resolutions[2].is_anchor is True
    assert resolutions[0].is_anchor is False


def test_midnight_rollover_advances_the_date():
    voyage = [gga('235800'), gga('000100'), rmc('000200', '100326')]
    resolutions = DateResolver().resolve(voyage)

    assert dates(resolutions) == [
        [D - DAY],
        [D],
        [D],
    ]


def test_bounded_delay_keeps_genuinely_ambiguous_dates():
    # The 23:59 record is 4 minutes late, inside a 5 minute window, so the
    # 00:03 record could be the previous evening or the anchor day.
    voyage = [gga('000300'), gga('235900'), rmc('000400', '100326')]

    tolerant = DateResolver(FIVE_MIN).resolve(voyage)
    assert dates(tolerant) == [
        [D - DAY, D],
        [D - DAY],
        [D],
    ]

    strict = DateResolver().resolve(voyage)
    assert strict[0].possible_dates == [D - DAY]


def test_tail_after_anchor_handles_midnight():
    strict = DateResolver().resolve(
        [rmc('235800', '100326'), gga('000100'), gga('000200')])
    assert dates(strict) == [[D], [D + DAY], [D + DAY]]

    tolerant = DateResolver(FIVE_MIN).resolve(
        [rmc('000400', '100326'), gga('235900'), gga('000500')])
    assert dates(tolerant) == [[D], [D], [D + DAY]]


def test_zda_sentence_is_a_date_anchor():
    resolutions = DateResolver().resolve(
        [gga('120000'), zda('120100', '100326')])
    assert dates(resolutions) == [[D], [D]]
    assert resolutions[1].is_anchor is True


def test_rmc_without_datestamp_is_not_an_anchor():
    no_date = pynmea2.parse(
        '$GPRMC,120000.00,A,3926.276,N,07739.361,W,0.0,0.0,,,E*00',
        checksums='my_data_is_corrupt')
    assert no_date.datestamp is None

    resolutions, conflicts = DateResolver().inspect([gga('120000'), no_date])
    assert conflicts == []
    assert all(r.possible_dates == [] for r in resolutions)


def test_voyage_without_any_anchor_cannot_be_dated():
    resolutions = DateResolver().resolve([gga('120000'), gga('120100')])
    assert all(r.possible_dates == [] for r in resolutions)
    assert all(r.date is None for r in resolutions)


def test_contradictory_anchors_raise_conflict_with_raw_segment():
    voyage = [
        rmc('120000', '100326'),
        gga('120500'),
        rmc('121000', '090326'),
    ]

    with pytest.raises(DateConflict) as excinfo:
        DateResolver().resolve(voyage)
    conflict = excinfo.value
    assert conflict.start_index == 0
    assert conflict.end_index == 2
    assert len(conflict.sentences) == 3
    assert all(sentence.startswith('$GP') for sentence in conflict.sentences)
    assert '100326' in conflict.sentences[0]
    assert '090326' in conflict.sentences[2]

    # In non-raising mode the anchors themselves stay factual; only the
    # time-only record between them loses its date.
    resolutions, conflicts = DateResolver().inspect(voyage)
    assert len(conflicts) == 1
    assert resolutions[0].possible_dates == [D]
    assert resolutions[1].possible_dates == []
    assert resolutions[2].possible_dates == [D - DAY]


def test_inspect_confines_conflict_to_its_segment():
    voyage = [
        rmc('100000', '100326'),
        rmc('100100', '090326'),
        rmc('100200', '110326'),
        gga('100300'),
        rmc('100400', '110326'),
    ]
    resolutions, conflicts = DateResolver().inspect(voyage)

    assert len(conflicts) == 2
    # Every anchor keeps its own stated date even where joins are broken ...
    assert resolutions[0].possible_dates == [D]
    assert resolutions[1].possible_dates == [D - DAY]
    assert resolutions[2].possible_dates == [D + DAY]
    # ... while the later, consistent segment is still archived.
    assert dates(resolutions[3:]) == [
        [D + DAY],
        [D + DAY],
    ]


def test_other_voyages_are_archived_when_one_voyage_conflicts():
    good = [gga('120000'), rmc('120100', '100326')]
    bad = [rmc('120000', '100326'), rmc('121000', '090326')]

    results = DateResolver().resolve_voyages([
        ('voyage-1', good),
        ('voyage-2', bad),
        ('voyage-3', good),
    ])

    assert all(isinstance(result, VoyageResult) for result in results)
    assert [result.ok for result in results] == [True, False, True]
    assert results[0].voyage_id == 'voyage-1'
    assert len(results[1].conflicts) == 1
    assert dates(results[2].resolutions) == [[D], [D]]


def test_sentences_without_timestamps_are_skipped_but_kept():
    route = pynmea2.RTE(
        'GP', 'RTE', ('2', '1', 'c', '0', 'PBRCPK', 'PBRTO'))
    voyage = [gga('120000'), route, rmc('120100', '100326')]

    resolutions, conflicts = DateResolver().inspect(voyage)
    assert conflicts == []
    assert resolutions[0].possible_dates == [D]
    assert resolutions[1].possible_dates == []
    assert resolutions[1].sentence is route
    assert resolutions[2].possible_dates == [D]


def test_raw_string_and_bytes_inputs_are_parsed():
    anchor = rmc('120100', '100326')
    raw_voyage = [gga('120000').render(), anchor.render()]
    resolutions = DateResolver().resolve(raw_voyage)
    assert dates(resolutions) == [[D], [D]]

    bytes_voyage = [sentence.encode('ascii') for sentence in raw_voyage]
    resolutions = DateResolver().resolve(bytes_voyage)
    assert dates(resolutions) == [[D], [D]]
    assert resolutions[0].raw == raw_voyage[0]


def test_resolution_datetime_helper():
    resolutions = DateResolver().resolve(
        [gga('120000.50'), rmc('120100', '100326')])
    assert resolutions[0].datetime == datetime.datetime(
        2026, 3, 10, 12, 0, 0, 500000, tzinfo=datetime.timezone.utc)
    assert resolutions[0].datetime.tzinfo is datetime.timezone.utc


def test_two_day_span_between_anchors_resolves_interior():
    voyage = [
        rmc('120000', '100326'),
        gga('235900'),
        gga('000100'),
        rmc('120000', '110326'),
    ]
    resolutions = DateResolver(FIVE_MIN).resolve(voyage)
    assert dates(resolutions) == [
        [D],
        [D],
        [D + DAY],
        [D + DAY],
    ]


def test_max_delay_accepts_seconds_and_validates_bounds():
    assert DateResolver(300).max_delay == FIVE_MIN
    assert DateResolver(datetime.timedelta(0)).max_delay == \
        datetime.timedelta(0)

    for invalid in (-1, 86400, datetime.timedelta(hours=24)):
        with pytest.raises(ValueError):
            DateResolver(invalid)


def test_resolution_repr_and_conflict_repr_are_informative():
    resolutions = DateResolver().resolve(
        [gga('120000'), rmc('120100', '100326')])
    assert 'DateResolution' in repr(resolutions[0])

    _, conflicts = DateResolver().inspect(
        [rmc('120000', '100326'), rmc('121000', '090326')])
    assert 'DateConflict' in repr(conflicts[0])


def test_top_level_package_exports():
    assert pynmea2.DateResolver is DateResolver
    assert pynmea2.DateConflict is DateConflict
    assert pynmea2.DateResolution is DateResolution
    assert pynmea2.VoyageResult is VoyageResult
