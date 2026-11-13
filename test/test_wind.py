import math

import pytest

import pynmea2
from pynmea2 import WindProcessor, process_sentences, true_wind_from_relative


def make_sentence(body):
    """Build a parseable, checksum-valid sentence from its bare body."""
    checksum = pynmea2.NMEASentence.checksum(body)
    return pynmea2.parse('$%s*%02X' % (body, checksum))


def mwv(angle, reference, speed, units='N', status='A'):
    return make_sentence(
        'WIMWV,%s,%s,%s,%s,%s' % (angle, reference, speed, units, status))


def hdt(heading):
    return make_sentence('HEHDT,%s,T' % heading)


def rmc(speed, course):
    return make_sentence(
        'GPRMC,120000,A,0.0,N,0.0,E,%s,%s,010123,,,A' % (speed, course))


def vtg(speed, track):
    return make_sentence('GPVTG,%s,T,,M,%s,N,,K,A' % (track, speed))


def vbw(lon, trans, reference='ground'):
    if reference == 'ground':
        return make_sentence('VDVBW,,,V,%s,%s,A' % (lon, trans))
    return make_sentence('VDVBW,%s,%s,A,,,V' % (lon, trans))


def vhw_water(speed):
    return make_sentence('VWVHW,,T,,M,%s,N,,K' % speed)


def osd(heading, course, speed, unit='N', speed_ref='N'):
    return make_sentence(
        'INOSD,%s,A,%s,T,%s,%s,,,%s' % (heading, course, speed, speed_ref, unit))


class TestTrueWindMath(object):
    def test_motionless_vessel(self):
        direction, speed = true_wind_from_relative(
            0.0, 10.0, heading_true=90.0,
            vessel_speed_knots=0.0, vessel_course_true=90.0)
        assert direction == pytest.approx(90.0)
        assert speed == pytest.approx(10.0)

    def test_headwind_subtracts_vessel_speed(self):
        direction, speed = true_wind_from_relative(
            0.0, 20.0, heading_true=90.0,
            vessel_speed_knots=10.0, vessel_course_true=90.0)
        assert direction == pytest.approx(90.0)
        assert speed == pytest.approx(10.0)

    def test_leeway_uses_course_not_heading(self):
        # Bow points north but the vessel makes good east: the ground
        # velocity must point along the 90 degree course.
        direction, speed = true_wind_from_relative(
            0.0, 10.0, heading_true=0.0,
            vessel_speed_knots=10.0, vessel_course_true=90.0)
        assert direction == pytest.approx(315.0)
        assert speed == pytest.approx(math.sqrt(200.0))

        wrong_direction, _ = true_wind_from_relative(
            0.0, 10.0, heading_true=0.0,
            vessel_speed_knots=10.0, vessel_course_true=0.0)
        assert wrong_direction != pytest.approx(direction)


class TestWindProcessor(object):
    def test_relative_mwv_with_heading_and_rmc_records_sources(self):
        processor = WindProcessor()
        assert processor.update(hdt(90.0)) is None
        assert processor.update(rmc(10.0, 90.0)) is None

        observation = processor.update(mwv(0.0, 'R', 20.0))

        assert observation.apparent is True
        assert observation.heading == pytest.approx(90.0)
        assert observation.heading_source == 'HEHDT'
        assert observation.speed_knots == pytest.approx(10.0)
        assert observation.speed_source == 'GPRMC'
        assert observation.speed_reference == 'ground'
        assert observation.course_true == pytest.approx(90.0)
        assert observation.course_source == 'GPRMC'
        assert observation.wind_direction == pytest.approx(90.0)
        assert observation.wind_speed_knots == pytest.approx(10.0)
        assert 'true_wind_direction' in observation.available
        assert observation.missing == []
        assert len(processor.observations) == 1

    def test_streams_update_at_different_rates(self):
        processor = WindProcessor()
        processor.update(hdt(45.0))
        processor.update(rmc(8.0, 45.0))

        first = processor.update(mwv(90.0, 'R', 12.0))
        second = processor.update(mwv(270.0, 'R', 12.0))

        # The second MWV reuses the heading and speed from the slower streams.
        assert first.heading_source == second.heading_source == 'HEHDT'
        assert first.speed_source == second.speed_source == 'GPRMC'
        assert first.apparent_wind_direction_true == pytest.approx(135.0)
        assert second.apparent_wind_direction_true == pytest.approx(315.0)

    def test_track_is_never_used_as_heading(self):
        processor = WindProcessor()
        processor.update(vtg(10.0, 90.0))
        observation = processor.update(mwv(0.0, 'R', 10.0))

        assert observation.heading is None
        assert observation.heading_source is None
        assert observation.wind_direction is None
        assert observation.wind_speed_knots is None
        assert observation.speed_source == 'GPVTG'
        assert any('leeway' in note for note in observation.notes)
        assert any('bow heading' in item for item in observation.missing)

    def test_speed_without_heading_states_what_is_available(self):
        processor = WindProcessor()
        processor.update(rmc(10.0, 90.0))
        observation = processor.update(mwv(30.0, 'R', 10.0))

        assert observation.relative_wind_angle == pytest.approx(30.0)
        assert observation.relative_wind_speed_knots == pytest.approx(10.0)
        assert observation.apparent_wind_direction_true is None
        assert observation.wind_direction is None
        assert observation.speed_knots == pytest.approx(10.0)
        assert 'apparent_wind_direction_true' not in observation.available
        assert any('track' in note and 'heading' in note
                   for note in observation.notes)

    def test_heading_without_speed_gives_apparent_bearing_only(self):
        processor = WindProcessor()
        processor.update(hdt(45.0))
        observation = processor.update(mwv(90.0, 'R', 10.0))

        assert observation.apparent_wind_direction_true == pytest.approx(135.0)
        assert observation.wind_direction is None
        assert observation.wind_speed_knots is None
        assert any('vessel speed' in item for item in observation.missing)
        assert any('true wind cannot be derived' in note
                   for note in observation.notes)

    def test_nothing_available_but_relative_observation(self):
        processor = WindProcessor()
        observation = processor.update(mwv(180.0, 'R', 10.0))

        assert observation.wind_direction is None
        assert observation.apparent_wind_direction_true is None
        assert len(observation.missing) == 2

    def test_native_true_wind_is_passed_through(self):
        processor = WindProcessor()
        observation = processor.update(mwv(200.0, 'T', 15.0))

        assert observation.apparent is False
        assert observation.wind_direction == pytest.approx(200.0)
        assert observation.wind_speed_knots == pytest.approx(15.0)
        assert observation.heading is None
        assert observation.speed_knots is None
        assert any('passed through' in note for note in observation.notes)

    def test_native_true_wind_is_not_corrected_twice(self):
        processor = WindProcessor()
        processor.update(hdt(90.0))
        processor.update(rmc(10.0, 90.0))
        observation = processor.update(mwv(200.0, 'T', 15.0))

        assert observation.wind_direction == pytest.approx(200.0)
        assert observation.wind_speed_knots == pytest.approx(15.0)
        assert observation.heading is None
        assert observation.speed_source is None

    def test_vbw_transverse_component_carries_leeway(self):
        processor = WindProcessor()
        processor.update(hdt(0.0))
        # 10 knots longitudinal, 10 knots starboard drift over ground.
        processor.update(vbw(10.0, 10.0))
        observation = processor.update(mwv(0.0, 'R', 10.0))

        assert observation.speed_source == 'VDVBW'
        assert observation.course_true == pytest.approx(45.0)
        assert observation.wind_direction == pytest.approx(270.0)
        assert observation.wind_speed_knots == pytest.approx(10.0)

    def test_scalar_water_speed_assumes_no_leeway(self):
        processor = WindProcessor()
        processor.update(hdt(45.0))
        processor.update(vhw_water(8.0))
        observation = processor.update(mwv(0.0, 'R', 8.0))

        assert observation.speed_reference == 'water'
        assert observation.course_true == pytest.approx(45.0)
        assert observation.course_source == 'HEHDT'
        assert any('no leeway' in note for note in observation.notes)
        assert any('relative to the water' in note for note in observation.notes)

    def test_osd_heading_and_course_are_kept_separate(self):
        processor = WindProcessor()
        processor.update(osd(heading=0.0, course=90.0, speed=10.0))
        observation = processor.update(mwv(0.0, 'R', 10.0))

        assert observation.heading == pytest.approx(0.0)
        assert observation.heading_source == 'INOSD'
        assert observation.course_true == pytest.approx(90.0)
        assert observation.course_source == 'INOSD'
        assert observation.wind_direction == pytest.approx(315.0)

    def test_speed_unit_conversions(self):
        processor = WindProcessor()
        processor.update(hdt(0.0))
        processor.update(rmc(0.0, 0.0))

        meters_per_second = mwv(0.0, 'R', 5.14444, units='M')
        observation = processor.update(meters_per_second)
        assert observation.relative_wind_speed_knots == pytest.approx(
            10.0, abs=1e-4)

        kmh = mwv(0.0, 'R', 18.52, units='K')
        observation = processor.update(kmh)
        assert observation.relative_wind_speed_knots == pytest.approx(
            10.0, abs=1e-4)

    def test_invalid_status_is_flagged(self):
        processor = WindProcessor()
        processor.update(hdt(0.0))
        processor.update(rmc(0.0, 0.0))
        observation = processor.update(mwv(0.0, 'R', 10.0, status='V'))

        assert any('status' in note for note in observation.notes)

    def test_bad_reference_raises(self):
        processor = WindProcessor()
        bad = make_sentence('WIMWV,0.0,X,10.0,N,A')
        with pytest.raises(ValueError):
            processor.process(bad)

    def test_missing_mwv_fields_raise(self):
        processor = WindProcessor()
        incomplete = make_sentence('WIMWV,,R,10.0,N,A')
        with pytest.raises(ValueError):
            processor.process(incomplete)

    def test_unknown_speed_unit_raises(self):
        processor = WindProcessor()
        with pytest.raises(ValueError):
            processor.update(mwv(0.0, 'R', 10.0, units='Z'))

    def test_magnetic_headings_are_not_used(self):
        processor = WindProcessor()
        processor.update(make_sentence('HCHDG,45.0,,,0.0,E'))
        processor.update(make_sentence('HCHDM,45.0,M'))
        processor.update(rmc(10.0, 45.0))
        observation = processor.update(mwv(0.0, 'R', 10.0))

        assert observation.heading is None
        assert observation.heading_source is None
        assert observation.wind_direction is None
        assert any('bow heading' in item for item in observation.missing)

    def test_invalid_ths_is_rejected(self):
        processor = WindProcessor()
        processor.update(make_sentence('INTHS,45.0,V'))
        observation = processor.update(mwv(0.0, 'R', 10.0))
        assert observation.heading is None

    def test_valid_ths_is_accepted(self):
        processor = WindProcessor()
        processor.update(make_sentence('INTHS,45.0,A'))
        processor.update(rmc(0.0, 45.0))
        observation = processor.update(mwv(0.0, 'R', 10.0))
        assert observation.heading == pytest.approx(45.0)
        assert observation.heading_source == 'INTHS'

    def test_vhw_true_heading_rotates_apparent_wind(self):
        processor = WindProcessor()
        processor.update(make_sentence('VWVHW,45.0,T,,M,8.0,N,,K'))
        observation = processor.update(mwv(90.0, 'R', 10.0))
        assert observation.heading == pytest.approx(45.0)
        assert observation.apparent_wind_direction_true == pytest.approx(135.0)

    def test_invalid_rmc_is_ignored(self):
        processor = WindProcessor()
        processor.update(hdt(45.0))
        processor.update(make_sentence(
            'GPRMC,120000,V,0.0,N,0.0,E,10.0,45.0,010123,,,N'))
        observation = processor.update(mwv(0.0, 'R', 10.0))
        assert observation.speed_source is None
        assert observation.wind_direction is None

    def test_osd_relative_course_is_not_used_as_track(self):
        processor = WindProcessor()
        processor.update(osd(heading=0.0, course=90.0, speed=10.0))
        relative = make_sentence('INOSD,0.0,A,90.0,R,10.0,N,,,N')
        processor.update(relative)
        observation = processor.update(mwv(0.0, 'R', 10.0))

        assert observation.heading == pytest.approx(0.0)
        assert observation.course_true == pytest.approx(0.0)
        assert any('no leeway' in note for note in observation.notes)

    def test_process_sentences_helper(self):
        sentences = [
            hdt(90.0),
            rmc(10.0, 90.0),
            mwv(0.0, 'R', 20.0),
            mwv(200.0, 'T', 15.0),
        ]
        processor = process_sentences(sentences)

        assert len(processor.observations) == 2
        assert processor.observations[0].wind_direction == pytest.approx(90.0)
        assert processor.observations[1].wind_direction == pytest.approx(200.0)
