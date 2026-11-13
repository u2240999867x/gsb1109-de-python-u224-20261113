import functools
import operator

import pytest

import pynmea2
from pynmea2 import compass


# --------------------------------------------------------------------------
# sentence builders
# --------------------------------------------------------------------------
def _checksum(body):
    return functools.reduce(operator.xor, map(ord, body), 0)


def sentence(body):
    return '$%s*%02X' % (body, _checksum(body))


def parse(body):
    return pynmea2.parse(sentence(body))


def zda(second):
    hour = 12 + second // 3600
    minute = (second % 3600) // 60
    sec = second % 60
    return sentence('GPZDA,%02d%02d%02d,14,11,2026,00,00' % (hour, minute, sec))


def hdg(magnetic, deviation, dev_dir, variation, var_dir):
    return sentence('HCHDG,%.1f,%.1f,%s,%.1f,%s' % (
        magnetic, abs(deviation), dev_dir, abs(variation), var_dir))


def hdt(true_heading):
    return sentence('HCHDT,%.1f,T' % true_heading)


def audit_lines(lines, table, **kwargs):
    messages = [pynmea2.parse(line) for line in lines]
    kwargs.setdefault('max_time_gap', 5.0)
    return compass.audit_sentences(messages, table, step=15, **kwargs)


def block(second, magnetic, dev, dev_dir, var, var_dir, true):
    """One timestamped HDG+HDT observation, isolated in its own time slot."""
    return [zda(second), hdg(magnetic, dev, dev_dir, var, var_dir),
            hdt(true)]


# --------------------------------------------------------------------------
# angle helpers: east/west sign and the 359 -> 1 seam
# --------------------------------------------------------------------------
def test_signed_ew_convention():
    assert compass.signed_ew(3.0, 'E') == 3.0
    assert compass.signed_ew(3.0, 'W') == -3.0
    assert compass.signed_ew(None, 'E') is None
    assert compass.signed_ew(3.0, '') is None
    with pytest.raises(ValueError):
        compass.signed_ew(3.0, 'N')


def test_wrap_helpers():
    assert compass.wrap360(361.0) == pytest.approx(1.0)
    assert compass.wrap360(-1.0) == pytest.approx(359.0)
    assert compass.wrap180(270.0) == pytest.approx(-90.0)
    # shortest arc across the seam
    assert compass.angular_diff(1.0, 359.0) == pytest.approx(2.0)
    assert compass.angular_diff(359.0, 1.0) == pytest.approx(-2.0)


def test_heading_bucket_straddles_seam():
    assert compass.heading_bucket(359.5, 15) == 0
    assert compass.heading_bucket(0.5, 15) == 0
    assert compass.heading_bucket(7.0, 15) == 0
    assert compass.heading_bucket(8.0, 15) == 15
    assert compass.heading_bucket(90.0, 15) == 90
    with pytest.raises(ValueError):
        compass.heading_bucket(90.0, 7)


def test_hdg_sentence_signs_are_parsed():
    records = compass.collect_records(
        [parse('HCHDG,90.0,3.0,W,5.0,E')])
    record = records[0]
    assert record.magnetic_heading == 90.0
    assert record.deviation == -3.0
    assert record.variation == 5.0


def test_rmc_true_course_is_a_reference():
    records = compass.collect_records([
        pynmea2.parse(zda(0)),
        pynmea2.parse(sentence(
            'GPRMC,120000,A,4916.45,N,12311.12,W,0.5,054.7,191194,020.3,E')),
    ])
    reference = [r for r in records if r.is_reference][0]
    assert reference.true_heading == pytest.approx(54.7)


def test_void_rmc_fix_is_not_a_reference():
    records = compass.collect_records([
        pynmea2.parse(zda(0)),
        pynmea2.parse(sentence(
            'GPRMC,120000,V,4916.45,N,12311.12,W,0.5,054.7,191194,020.3,E')),
    ])
    assert not [r for r in records if r.is_reference]


# --------------------------------------------------------------------------
# per-band verification
# --------------------------------------------------------------------------
def test_correct_table_band_is_ok():
    # variation 5W = -5; true = 90 + dev(7) + var(-5) = 92
    audit = audit_lines(
        block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0),
        {90: 7.0})
    band = audit.buckets[90]
    assert band.verdict == compass.OK
    assert band.required_min == pytest.approx(7.0)
    assert band.residual_mean == pytest.approx(0.0)


def test_west_deviation_and_east_variation():
    # deviation 4W (-4), variation 10E (+10): true = 100 - 4 + 10 = 106
    lines = block(0, 100.0, 4.0, 'W', 10.0, 'E', 106.0)
    audit = audit_lines(lines, {105: -4.0})
    band = audit.buckets[105]
    assert band.verdict == compass.OK
    assert band.required_min == pytest.approx(-4.0)


def test_required_deviation_includes_variation():
    # required deviation = true - magnetic - variation = 106 - 100 - 10 = -4,
    # proving the 10E variation is subtracted rather than silently ignored
    lines = block(0, 100.0, 4.0, 'W', 10.0, 'E', 106.0)
    audit = audit_lines(lines, {105: 0.0})
    band = audit.buckets[105]
    assert band.required_min == pytest.approx(-4.0)
    assert band.residual_mean == pytest.approx(-4.0)


def test_format_helpers():
    assert compass.format_signed(-3.5) == '3.5°W'
    assert compass.format_signed(0.0) == '0.0°E'
    assert compass.format_signed(None) == '—'
    assert compass._range_text(2.0, 2.0) == '+2.0°'
    assert compass._range_text(-1.0, 1.0) == '-1.0°~+1.0°'


def test_seam_band_groups_359_and_001():
    # deviation 2E at the 0 deg band; variation zero
    lines = (
        block(0, 359.5, 2.0, 'E', 0.0, 'E', 1.5)
        + block(10, 0.5, 2.0, 'E', 0.0, 'E', 2.5)
    )
    audit = audit_lines(lines, {0: 2.0})
    band = audit.buckets[0]
    assert len(band.pairs) == 2
    assert {p.hdg.magnetic_heading for p in band.pairs} == {359.5, 0.5}
    assert band.verdict == compass.OK
    # wrapping across the seam yields +2.0, never a -358 deg artifact
    assert band.required_min == pytest.approx(2.0)
    assert band.residual_max == pytest.approx(0.0, abs=1e-9)
    assert band.residual_min == pytest.approx(0.0, abs=1e-9)


def test_sign_flipped_band_and_overall():
    lines = (
        # band 90: needed +7, table claims -7 (7W) -> residual +14 = -2*table
        block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
        # band 180: needed +13, table claims -13 -> residual +26
        + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    )
    audit = audit_lines(lines, {90: -7.0, 180: -13.0})
    assert audit.buckets[90].verdict == compass.SIGN_FLIPPED
    assert audit.buckets[180].verdict == compass.SIGN_FLIPPED
    assert audit.verdict == compass.SIGN_FLIPPED


def test_constant_bias_is_distinct_from_sign_flip():
    lines = (
        # needed 7, table 3 -> residual +4 (same sign, just offset)
        block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
        # needed 13, table 9 -> residual +4
        + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    )
    audit = audit_lines(lines, {90: 3.0, 180: 9.0})
    assert audit.verdict == compass.CONSTANT_BIAS
    assert audit.constant_bias == pytest.approx(4.0)
    assert audit.buckets[90].verdict == compass.CONSTANT_BIAS


def test_single_band_cannot_separate_bias_from_flip():
    # one populated band only: residual -3, tight, but flip cannot be ruled out
    lines = block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
    audit = audit_lines(lines, {90: 10.0})
    assert audit.buckets[90].verdict == compass.CONSTANT_BIAS
    assert audit.verdict == compass.INCONCLUSIVE


def test_single_flip_band_is_not_declared_global_flip():
    # only one populated band matches the flip prediction: not enough evidence
    lines = block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
    audit = audit_lines(lines, {90: -7.0})
    assert audit.buckets[90].verdict == compass.SIGN_FLIPPED
    assert audit.verdict == compass.INCONCLUSIVE


def test_mixed_flip_and_bias_bands_are_inconclusive():
    lines = (
        # band 90 behaves like a sign reversal (needed +7, table -7)
        block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
        # band 180 behaves like a plain offset (needed +13, table +9)
        + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    )
    audit = audit_lines(lines, {90: -7.0, 180: 9.0})
    assert audit.buckets[90].verdict == compass.SIGN_FLIPPED
    assert audit.buckets[180].verdict == compass.CONSTANT_BIAS
    assert audit.verdict == compass.INCONCLUSIVE
    assert '不一致' in audit.summary


def test_all_bands_ok():
    lines = (
        block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
        + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    )
    audit = audit_lines(lines, {90: 7.0, 180: 13.0})
    assert audit.verdict == compass.OK
    assert audit.buckets[180].verdict == compass.OK


def test_no_reference_at_all():
    audit = audit_lines([zda(0)], {90: 7.0})
    assert audit.verdict == compass.NO_REFERENCE
    assert all(b.verdict == compass.NO_REFERENCE for b in audit.bucket_list())


# --------------------------------------------------------------------------
# reference time window
# --------------------------------------------------------------------------
def test_reference_outside_time_window_is_rejected():
    lines = [zda(0), hdg(90.0, 7.0, 'E', 5.0, 'W'),
             zda(10), hdt(92.0)]
    stale = audit_lines(lines, {90: 7.0}, max_time_gap=5.0)
    assert stale.buckets[90].verdict == compass.NO_REFERENCE
    usable = audit_lines(lines, {90: 7.0}, max_time_gap=15.0)
    assert usable.buckets[90].verdict == compass.OK


def test_index_fallback_without_timestamps():
    lines = [hdg(90.0, 7.0, 'E', 5.0, 'W'), hdt(92.0)]
    close = audit_lines(lines, {90: 7.0}, max_index_gap=3)
    assert close.buckets[90].verdict == compass.OK
    far = audit_lines(lines, {90: 7.0}, max_index_gap=0)
    assert far.buckets[90].verdict == compass.NO_REFERENCE


# --------------------------------------------------------------------------
# turns: several references match, report an error range
# --------------------------------------------------------------------------
def test_turn_produces_error_range_and_inconclusive_when_wide():
    lines = [
        zda(0),
        hdg(90.0, 7.0, 'E', 5.0, 'W'),
        hdt(92.0),   # required deviation 7
        hdt(98.0),   # required deviation 13, still inside the window
    ]
    audit = audit_lines(lines, {90: 7.0}, spread_tolerance=2.0)
    band = audit.buckets[90]
    assert band.ambiguous is True
    assert band.verdict == compass.INCONCLUSIVE
    assert band.residual_min == pytest.approx(0.0)
    assert band.residual_max == pytest.approx(6.0)
    rendered = compass._range_text(band.residual_min, band.residual_max)
    assert rendered == '+0.0°~+6.0°'


def test_turn_with_tight_range_still_verifies_ok():
    lines = [
        zda(0),
        hdg(90.0, 7.0, 'E', 5.0, 'W'),
        hdt(92.0),  # required 7
        hdt(93.0),  # required 8, within tolerance
    ]
    audit = audit_lines(lines, {90: 7.0})
    band = audit.buckets[90]
    assert band.ambiguous is True
    assert band.verdict == compass.OK


# --------------------------------------------------------------------------
# table input forms and reporting
# --------------------------------------------------------------------------
def test_table_input_forms(tmp_path):
    assert compass._normalise_table({90: 3.0}, 15)[90] == 3.0
    sequence = compass._normalise_table([0.0] * 24, 15)
    assert sequence[0] == 0.0 and sequence[345] == 0.0
    callable_table = compass._normalise_table(lambda c: 1.5, 15)
    assert callable_table[180] == 1.5
    ew = compass.table_from_e_w([(90, 3.0, 'W'), (0, 2.0, 'E')], 15)
    assert ew[90] == -3.0 and ew[0] == 2.0


def test_report_contains_verdict_and_range(tmp_path):
    lines = (
        block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
        + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    )
    audit = audit_lines(lines, {90: 3.0, 180: 9.0})
    report = compass.format_report(audit)
    assert '固定偏差' in report
    assert '总体判定' in report
    assert '359' in report and '1°' in report


def test_audit_file_and_cli(tmp_path, capsys):
    log = tmp_path / 'compass.log'
    log.write_text('\n'.join(
        ['garbage line that is not a sentence']
        + block(0, 90.0, 7.0, 'E', 5.0, 'W', 92.0)
        + block(10, 180.0, 13.0, 'E', 5.0, 'W', 188.0)
    ), encoding='utf-8')
    table_file = tmp_path / 'table.txt'
    table_file.write_text('90 3.0\n180 9.0\n', encoding='utf-8')

    audit = compass.audit_file(str(log),
                               compass._load_table(str(table_file), 15))
    assert audit.verdict == compass.CONSTANT_BIAS

    exit_code = compass.main([
        str(log), '--table', str(table_file), '--step', '15'])
    assert exit_code == 1
    assert '固定偏差' in capsys.readouterr().out
