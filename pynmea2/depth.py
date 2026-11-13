from collections import namedtuple
from decimal import Decimal, ROUND_HALF_UP

from . import parse
from .nmea import ParseError

FEET_PER_METER = Decimal('3.280839895013123')
FATHOMS_PER_METER = Decimal('0.5468066491688539')
DBT_COLUMNS = ('feet', 'meters', 'fathoms')
DBT_UNIT_LABELS = {
    'feet': ('f',),
    'meters': ('M',),
    'fathoms': ('F',),
}

Observation = namedtuple(
    'Observation',
    ('sentence', 'depth_feet', 'depth_meters', 'depth_fathoms', 'bad'),
)
ColumnEvidence = namedtuple(
    'ColumnEvidence',
    ('column', 'unit_label', 'value', 'depth', 'precision', 'rounding'),
)
AuditResult = namedtuple(
    'AuditResult',
    ('mapping', 'consistent', 'evidence', 'rejected', 'ambiguous'),
)


class DepthAuditError(ValueError):
    pass


def to_decimal(value):
    if value is None or value == '':
        raise DepthAuditError('blank depth column')
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DepthAuditError('invalid depth %r' % (value,)) from exc


def precision_of(value):
    exponent = Decimal(str(value)).as_tuple().exponent
    if exponent >= 0:
        return Decimal(1)
    return Decimal(1).scaleb(exponent)


def rounded_value(depth, precision):
    return depth.quantize(precision, rounding=ROUND_HALF_UP)


def rounded_interval(value, precision):
    value = Decimal(str(value))
    half = precision / Decimal(2)
    return value - half, value + half


def meters_for(value, column):
    value = Decimal(str(value))
    if column == 'feet':
        return value / FEET_PER_METER
    if column == 'fathoms':
        return value / FATHOMS_PER_METER
    return value


def column_value(depth_meters, column, precision):
    if column == 'feet':
        value = depth_meters * FEET_PER_METER
    elif column == 'fathoms':
        value = depth_meters * FATHOMS_PER_METER
    else:
        value = depth_meters
    return rounded_value(value, precision)


def parse_dbt(sentence, bad=False):
    message = parse(sentence, checksums='my_data_is_corrupt')
    if not hasattr(message, 'depth_fathoms'):
        raise DepthAuditError('not a DBT/DBK/DBS depth sentence: %r' % sentence)
    values = (
        to_decimal(message.depth_feet),
        to_decimal(message.depth_meters),
        to_decimal(message.depth_fathoms),
    )
    return Observation(sentence.strip(), values[0], values[1], values[2], bad)


def load_observations(lines, bad_markers=('# BAD', '# BAD SAMPLE')):
    observations = []
    bad_markers = tuple(marker.upper() for marker in bad_markers)
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        bad = any(marker in line.upper() for marker in bad_markers)
        sentence = line.split('#', 1)[0].strip()
        try:
            observations.append(parse_dbt(sentence, bad=bad))
        except (ParseError, DepthAuditError) as exc:
            raise DepthAuditError('line %d: %s' % (line_number, exc)) from exc
    return observations


def mapping_for_label(column, unit_label):
    if unit_label in DBT_UNIT_LABELS[column]:
        return column
    for candidate, labels in DBT_UNIT_LABELS.items():
        if unit_label in labels and candidate != column:
            return candidate
    raise DepthAuditError('unknown unit label %r' % unit_label)


def label_mapping(sentence):
    message = parse(sentence, checksums='my_data_is_corrupt')
    labels = (
        message.unit_feet,
        message.unit_meters,
        message.unit_fathoms,
    )
    mapping = {}
    for physical_column, label in zip(DBT_COLUMNS, labels):
        mapping[mapping_for_label(physical_column, label)] = physical_column
    return mapping


def standard_mapping():
    return {column: column for column in DBT_COLUMNS}


def swapped_feet_meters_mapping():
    return {
        'feet': 'meters',
        'meters': 'feet',
        'fathoms': 'fathoms',
    }


def physical_column(mapping, display_column):
    for physical, display in mapping.items():
        if display == display_column:
            return physical
    raise DepthAuditError('missing display column %r' % display_column)


def _column_intervals(observations, mapping, display_column):
    intervals = []
    physical = physical_column(mapping, display_column)
    for observation in observations:
        if observation.bad:
            continue
        value = getattr(observation, 'depth_%s' % display_column)
        low, high = rounded_interval(value, precision_of(value))
        intervals.append(tuple(
            meters_for(bound, physical) for bound in (low, high)
        ))
    return intervals


def _evaluate_mapping(observations, mapping):
    evidence = []
    rejected = []
    for observation in observations:
        if observation.bad:
            continue
        intervals = []
        for display_column in DBT_COLUMNS:
            physical = physical_column(mapping, display_column)
            value = getattr(observation, 'depth_%s' % display_column)
            low, high = rounded_interval(value, precision_of(value))
            interval = tuple(
                meters_for(bound, physical) for bound in (low, high)
            )
            intervals.append(interval)
            evidence.append(ColumnEvidence(
                physical,
                DBT_UNIT_LABELS[physical][0],
                value,
                meters_for(value, physical),
                precision_of(value),
                interval,
            ))
        common_low = max(interval[0] for interval in intervals)
        common_high = min(interval[1] for interval in intervals)
        if common_low >= common_high:
            rejected.append(observation.sentence)
    consistent = not rejected
    return AuditResult(mapping, consistent, evidence, rejected, False)


def audit_dbt(observations, mappings=None):
    if not observations:
        raise DepthAuditError('no observations')
    if mappings is None:
        mappings = (standard_mapping(), swapped_feet_meters_mapping())
    results = [_evaluate_mapping(observations, mapping) for mapping in mappings]
    consistent = [result for result in results if result.consistent]
    if len(consistent) == 1:
        return consistent[0]
    if len(consistent) > 1:
        return consistent[0]._replace(ambiguous=True)
    return results[0]


def common_depth_interval(observations, mapping, display_column):
    intervals = _column_intervals(observations, mapping, display_column)
    if not intervals:
        raise DepthAuditError('no usable observations')
    return max(interval[0] for interval in intervals), min(
        interval[1] for interval in intervals
    )


def distinguishing_depth(observations, mappings, valid_range_meters):
    minimum, maximum = map(Decimal, valid_range_meters)
    if minimum > maximum:
        raise DepthAuditError('invalid device range')
    sample = next(item for item in observations if not item.bad)
    precisions = {}
    for mapping in mappings:
        for display_column in DBT_COLUMNS:
            precisions[(id(mapping), display_column)] = precision_of(
                getattr(sample, 'depth_%s' % display_column)
            )

    step = Decimal('0.001')
    depth = minimum
    while depth <= maximum:
        outputs = []
        for mapping in mappings:
            output = {}
            for display_column in DBT_COLUMNS:
                physical = physical_column(mapping, display_column)
                output[display_column] = column_value(
                    depth, physical, precisions[(id(mapping), display_column)]
                )
            outputs.append(output)
        if outputs[0] != outputs[1]:
            return depth
        depth += step
    return None
