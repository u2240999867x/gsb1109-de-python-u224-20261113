# SPDX-FileCopyrightText: 2026 Mido contributors
#
# SPDX-License-Identifier: MIT

from fractions import Fraction

import pytest

from mido.midifiles.timecode import (
    DroppedFrameError,
    InvalidTimecodeError,
    Reel,
    ReelTable,
    Timecode,
    TimecodeGapError,
    TimecodeTable,
    drop_frames_per_minute,
    format_timecode,
    frame_rate_fraction,
    frames_per_day,
    frames_to_smpte_offset,
    frames_to_timecode,
    is_drop_frame_rate,
    midnight_wrap,
    nominal_frame_rate,
    parse_timecode,
    seconds_to_timecode,
    smpte_offset_to_frames,
    timecode_to_frames,
    timecode_to_seconds,
)
from mido.midifiles.meta import MetaMessage


class TestFrameRate:
    def test_ntsc_names_are_exact_rationals(self):
        assert frame_rate_fraction('29.97') == Fraction(30000, 1001)
        assert frame_rate_fraction('23.976') == Fraction(24000, 1001)
        assert frame_rate_fraction('59.94') == Fraction(60000, 1001)

    def test_decimal_approximations_match_ntsc(self):
        # 29.97, the full repeating decimal and the rational all agree.
        for value in ['29.97', 29.97, 29.97002997002997,
                      Fraction(30000, 1001)]:
            assert frame_rate_fraction(value) == Fraction(30000, 1001)

    def test_whole_rates(self):
        assert frame_rate_fraction('24') == 24
        assert frame_rate_fraction(25) == 25
        assert nominal_frame_rate(24) == 24
        assert nominal_frame_rate('29.97') == 30
        assert nominal_frame_rate('59.94') == 60

    def test_drop_frame_detection(self):
        assert is_drop_frame_rate('29.97')
        assert is_drop_frame_rate('59.94')
        assert not is_drop_frame_rate('30')
        assert not is_drop_frame_rate('24')

    def test_rejects_bad_rates(self):
        with pytest.raises(ValueError):
            frame_rate_fraction(0)
        with pytest.raises(ValueError):
            frame_rate_fraction('not-a-rate')
        with pytest.raises(ValueError):
            nominal_frame_rate(Fraction(12, 5))


class TestDropFrameArithmetic:
    def test_labels_at_minute_boundary(self):
        # The last label before minute 1 and the first one after it.
        assert timecode_to_frames('00:00:59;29', 29.97) == 1799
        assert timecode_to_frames('00:01:00;02', 29.97) == 1800

    def test_ten_minute_boundary_keeps_labels(self):
        # At minute 10 no labels are dropped, so the count is exact.
        assert timecode_to_frames('00:10:00;00', 29.97) == 30 * 600 - 2 * 9
        assert frames_to_timecode(17982, 29.97) == (0, 10, 0, 0)

    def test_hour_frame_count(self):
        assert timecode_to_frames('01:00:00;00', 29.97) == 107892
        assert frames_to_timecode(107892, 29.97) == (1, 0, 0, 0)

    def test_dropped_labels_are_rejected_with_position(self):
        for label in ['00:01:00;00', '00:01:00;01']:
            with pytest.raises(DroppedFrameError) as excinfo:
                timecode_to_frames(label, 29.97)
            error = excinfo.value
            assert error.parts == parse_timecode(label)
            assert error.label == label.replace(';', ':')
            assert '00:01:00' in str(error)

    def test_tenth_minute_does_not_drop(self):
        assert frames_to_timecode(17982, 29.97) == (0, 10, 0, 0)
        assert timecode_to_frames('00:10:00;00', 29.97) == 17982

    def test_59_94_drops_four(self):
        assert drop_frames_per_minute('59.94') == 4
        assert frames_to_timecode(3600, '59.94') == (0, 1, 0, 4)
        for label in ['00:01:00;00', '00:01:00;03']:
            with pytest.raises(DroppedFrameError):
                timecode_to_frames(label, '59.94')

    def test_non_drop_at_29_97(self):
        assert frames_to_timecode(1800, '29.97', drop_frame=False) == (
            0, 1, 0, 0)
        assert timecode_to_frames('00:01:00;00', '29.97',
                                  drop_frame=False) == 1800

    def test_frames_per_day(self):
        # 24 hours minus 2 frames for each of the 9 dropped minutes/hour.
        assert frames_per_day('29.97') == 30 * 86400 - 2 * 9 * 144
        assert frames_per_day('30') == 30 * 86400
        assert frames_per_day('24') == 24 * 86400

    def test_every_existing_drop_frame_label_round_trips(self):
        day = frames_per_day('29.97')
        for frame in range(0, day, 7919):  # a prime stride
            label = frames_to_timecode(frame, '29.97')
            assert timecode_to_frames(label, '29.97') == frame

    def test_whole_day_round_trip(self):
        for frame_rate in ['24', '25', '29.97', '30', '59.94']:
            day = frames_per_day(frame_rate)
            for frame in [0, 1, day // 2, day - 1]:
                label = frames_to_timecode(frame, frame_rate)
                assert timecode_to_frames(label, frame_rate) == frame


class TestMidnight:
    def test_wrap_at_midnight(self):
        assert timecode_to_frames('23:59:59;29', '29.97') == (
            frames_per_day('29.97') - 1)
        assert frames_to_timecode(frames_per_day('29.97'), '29.97') == (
            0, 0, 0, 0)
        assert frames_to_timecode(frames_per_day('29.97') + 7, '29.97') == (
            0, 0, 0, 7)

    def test_wrap_disabled_keeps_counting_hours(self):
        day = frames_per_day('29.97')
        assert frames_to_timecode(day + 7, '29.97', wrap=False) == (
            24, 0, 0, 7)

    def test_midnight_wrap_helper(self):
        assert midnight_wrap(-1, '29.97') == frames_per_day('29.97') - 1
        assert midnight_wrap(0, '29.97') == 0

    def test_negative_frames_wrap(self):
        assert frames_to_timecode(-1, '30') == (23, 59, 59, 29)
        with pytest.raises(ValueError):
            frames_to_timecode(-1, '30', wrap=False)


class TestLabels:
    def test_parse_formats(self):
        assert parse_timecode('01:02:03:04') == (1, 2, 3, 4)
        assert parse_timecode('1:2:3.4') == (1, 2, 3, 4)
        assert parse_timecode('01:02:03;04') == (1, 2, 3, 4)
        assert parse_timecode('01:02:03:04.50') == (1, 2, 3, 4)
        assert parse_timecode((1, 2, 3, 4)) == (1, 2, 3, 4)

    @pytest.mark.parametrize('bad', ['bad', '01:02:03', '01:02:03:04:05',
                                     '-01:02:03:04'])
    def test_parse_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            parse_timecode(bad)

    def test_format(self):
        assert format_timecode((1, 2, 3, 4)) == '01:02:03:04'
        assert format_timecode((1, 2, 3, 4), drop_frame=True) == (
            '01:02:03;04')
        assert format_timecode((1, 2, 3, 4), sub_frames=50) == (
            '01:02:03:04.50')

    @pytest.mark.parametrize('parts', [(0, 0, 60, 0), (0, 60, 0, 0),
                                       (0, 0, 0, 30), (24, 0, 0, 0)])
    def test_nonexistent_labels_raise_with_position(self, parts):
        with pytest.raises(InvalidTimecodeError) as excinfo:
            timecode_to_frames(parts, 30)
        assert excinfo.value.parts == parts
        assert excinfo.value.label == format_timecode(parts)

    def test_seconds(self):
        assert timecode_to_seconds('00:10:00;00', '30') == 600.0
        # Drop-frame labels track wall time; 1 hour of 29.97 material
        # is 107892 frames, a hair under 3600 seconds.
        assert timecode_to_seconds('01:00:00;00', '29.97') == pytest.approx(
            107892 / (30000 / 1001))
        # 60 s of 29.97 material is frame 1798.2, not the label 00:01:00;02.
        assert str(seconds_to_timecode(60, '29.97')) == '00:00:59;28.20'


class TestTimecode:
    def test_equality_includes_rate_reel_and_subframes(self):
        a = Timecode(1, 2, 3, 4, frame_rate='29.97', reel='A')
        b = Timecode(1, 2, 3, 4, frame_rate='29.97', reel='A')
        assert a == b
        assert hash(a) == hash(b)
        assert a != Timecode(1, 2, 3, 4, frame_rate='29.97', reel='B')
        assert a != Timecode(1, 2, 3, 4, frame_rate='30', reel='A')

    def test_parse_keeps_subframes_and_drop_hint(self):
        tc = Timecode.parse('01:02:03;04.25', frame_rate='29.97', reel='R')
        assert tc.sub_frames == 25
        assert tc.drop_frame is True
        assert tc.parts == (1, 2, 3, 4)
        assert str(tc) == 'R:01:02:03;04.25'

    def test_position(self):
        tc = Timecode(0, 1, 0, 2, sub_frames=50, frame_rate='29.97')
        assert tc.frame_number == 1800
        assert tc.position == 1800 + Fraction(1, 2)

    def test_smpte_offset_round_trip(self):
        tc = Timecode(1, 2, 3, 4, sub_frames=50, frame_rate='29.97')
        message = tc.to_meta_message()
        assert message.type == 'smpte_offset'
        assert message.frame_rate == 29.97
        restored = Timecode.from_smpte_offset(message)
        assert restored == tc

    def test_smpte_offset_message_helpers(self):
        message = MetaMessage('smpte_offset', frame_rate=30, hours=0,
                              minutes=1, seconds=0, frames=2)
        assert smpte_offset_to_frames(message) == 1802
        assert frames_to_smpte_offset(1802, 30) == message

    def test_smpte_offset_rejects_unencodable_rates(self):
        with pytest.raises(ValueError):
            Timecode(0, 0, 0, 0, frame_rate='59.94').to_meta_message()


class TestReelTable:
    def make_table(self):
        return ReelTable.from_starts(
            {'A': '00:00:00;00', 'B': '00:01:00;02'},
            frame_rate='29.97')

    def test_layout(self):
        table = self.make_table()
        assert len(table) == 2
        assert table.reels[0].frame_count == 1800
        assert table.reels[1].frame_count == (
            frames_per_day('29.97') - 1800)

    def test_reel_boundary_is_continuous(self):
        table = self.make_table()
        assert table.to_frames('A', '00:00:59;29') == 1799
        assert table.to_frames('B', '00:01:00;02') == 1800
        assert table.from_frames(1799)[1].reel == 'A'
        assert table.from_frames(1800)[1].reel == 'B'

    def test_round_trip_preserves_reel_label_and_subframes(self):
        table = self.make_table()
        for reel, label in [('A', '00:00:10;05'),
                            ('A', '00:00:42;19'),
                            ('B', '00:05:00;02')]:
            restored = table.roundtrip(reel, label)
            assert restored.reel == reel
            assert format_timecode(restored.parts,
                                   drop_frame=True) == label
            assert restored.sub_frames == 0

        restored = table.roundtrip('A', '00:00:10;05', sub_frames=25)
        assert restored.sub_frames == 25
        assert str(restored) == 'A:00:00:10;05.25'

    def test_label_outside_reel_is_a_gap(self):
        table = self.make_table()
        with pytest.raises(TimecodeGapError) as excinfo:
            table.to_frames('B', '00:00:10;00')
        assert excinfo.value.parts == (0, 0, 10, 0)
        assert excinfo.value.reel == 'B'

    def test_broken_numbering_in_one_reel(self):
        reel = Reel('R', segments=[('00:00:00;00', 100),
                                   ('00:00:10;00', 100)])
        table = ReelTable([reel], frame_rate='29.97')
        assert table.frame_count == 200
        assert table.to_frames('R', '00:00:03;09') == 99
        assert table.to_frames('R', '00:00:10;00') == 100
        # Labels between 00:00:03;10 and 00:00:09;29 belong to the gap.
        with pytest.raises(TimecodeGapError) as excinfo:
            table.to_frames('R', '00:00:05;00')
        assert excinfo.value.parts == (0, 0, 5, 0)

        for position in range(200):
            reel_name, timecode = table.from_frames(position)
            assert reel_name == 'R'
            assert table.to_frames('R', timecode.parts) == position

    def test_midnight_crossing_segment(self):
        reel = Reel('M', start='23:59:59;00', frame_count=31)
        table = ReelTable([reel], frame_rate='29.97')
        assert table.from_frames(0)[1].parts == (23, 59, 59, 0)
        assert table.from_frames(30)[1].parts == (0, 0, 0, 0)
        assert table.to_frames('M', '00:00:00;00') == 30
        assert table.to_frames('M', '23:59:59;29') == 29
        assert table.roundtrip('M', '00:00:00;00').reel == 'M'

    def test_dropped_label_on_a_reel_is_rejected(self):
        table = self.make_table()
        with pytest.raises(DroppedFrameError):
            table.to_frames('B', '00:02:00;00')

    def test_unknown_reel(self):
        table = self.make_table()
        with pytest.raises(KeyError):
            table.to_frames('Z', '00:00:00;00')
        with pytest.raises(KeyError):
            table.from_frames(99999999)

    def test_position_past_end(self):
        table = self.make_table()
        with pytest.raises(KeyError):
            table.from_frames(table.frame_count)

    def test_alias(self):
        assert TimecodeTable is ReelTable


class TestHelpers:
    def test_midnight_wrap(self):
        assert midnight_wrap(-1, '29.97') == frames_per_day('29.97') - 1
        assert midnight_wrap(frames_per_day('29.97') + 3, '29.97') == 3

    def test_drop_frames_per_minute(self):
        assert drop_frames_per_minute('29.97') == 2
        assert drop_frames_per_minute('59.94') == 4
        assert drop_frames_per_minute('30') == 0
        assert drop_frames_per_minute('29.97', drop_frame=False) == 0
        with pytest.raises(ValueError):
            drop_frames_per_minute('25', drop_frame=True)

    def test_smpte_message_helpers(self):
        message = MetaMessage('smpte_offset', frame_rate=25, hours=1,
                              minutes=2, seconds=3, frames=4,
                              sub_frames=99)
        frames = smpte_offset_to_frames(message)
        assert frames == timecode_to_frames((1, 2, 3, 4), 25)
        assert frames_to_smpte_offset(frames, 25, time=12) == (
            MetaMessage('smpte_offset', frame_rate=25, hours=1,
                        minutes=2, seconds=3, frames=4, sub_frames=0,
                        time=12))
        timecode = Timecode(1, 2, 3, 4, sub_frames=99, frame_rate=25)
        assert frames_to_smpte_offset(timecode) == message

    def test_smpte_message_type_check(self):
        with pytest.raises(TypeError):
            smpte_offset_to_frames(MetaMessage('end_of_track'))
        with pytest.raises(TypeError):
            Timecode.from_smpte_offset(MetaMessage('end_of_track'))

    def test_from_position_rounds_subframes(self):
        tc = Timecode.from_position(Fraction(10) + Fraction(1, 3),
                                    '30')
        assert tc.parts == (0, 0, 0, 10)
        assert tc.sub_frames == 33

    def test_reel_validation(self):
        with pytest.raises(ValueError):
            Reel('A')
        with pytest.raises(ValueError):
            Reel('A', start='00:00:00:00', frame_count=0)
        with pytest.raises(ValueError):
            Reel('A', start='00:00:00:00', frame_count=10,
                 segments=[('00:00:00:00', 10)])
        with pytest.raises(ValueError):
            Reel('A', segments=[])
        assert Reel('A', start='00:00:00:00', frame_count=10).frame_count == 10

    def test_duplicate_reel_names(self):
        with pytest.raises(ValueError):
            ReelTable([Reel('A', start='00:00:00:00', frame_count=10),
                       Reel('A', start='00:00:10:00', frame_count=10)],
                      frame_rate='30')

    def test_empty_starts(self):
        with pytest.raises(ValueError):
            ReelTable.from_starts({}, frame_rate='30')

    def test_timecode_rejects_non_integer_fields(self):
        with pytest.raises(TypeError):
            Timecode(0.5, 0, 0, 0, frame_rate='30')
        with pytest.raises(TypeError):
            Timecode(0, True, 0, 0, frame_rate='30')

    def test_immutability(self):
        reel = Reel('A', start='00:00:00:00', frame_count=10)
        with pytest.raises(AttributeError):
            reel.name = 'B'
        tc = Timecode(0, 0, 0, 0, frame_rate='30')
        with pytest.raises(AttributeError):
            tc.frames = 1

    def test_format_uses_timecode_attributes(self):
        tc = Timecode.parse('01:02:03;04.50', frame_rate='29.97')
        assert format_timecode(tc) == '01:02:03;04.50'
        assert format_timecode(tc, drop_frame=False) == '01:02:03:04.50'

    def test_with_reel_and_to_frame_count(self):
        tc = Timecode(0, 1, 0, 2, sub_frames=50, frame_rate='29.97')
        moved = tc.with_reel('B')
        assert moved.reel == 'B'
        assert moved.parts == tc.parts
        assert moved.sub_frames == 50
        assert tc.with_reel('B') == moved

        table = ReelTable.from_starts(
            {'A': '00:00:00;00', 'B': '00:01:00;02'},
            frame_rate='29.97')
        assert table.to_frame_count('A', '00:00:10;05') == 305
