import datetime

import pytest

import pynmea2


def gga(timestamp, lat, lon, quality=1, talker='GP'):
    return pynmea2.GGA(talker, 'GGA', (
        timestamp, lat, 'N', lon, 'E', str(quality), '08', '0.9',
        '545.4', 'M', '46.9', 'M', '', '0000'))


def reception(timestamp, lat, lon, forwarder, quality=1):
    return pynmea2.Reception(
        gga(timestamp, lat, lon, quality), forwarder, station='BASE')


def test_quality_flags_do_not_prevent_confirmed_retransmission():
    left = reception('120000.00', '4807.038', '01131.000', 'A', quality=1)
    right = reception('120001.00', '4807.040', '01131.002', 'B', quality=4)

    result = pynmea2.match_retransmissions(
        [left, right], position_error=10.0, reception_delay=2.0)

    assert (left.quality, right.quality) == (1, 4)
    assert not result.is_ambiguous
    assert result.fix_count == 1
    assert len(result.retransmissions) == 1
    fix = result.retransmissions[0]
    assert fix.forwarders == ('A', 'B')
    assert fix.receptions == (left, right)


def test_two_real_fixes_in_the_same_second_are_not_merged():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.100', '01131.000', 'A')]
    right = [reception('120000.00', '4807.038', '01131.000', 'B'),
             reception('120000.00', '4807.100', '01131.000', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=5.0, reception_delay=0.0)

    assert not result.is_ambiguous
    assert len(result.receptions) == 4
    assert result.fix_count == 2
    assert [fix.forwarders for fix in result.fixes] == [('A', 'B'), ('A', 'B')]
    assert [fix.receptions[0].latitude for fix in result.fixes] == [
        pytest.approx(48.1173), pytest.approx(48.11833333333333)]


def test_pairwise_near_records_can_still_be_two_or_three_fixes():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.038', '01131.026', 'A')]
    right = [reception('120000.00', '4807.038', '01131.026', 'B'),
             reception('120000.00', '4807.038', '01131.060', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=50.0, reception_delay=0.0)

    assert result.is_ambiguous
    assert result.fix_count is None
    assert len(result.fixes) == 0
    assert len(result.ambiguous_groups) == 1

    group = result.ambiguous_groups[0]
    assert len(group.records) == 4
    assert group.distinct_fix_counts == [2, 3]
    assert sorted(option.matched_count for option in group.options) == [1, 2]


def test_a_full_bipartite_square_has_two_pairings_of_equal_size():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.050', '01131.000', 'A')]
    right = [reception('120000.00', '4807.038', '01131.026', 'B'),
             reception('120000.00', '4807.050', '01131.026', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=50.0, reception_delay=0.0)

    group = result.ambiguous_groups[0]
    assert group.distinct_fix_counts == [2]
    assert len(group.options) == 2


def test_three_pairwise_near_records_do_not_become_one_fix():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.050', '01131.000', 'A')]
    right = [reception('120000.00', '4807.038', '01131.000', 'B'),
             reception('120000.00', '4807.050', '01131.000', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=30.0, reception_delay=0.0)

    assert result.is_ambiguous
    group = result.ambiguous_groups[0]
    assert group.distinct_fix_counts == [2]
    assert len(group.options) == 2
    for option in group.options:
        assert option.fix_count == 2


def test_ambiguous_group_lists_each_correspondence_and_fix_count():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.050', '01131.000', 'A')]
    right = [reception('120000.00', '4807.038', '01131.000', 'B'),
             reception('120000.00', '4807.050', '01131.000', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=30.0, reception_delay=0.0)

    assert result.is_ambiguous
    group = result.ambiguous_groups[0]
    assert group.distinct_fix_counts == [2]
    assert len(group.options) == 2
    for option in group.options:
        assert option.matched_count == 2
        assert len(option.fixes) == option.fix_count == 2
        assert sum(len(fix) for fix in option.fixes) == len(group.records)
    assert group.shared_pairs == ()


def test_minimal_group_has_one_pair_and_one_singleton_per_option():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.050', '01131.000', 'A')]
    right = [reception('120000.00', '4807.038', '01131.000', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=30.0, reception_delay=0.0)

    group = result.ambiguous_groups[0]
    assert group.distinct_fix_counts == [2]
    assert len(group.options) == 2
    assert all(option.matched_count == 1 for option in group.options)
    assert all(option.fix_count == 2 for option in group.options)


def test_out_of_tolerance_records_remain_separate():
    left = reception('120000.00', '4807.038', '01131.000', 'A')
    right = reception('120005.00', '4807.038', '01131.000', 'B')

    result = pynmea2.match_retransmissions(
        [left, right], position_error=10.0, reception_delay=2.0)

    assert not result.is_ambiguous
    assert result.fix_count == 2
    assert result.retransmissions == ()
    assert len(result.singletons) == 2


def test_position_tolerance_is_inclusive_and_uses_metres():
    left = reception('120000.00', '4807.038', '01131.000', 'A')
    right = pynmea2.Reception(
        gga('120000.00', '4807.038', '01131.008'), 'B', station='BASE')

    inside = pynmea2.match_retransmissions(
        [left], position_error=0.0)
    assert inside.fix_count == 1

    assert pynmea2.match_retransmissions(
        [left, right], position_error=10.0).retransmissions
    assert not pynmea2.match_retransmissions(
        [left, right], position_error=9.5).retransmissions


def test_reception_delta_ignores_subseconds_and_handles_midnight():
    left = reception('235959.90', '4807.038', '01131.000', 'A')
    right = reception('000000.10', '4807.038', '01131.000', 'B')

    result = pynmea2.match_retransmissions(
        [left, right], position_error=10.0, reception_delay=1.0)

    assert len(result.retransmissions) == 1
    assert left.seconds_of_day == 86399
    assert right.seconds_of_day == 0


def test_dated_receptions_do_not_match_across_days():
    day_one = pynmea2.Reception(
        gga('235959.00', '4807.038', '01131.000'), 'A', station='BASE',
        date=datetime.date(2026, 11, 13))
    day_two = pynmea2.Reception(
        gga('000000.00', '4807.038', '01131.000'), 'B', station='BASE',
        date=datetime.date(2026, 11, 14))

    result = pynmea2.match_retransmissions(
        [day_one, day_two], position_error=10.0, reception_delay=1.0)

    assert result.retransmissions == ()
    assert result.fix_count == 2


def test_station_mismatch_is_never_a_retransmission():
    left = pynmea2.Reception(
        gga('120000.00', '4807.038', '01131.000'), 'A', station='BASE-1')
    right = pynmea2.Reception(
        gga('120000.00', '4807.038', '01131.000'), 'B', station='BASE-2')

    result = pynmea2.match_retransmissions(
        [left, right], position_error=10.0, reception_delay=1.0)

    assert result.retransmissions == ()


def test_raw_sentence_is_kept_and_can_be_parsed():
    raw = str(gga('120000.00', '4807.038', '01131.000', quality=4))
    record = pynmea2.Reception(raw, 'A', station='BASE')

    assert record.raw == raw
    assert record.quality == 4
    assert record.sentence.gps_qual == 4


def test_reception_without_position_is_rejected():
    empty = pynmea2.GGA('GP', 'GGA', (
        '120000.00', '', '', '', '', '0', '00', '', '', 'M', '', 'M',
        '', ''))
    with pytest.raises(ValueError):
        pynmea2.Reception(empty, 'A')


def test_three_forwarders_are_rejected():
    records = [reception('120000.00', '4807.038', '01131.000', name)
               for name in ('A', 'B', 'C')]
    with pytest.raises(ValueError):
        pynmea2.match_retransmissions(records, position_error=10.0)


def test_the_same_reception_object_cannot_be_counted_twice():
    record = reception('120000.00', '4807.038', '01131.000', 'A')
    with pytest.raises(ValueError):
        pynmea2.match_retransmissions(
            [record, record], position_error=10.0)


def test_ambiguity_enumeration_can_be_bounded():
    left = [reception('120000.00', '4807.038', '01131.000', 'A'),
            reception('120000.00', '4807.050', '01131.000', 'A')]
    right = [reception('120000.00', '4807.038', '01131.026', 'B'),
             reception('120000.00', '4807.050', '01131.026', 'B')]

    result = pynmea2.match_retransmissions(
        left + right, position_error=50.0, reception_delay=0.0,
        max_ambiguity_options=1)

    group = result.ambiguous_groups[0]
    assert group.truncated
    assert len(group.options) == 1
    assert group.shared_pairs is None
