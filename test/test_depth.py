import operator
from decimal import Decimal

import pytest

import pynmea2
from pynmea2.depth import (
    FEET_PER_METER,
    FATHOMS_PER_METER,
    DepthAuditError,
    audit_dbt,
    distinguishing_depth,
    label_mapping,
    load_observations,
    parse_dbt,
    standard_mapping,
    swapped_feet_meters_mapping,
)


def checksum(body):
    return '%02X' % (
        __import__('functools').reduce(operator.xor, map(ord, body), 0)
    )


def dbt(feet, feet_label, meters, meter_label, fathoms, fathom_label='F'):
    body = 'SDDBT,%s,%s,%s,%s,%s,%s' % (
        feet, feet_label, meters, meter_label, fathoms, fathom_label,
    )
    return '$%s*%s' % (body, checksum(body))


def test_parse_dbt_keeps_three_display_precisions():
    sentence = dbt('6.6', 'f', '2.0', 'M', '1.1')
    observation = parse_dbt(sentence)

    assert observation.sentence == sentence
    assert observation.depth_feet == Decimal('6.6')
    assert observation.depth_meters == Decimal('2.0')
    assert observation.depth_fathoms == Decimal('1.1')
    assert observation.bad is False


def test_bad_marker_is_retained_but_not_used_as_evidence():
    good = dbt('3.3', 'f', '1.0', 'M', '0.5')
    bad = dbt('99.9', 'f', '1.0', 'M', '0.5') + '  # BAD'

    observations = load_observations([good, bad])

    assert [item.bad for item in observations] == [False, True]
    result = audit_dbt(observations)
    assert result.consistent is True
    assert result.mapping == standard_mapping()
    assert all(item.value != Decimal('99.9') for item in result.evidence)


def test_label_mapping_detects_swapped_feet_and_meters_labels():
    sentence = dbt('6.6', 'M', '2.0', 'f', '1.1')
    assert label_mapping(sentence) == {
        'feet': 'meters',
        'meters': 'feet',
        'fathoms': 'fathoms',
    }


def test_standard_mapping_is_the_only_consistent_mapping_for_deep_observation():
    # 20.0 displayed under the feet label cannot be the same physical depth as
    # 65.6 displayed under the meters label once the fathoms column is included.
    sentence = dbt('20.0', 'f', '65.6', 'M', '10.9')
    standard = audit_dbt([parse_dbt(sentence)], (standard_mapping(),))
    swapped = audit_dbt(
        [parse_dbt(sentence)], (swapped_feet_meters_mapping(),)
    )

    assert standard.consistent is False
    assert swapped.consistent is True
    result = audit_dbt([parse_dbt(sentence)])
    assert result.consistent is True
    assert result.ambiguous is False
    assert result.mapping == swapped_feet_meters_mapping()
    assert {item.column for item in result.evidence} == {
        'feet', 'meters', 'fathoms',
    }


def test_swapped_mapping_is_rejected_by_three_column_rounding_intervals():
    sentence = dbt('20.0', 'f', '65.6', 'M', '10.9')
    standard = audit_dbt([parse_dbt(sentence)], (standard_mapping(),))

    assert standard.consistent is False
    assert standard.rejected == [sentence]
    swapped = audit_dbt(
        [parse_dbt(sentence)],
        (swapped_feet_meters_mapping(),),
    )
    assert swapped.consistent is True
    assert swapped.rejected == []


def test_shallow_observation_can_be_ambiguous_between_two_mappings():
    # At the surface, 0.0 in every 0.1-resolution column is compatible with
    # either feet/meters correspondence.
    sentence = dbt('0.0', 'f', '0.0', 'M', '0.0')
    standard = audit_dbt([parse_dbt(sentence)], (standard_mapping(),))
    swapped = audit_dbt([parse_dbt(sentence)], (
        swapped_feet_meters_mapping(),
    ))

    assert standard.consistent is True
    assert swapped.consistent is True
    result = audit_dbt([parse_dbt(sentence)])
    assert result.ambiguous is True


def test_distinguishing_depth_uses_device_range_and_display_precision():
    observations = [parse_dbt(dbt('3.3', 'f', '1.0', 'M', '0.5'))]
    depth = distinguishing_depth(
        observations,
        (standard_mapping(), swapped_feet_meters_mapping()),
        (Decimal('0'), Decimal('20')),
    )

    assert depth is not None
    assert Decimal('0') <= depth <= Decimal('20')
    standard_output = (
        (depth * FEET_PER_METER).quantize(Decimal('0.1')),
        depth.quantize(Decimal('0.1')),
        (depth * FATHOMS_PER_METER).quantize(Decimal('0.1')),
    )
    swapped_output = (
        depth.quantize(Decimal('0.1')),
        (depth * FEET_PER_METER).quantize(Decimal('0.1')),
        (depth * FATHOMS_PER_METER).quantize(Decimal('0.1')),
    )
    assert standard_output != swapped_output


def test_distinguishing_depth_returns_none_when_range_cannot_separate_labels():
    observations = [parse_dbt(dbt('0.0', 'f', '0.0', 'M', '0.0'))]
    depth = distinguishing_depth(
        observations,
        (standard_mapping(), swapped_feet_meters_mapping()),
        (Decimal('0'), Decimal('0.01')),
    )

    assert depth is None


def test_blank_column_is_an_audit_error():
    with pytest.raises(DepthAuditError):
        parse_dbt('$SDDBT,,f,1.0,M,0.5*00')
