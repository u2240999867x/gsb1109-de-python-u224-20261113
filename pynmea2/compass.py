"""
Compass deviation table (罗经自差表) auditing.

The magnetic compass deviation table is organised by ship's heading, one
band (档) per ``step`` degrees.  Aboard ship the NMEA log records, at the
same time, an ``HDG`` sentence (magnetic sensor heading plus deviation and
variation, each tagged E/W) and a reference true heading from ``HDT`` (or
the true course carried by ``RMC``).

This module pairs those records, recomputes the deviation that each band
would need in order to agree with the true reference, and compares it with
the tabulated value.  It is careful about:

* east/west sign convention (E is positive, W negative);
* the 359 deg -> 1 deg seam, all angle arithmetic is wrapped;
* references only being usable inside a finite time window;
* turns, where one record can match several neighbours, in which case an
  error range is reported instead of a single number;
* telling apart a constant bias, an east/west sign reversal and evidence
  that genuinely cannot distinguish the two.
"""

from collections import OrderedDict
import argparse
import sys

from .nmea import ParseError

# verdict identifiers (machine readable) ------------------------------------
OK = 'ok'
NO_REFERENCE = 'no_reference'
SIGN_FLIPPED = 'sign_flipped'
CONSTANT_BIAS = 'constant_bias'
INCONCLUSIVE = 'inconclusive'

_VERDICT_TEXT = {
    OK: '正确',
    NO_REFERENCE: '时差内无参考源',
    SIGN_FLIPPED: '东西符号写反',
    CONSTANT_BIAS: '固定偏差',
    INCONCLUSIVE: '证据不足，无法判定',
}

_EAST_WEST = ('E', 'W')


# angle helpers -------------------------------------------------------------
def wrap360(angle):
    """Normalise any angle to [0, 360)."""
    return angle % 360.0


def wrap180(angle):
    """Normalise a signed angle difference to (-180, 180]."""
    return (angle + 180.0) % 360.0 - 180.0


def angular_diff(a, b):
    """Signed shortest-arc difference ``a - b`` in (-180, 180]."""
    return wrap180(a - b)


def signed_ew(magnitude, direction):
    """Convert a (magnitude, 'E'/'W') pair to a signed angle.

    East is positive, west is negative.  Empty input means the field was
    not recorded and returns ``None``; an unknown direction letter raises
    ``ValueError`` so that a bad sentence is never silently misread.
    """
    if magnitude is None or magnitude == '' or direction in (None, ''):
        return None
    if direction not in _EAST_WEST:
        raise ValueError("direction must be 'E' or 'W', got %r" % direction)
    sign = 1.0 if direction == 'E' else -1.0
    return sign * float(magnitude)


def format_signed(angle):
    """Render a signed angle with the E/W convention, e.g. ``3.0°E``."""
    if angle is None:
        return '—'
    direction = 'E' if angle >= 0 else 'W'
    return '{:.1f}°{}'.format(abs(angle), direction)


def heading_bucket(heading, step):
    """Return the band centre (multiple of ``step``) for a heading.

    The 0 deg band straddles the seam: both 359 deg and 1 deg map to 0.
    """
    if step <= 0 or 360 % step != 0:
        raise ValueError('step must divide 360 evenly')
    shifted = wrap360(heading) + step / 2.0
    return int(wrap360(shifted // step * step))


def bucket_centres(step):
    """All band centres in ascending order, 0 first."""
    return list(range(0, 360, step))


# record extraction ---------------------------------------------------------
class CompassRecord(object):
    """One normalised heading record pulled from an NMEA sentence."""

    __slots__ = (
        'index', 'time', 'sentence_type', 'talker',
        'magnetic_heading', 'deviation', 'variation',
        'true_heading', 'valid', 'issues',
    )

    def __init__(self, index, time, sentence_type, talker):
        self.index = index
        self.time = time
        self.sentence_type = sentence_type
        self.talker = talker
        self.magnetic_heading = None
        self.deviation = None
        self.variation = None
        self.true_heading = None
        self.valid = True
        self.issues = []

    @property
    def is_hdg(self):
        return self.sentence_type == 'HDG'

    @property
    def is_reference(self):
        return self.true_heading is not None


def _message_time(msg, clock):
    """Return the timestamp of a sentence as seconds-of-day, if any.

    ``clock`` is the running (seconds-of-day, rollover-adjusted) clock
    carried over from earlier timestamped sentences.  Returns ``None`` for
    sentences that carry no time (plain HDG/HDT).
    """
    stamp = getattr(msg, 'timestamp', None)
    if stamp is None:
        return None
    seconds = (stamp.hour * 3600 + stamp.minute * 60 + stamp.second
               + stamp.microsecond / 1000000.0)
    if clock is not None and seconds + 86400.0 - clock < clock - seconds:
        # timestamp wrapped past midnight relative to the running clock
        seconds += 86400.0
    return seconds


def _build_hdg_record(msg, index, time):
    record = CompassRecord(index, time, 'HDG', getattr(msg, 'talker', ''))
    record.magnetic_heading = (
        float(msg.heading) if getattr(msg, 'heading', None) not in (None, '')
        else None)
    try:
        record.deviation = signed_ew(msg.deviation, msg.dev_dir)
        record.variation = signed_ew(msg.variation, msg.var_dir)
    except ValueError as exc:
        record.valid = False
        record.issues.append(str(exc))
    if record.magnetic_heading is None:
        record.valid = False
        record.issues.append('HDG missing magnetic heading')
    return record


def _build_reference_record(msg, index, time, true_heading):
    record = CompassRecord(index, time, msg.sentence_type,
                           getattr(msg, 'talker', ''))
    record.true_heading = wrap360(float(true_heading))
    return record


def collect_records(sentences):
    """Turn parsed NMEA sentences into :class:`CompassRecord` objects.

    ``HDG`` feeds the compass side; ``HDT`` and valid ``RMC`` (its true
    course field) feed the reference side.  Anything else is only used to
    carry the running timestamp clock.
    """
    records = []
    clock = None
    for index, msg in enumerate(sentences):
        time = _message_time(msg, clock)
        if time is not None:
            clock = time
        # HDG/HDT carry no timestamp of their own: they inherit the running
        # clock established by the latest timestamped sentence (RMC/ZDA/...).
        effective_time = time if time is not None else clock
        sentence_type = getattr(msg, 'sentence_type', None)
        if sentence_type == 'HDG':
            records.append(_build_hdg_record(msg, index, effective_time))
        elif sentence_type == 'HDT' and getattr(msg, 'heading', None) not in (None, ''):
            records.append(
                _build_reference_record(msg, index, effective_time,
                                        msg.heading))
        elif sentence_type == 'RMC' and getattr(msg, 'true_course', None) not in (None, ''):
            # a void GPS fix (status 'V') must not be trusted as a reference
            if getattr(msg, 'is_valid', True):
                records.append(
                    _build_reference_record(msg, index, effective_time,
                                            msg.true_course))
    return records


# pairing -------------------------------------------------------------------
class PairedObservation(object):
    """An HDG record matched with a nearby true-heading reference."""

    __slots__ = (
        'bucket', 'hdg', 'reference', 'time_gap', 'index_gap',
        'tabulated', 'required_deviation', 'residual',
    )

    def __init__(self, hdg, reference, bucket, tabulated):
        self.hdg = hdg
        self.reference = reference
        self.bucket = bucket
        self.tabulated = tabulated
        self.time_gap = self._gap(hdg.time, reference.time)
        self.index_gap = abs(hdg.index - reference.index)
        # deviation the compass would need for the true heading to match:
        #   true = magnetic + deviation + variation
        variation = hdg.variation or 0.0
        self.required_deviation = wrap180(
            reference.true_heading - hdg.magnetic_heading - variation)
        self.residual = angular_diff(self.required_deviation, tabulated)

    @staticmethod
    def _gap(a, b):
        if a is None or b is None:
            return None
        return abs(a - b)


def pair_records(records, table, step, max_time_gap=5.0, max_index_gap=3):
    """Match every HDG record with every reference inside the time window.

    When timestamps exist the *time* difference must be within
    ``max_time_gap`` seconds ("参考源只在一定时差内可用").  Sentences
    without any timestamp fall back to a maximum sequence-index distance.
    All qualifying pairs are kept: during a turn several references can
    legitimately match one HDG and the spread between them becomes the
    reported error range.
    """
    hdg_records = [r for r in records if r.is_hdg and r.valid]
    references = [r for r in records if r.is_reference]
    pairs = []
    for hdg in hdg_records:
        bucket = heading_bucket(hdg.magnetic_heading, step)
        tabulated = table.get(bucket, 0.0)
        for reference in references:
            if hdg.time is not None and reference.time is not None:
                if abs(hdg.time - reference.time) > max_time_gap:
                    continue
            elif abs(hdg.index - reference.index) > max_index_gap:
                continue
            pairs.append(PairedObservation(hdg, reference, bucket, tabulated))
    return pairs


# per-band and overall results ----------------------------------------------
class BucketResult(object):
    """Audit outcome for one heading band."""

    __slots__ = (
        'bucket', 'tabulated', 'pairs', 'residual_min', 'residual_max',
        'required_min', 'required_max', 'ambiguous', 'verdict', 'detail',
    )

    def __init__(self, bucket, tabulated):
        self.bucket = bucket
        self.tabulated = tabulated
        self.pairs = []
        self.residual_min = None
        self.residual_max = None
        self.required_min = None
        self.required_max = None
        self.ambiguous = False
        self.verdict = NO_REFERENCE
        self.detail = ''

    @property
    def residual_mean(self):
        if not self.pairs:
            return None
        return (self.residual_min + self.residual_max) / 2.0

    @property
    def residual_spread(self):
        if not self.pairs:
            return None
        return abs(wrap180(self.residual_max - self.residual_min))


class DeviationAudit(object):
    """Full audit result: per-band findings plus the overall conclusion."""

    def __init__(self, step, tolerance, spread_tolerance, max_time_gap):
        self.step = step
        self.tolerance = tolerance
        self.spread_tolerance = spread_tolerance
        self.max_time_gap = max_time_gap
        self.buckets = OrderedDict()
        self.verdict = INCONCLUSIVE
        self.constant_bias = None
        self.summary = ''

    def bucket_list(self):
        return list(self.buckets.values())


def _classify_bucket(result, tolerance, spread_tolerance):
    pairs = result.pairs
    if not pairs:
        result.verdict = NO_REFERENCE
        result.detail = '时间窗内没有可配对的真航向参考'
        return

    residuals = [p.residual for p in pairs]
    required = [p.required_deviation for p in pairs]
    result.residual_min = min(residuals)
    result.residual_max = max(residuals)
    result.required_min = min(required)
    result.required_max = max(required)
    # genuine pairing ambiguity: one HDG matched by several references or
    # vice versa.  Repeated 1:1 samples that happen to share a band (e.g.
    # the 359/1 seam) are independent observations, not a turn ambiguity.
    hdg_counts = {}
    ref_counts = {}
    for pair in pairs:
        hdg_counts[pair.hdg.index] = hdg_counts.get(pair.hdg.index, 0) + 1
        ref_counts[pair.reference.index] = ref_counts.get(
            pair.reference.index, 0) + 1
    result.ambiguous = (any(c > 1 for c in hdg_counts.values())
                        or any(c > 1 for c in ref_counts.values()))

    tab = result.tabulated
    # residual we would see if the E/W letter of the table were reversed
    flip_residual = wrap180(-2.0 * tab) if tab else None

    fits_zero = all(abs(r) <= tolerance for r in residuals)
    fits_flip = (flip_residual is not None
                 and all(abs(angular_diff(r, flip_residual)) <= tolerance
                         for r in residuals))
    tight = result.residual_spread <= spread_tolerance

    if fits_zero:
        result.verdict = OK
        result.detail = '表列自差与参考真航向一致'
    elif fits_flip:
        result.verdict = SIGN_FLIPPED
        result.detail = '将该档 E/W 符号取反后即与参考一致'
    elif tight:
        result.verdict = CONSTANT_BIAS
        result.detail = '存在大小稳定的偏差，均值 {:+.1f}°'.format(
            result.residual_mean)
    else:
        result.verdict = INCONCLUSIVE
        result.detail = ('转弯期多候选导致残差散布 {:+.1f}°~{:+.1f}°，'
                         '超出散布容差').format(result.residual_min,
                                              result.residual_max)

    if result.ambiguous and result.verdict in (OK, SIGN_FLIPPED):
        result.detail += '（转弯期多候选，已按误差范围核对）'


def _classify_overall(audit):
    populated = [b for b in audit.bucket_list() if b.pairs]
    if not populated:
        audit.verdict = NO_REFERENCE
        audit.summary = '没有任何一档在时间窗内取得真航向参考，无法核对。'
        return

    if all(b.verdict == OK for b in populated):
        audit.verdict = OK
        audit.summary = '所有有参考的档均与罗经表一致。'
        return

    flip_bands = [b for b in populated if b.verdict == SIGN_FLIPPED]
    bias_bands = [b for b in populated if b.verdict == CONSTANT_BIAS]
    wide_bands = [b for b in populated if b.verdict == INCONCLUSIVE]

    # Turn ambiguity in any band means the global correction cannot be fixed
    # yet; per-band rows still carry whatever each band could establish.
    if wide_bands:
        audit.verdict = INCONCLUSIVE
        audit.summary = ('转弯期间多个参考都能对上，残差只能给出范围，'
                         '现有证据不足以确定修正量。')
        return

    # A sign reversal must explain *every* populated band, on at least two
    # headings, via the band-specific prediction residual = -2 * table.
    if flip_bands and not bias_bands and len(populated) >= 2:
        audit.verdict = SIGN_FLIPPED
        audit.summary = ('各档残差均等于表列值取反后的预测值，符合整表 E/W '
                         '符号写反；把自差表东西向整体反号即可。')
        return

    # A constant bias predicts the same residual in every populated band.
    if bias_bands and not flip_bands and len(bias_bands) >= 2:
        means = [b.residual_mean for b in bias_bands]
        if max(means) - min(means) <= audit.tolerance:
            audit.verdict = CONSTANT_BIAS
            audit.constant_bias = sum(means) / len(means)
            audit.summary = (
                '各档残差一致，约为 {:+.1f}°，属于与船首向无关的固定偏差'
                '（例如安装/索引偏差），不是东西符号问题。').format(
                audit.constant_bias)
            return

    # Mixed per-band evidence, or only one populated band: the two
    # hypotheses are not separable with the data currently on hand.
    audit.verdict = INCONCLUSIVE
    if flip_bands and bias_bands:
        audit.summary = ('部分档符合符号写反、部分档符合固定偏差，证据互相'
                         '不一致，需要补测后再下结论。')
    else:
        audit.summary = ('各档证据不一致或只有单档样本，固定偏差与符号写反'
                         '目前分不出来，需要补充更多航向的观测。')


def audit_records(records, table, step=15, tolerance=2.0,
                  spread_tolerance=2.0, max_time_gap=5.0, max_index_gap=3):
    """Build the full :class:`DeviationAudit` from collected records."""
    normalised = _normalise_table(table, step)
    pairs = pair_records(records, normalised, step,
                         max_time_gap=max_time_gap,
                         max_index_gap=max_index_gap)
    audit = DeviationAudit(step, tolerance, spread_tolerance, max_time_gap)
    for centre in bucket_centres(step):
        audit.buckets[centre] = BucketResult(centre, normalised[centre])
    for pair in pairs:
        audit.buckets[pair.bucket].pairs.append(pair)
    for result in audit.bucket_list():
        _classify_bucket(result, tolerance, spread_tolerance)
    _classify_overall(audit)
    return audit


def audit_sentences(sentences, table, step=15, **kwargs):
    """Parse-level convenience: audit an iterable of NMEA sentences."""
    return audit_records(collect_records(sentences), table, step=step, **kwargs)


def audit_file(path, table, step=15, **kwargs):
    """Audit a NMEA log file, skipping lines that fail to parse."""
    sentences = []
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                sentences.append(_parse(line))
            except ParseError:
                continue
    return audit_sentences(sentences, table, step=step, **kwargs)


def _parse(line):
    # local import keeps the module importable without the full package wire-up
    from . import parse as nmea_parse
    return nmea_parse(line)


# table helpers -------------------------------------------------------------
def _normalise_table(table, step):
    """Accept a dict, list or callable and return {centre: signed deviation}."""
    centres = bucket_centres(step)
    result = OrderedDict((centre, 0.0) for centre in centres)
    if callable(table):
        for centre in centres:
            value = table(centre)
            result[centre] = 0.0 if value is None else float(value)
        return result
    if isinstance(table, dict):
        for centre in centres:
            if centre in table:
                result[centre] = float(table[centre])
        return result
    # plain sequence: aligned with bucket_centres, or (centre, value) pairs
    values = list(table)
    if values and isinstance(values[0], (tuple, list)):
        for centre, value in values:
            result[int(centre)] = float(value)
    else:
        for centre, value in zip(centres, values):
            result[centre] = float(value)
    return result


def table_from_e_w(pairs, step):
    """Build a signed table from ``(bucket, magnitude, 'E'/'W')`` rows."""
    table = OrderedDict((centre, 0.0) for centre in bucket_centres(step))
    for bucket, magnitude, direction in pairs:
        table[int(bucket)] = signed_ew(magnitude, direction)
    return table


# reporting -----------------------------------------------------------------
def _display_width(text):
    """Terminal column width: CJK/full-width glyphs occupy two columns."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
               for ch in text)


def _pad(text, width):
    return text + ' ' * max(0, width - _display_width(text))


def format_report(audit):
    """Render a human-readable Chinese report."""
    lines = []
    lines.append('罗经自差表核对报告')
    lines.append('=' * 72)
    lines.append('分档步长: {0}°    时间窗: {1}s    判定容差: ±{2}°    '
                 '散布容差: ±{3}°'.format(
                     audit.step, audit.max_time_gap, audit.tolerance,
                     audit.spread_tolerance))
    lines.append('符号约定: 东偏 E 为正，西偏 W 为负；角度差均按 360° 环绕计算')
    lines.append('')
    columns = ('船首向', '表列自差', '样本数', '实测所需自差',
               '残差(实测-表列)', '判定', '备注')
    rows = [_bucket_row_cells(result) for result in audit.bucket_list()]
    widths = [max(_display_width(col),
                  max((_display_width(row[i]) for row in rows), default=0))
              for i, col in enumerate(columns)]

    def render(cells):
        return ' | '.join(_pad(cell, widths[i]) for i, cell in enumerate(cells))

    lines.append(render(columns))
    lines.append('-' * (sum(widths) + 3 * (len(columns) - 1)))
    for row in rows:
        lines.append(render(row))
    lines.append('')
    lines.append('0° 档覆盖 359°→1° 交界：359.x° 与 0.x° 的船首向均归入该档，')
    lines.append('残差按最短弧环绕求解，不会在 359/1 处产生 358° 的假误差。')
    lines.append('')
    lines.append('总体判定: {0} — {1}'.format(
        audit.verdict, _VERDICT_TEXT.get(audit.verdict, audit.verdict)))
    lines.append('结论: {0}'.format(audit.summary))
    return '\n'.join(lines)


def _bucket_row_cells(result):
    if not result.pairs:
        required = '—'
        residual = '—'
        count = '0'
    else:
        count = str(len(result.pairs))
        required = _range_text(result.required_min, result.required_max)
        residual = _range_text(result.residual_min, result.residual_max)
    note = result.detail
    if result.ambiguous:
        note = '转弯多候选; ' + note
    return (
        '{:>4}°'.format(result.bucket),
        format_signed(result.tabulated),
        count,
        required,
        residual,
        _VERDICT_TEXT.get(result.verdict, result.verdict),
        note,
    )


def _range_text(low, high):
    if low is None:
        return '—'
    if abs(angular_diff(high, low)) < 0.05:
        return '{:+.1f}°'.format(low)
    return '{:+.1f}°~{:+.1f}°'.format(low, high)


# CLI -----------------------------------------------------------------------
def _load_table(path, step):
    table = OrderedDict((centre, 0.0) for centre in bucket_centres(step))
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.replace(',', ' ').split()]
            bucket = int(float(parts[0]))
            if len(parts) >= 3:
                table[bucket] = signed_ew(float(parts[1]), parts[2].upper())
            else:
                table[bucket] = float(parts[1])
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='核对磁罗经自差表（按船首向分档）')
    parser.add_argument('logfile', help='NMEA 0183 日志文件')
    parser.add_argument('--table', required=True,
                        help='自差表文件，每行: 船首向 自差大小 E/W（或带符号自差）')
    parser.add_argument('--step', type=int, default=15,
                        help='分档步长（度，须整除 360）')
    parser.add_argument('--window', type=float, default=5.0,
                        help='参考源最大可用时差（秒）')
    parser.add_argument('--tolerance', type=float, default=2.0,
                        help='判定容差（度）')
    parser.add_argument('--spread', type=float, default=2.0,
                        help='转弯期残差散布容差（度）')
    args = parser.parse_args(argv)

    table = _load_table(args.table, args.step)
    audit = audit_file(args.logfile, table, step=args.step,
                       tolerance=args.tolerance, spread_tolerance=args.spread,
                       max_time_gap=args.window)
    print(format_report(audit))
    return 0 if audit.verdict in (OK, NO_REFERENCE) else 1


if __name__ == '__main__':
    sys.exit(main())
