import datetime

import pytest

import pynmea2
from pynmea2 import FirmwareRevisionRegistry, diff_fields
from pynmea2.types.proprietary.acm import ACMFLX


SWITCH = datetime.datetime(2026, 3, 1, 0, 0)
FW1_FIELDS = ACMFLX.revisions['fw1']
FW2_FIELDS = ACMFLX.revisions['fw2']


def _checksummed(body):
    return '$%s*%02X' % (body, pynmea2.NMEASentence.checksum(body))


# Raw sentence: the value in column 3/4 is ambiguous between temperature and
# pressure because the firmware swap only permutes the columns.
BODY = 'PACM,FLX,7,21.5,1013.0,OK'
LINE = _checksummed(BODY)


def _registry_with_timeline(device='dev-A'):
    registry = FirmwareRevisionRegistry()
    registry.announce(
        'ACM', 'FLX', 'fw1', fields=FW1_FIELDS,
        effective_until=SWITCH, device=device)
    registry.announce(
        'ACM', 'FLX', 'fw2', fields=FW2_FIELDS,
        effective_from=SWITCH, device=device, replaces='fw1')
    return registry


def test_checksum_and_column_count_are_invariant_under_swap():
    # permuting columns keeps the same bytes, hence the same XOR checksum
    assert pynmea2.NMEASentence.checksum('PACM,FLX,7,21.5,1013.0,OK') == \
        pynmea2.NMEASentence.checksum('PACM,FLX,7,1013.0,21.5,OK')
    assert len(FW1_FIELDS) == len(FW2_FIELDS)


def test_no_evidence_retains_both_readings():
    registry = FirmwareRevisionRegistry()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 1, 1))

    assert isinstance(msg, ACMFLX)
    assert msg.ambiguous is True
    assert msg.revision is None
    assert msg.revision_candidates == ['fw1', 'fw2']
    assert msg.raw_sentence == LINE

    readings = {m.revision: m for m in msg.interpretations()}
    assert set(readings) == {'fw1', 'fw2'}
    assert readings['fw1'].temperature == 21.5
    assert readings['fw1'].pressure == 1013.0
    assert readings['fw2'].temperature == 1013.0
    assert readings['fw2'].pressure == 21.5


def test_open_intervals_keep_both_readings():
    registry = FirmwareRevisionRegistry()
    registry.announce('ACM', 'FLX', 'fw1', fields=FW1_FIELDS,
                      device='dev-A')
    registry.announce('ACM', 'FLX', 'fw2', fields=FW2_FIELDS,
                      device='dev-A')

    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 2, 1))
    assert msg.ambiguous is True
    assert msg.revision_candidates == ['fw1', 'fw2']


def test_evidenced_timeline_selects_by_time():
    registry = _registry_with_timeline()

    before = pynmea2.parse(LINE, registry=registry, device='dev-A',
                           at=datetime.datetime(2026, 2, 28))
    assert before.revision == 'fw1'
    assert before.ambiguous is False
    assert before.temperature == 21.5
    assert before.pressure == 1013.0

    after = pynmea2.parse(LINE, registry=registry, device='dev-A',
                          at=datetime.datetime(2026, 3, 2))
    assert after.revision == 'fw2'
    assert after.temperature == 1013.0
    assert after.pressure == 21.5


def test_late_announcement_reinterprets_original_message():
    registry = FirmwareRevisionRegistry()
    # the raw message is observed long before the firmware notice arrives
    original = pynmea2.parse(LINE, registry=registry, device='dev-A',
                             at=datetime.datetime(2026, 1, 1))
    assert original.ambiguous is True

    # the announcement only arrives in June, but pins a March switch
    diff = registry.announce(
        'ACM', 'FLX', 'fw1', fields=FW1_FIELDS,
        effective_until=SWITCH, device='dev-A')
    assert diff['changed_fields'] == []
    registry.announce(
        'ACM', 'FLX', 'fw2', fields=FW2_FIELDS,
        effective_from=SWITCH, device='dev-A', replaces='fw1')

    reinterpreted = original.reinterpret()
    assert reinterpreted is not original
    assert reinterpreted.raw_sentence == LINE
    assert reinterpreted.revision == 'fw1'
    assert reinterpreted.temperature == 21.5
    assert reinterpreted.pressure == 1013.0


def test_interpretations_converge_after_switch_is_pinned():
    registry = FirmwareRevisionRegistry()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 1, 1))
    assert [m.revision for m in msg.interpretations()] == ['fw1', 'fw2']

    msg.apply_announcement('fw1', effective_until=SWITCH)
    msg.apply_announcement('fw2', effective_from=SWITCH, replaces='fw1')

    converged = msg.interpretations()
    assert len(converged) == 1
    assert converged[0].revision == 'fw1'
    assert converged[0].temperature == 21.5


def test_apply_announcement_returns_reinterpreted_message():
    registry = FirmwareRevisionRegistry()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 2, 1))
    assert msg.ambiguous is True

    pinned = msg.apply_announcement(
        'fw2', effective_from=SWITCH, replaces='fw1')
    assert pinned.revision == 'fw1'  # Feb observation precedes March switch
    assert pinned.temperature == 21.5


def test_device_does_not_inherit_another_devices_version():
    registry = _registry_with_timeline(device='dev-A')

    # a different device on the post-switch date must not adopt dev-A's fw2
    other = pynmea2.parse(LINE, registry=registry, device='dev-B',
                          at=datetime.datetime(2026, 4, 1))
    assert other.ambiguous is True
    assert other.revision_candidates == ['fw1', 'fw2']

    # an unidentified device likewise inherits nothing
    unknown = pynmea2.parse(LINE, registry=registry,
                            at=datetime.datetime(2026, 4, 1))
    assert unknown.ambiguous is True


def test_forced_revision_overrides_timeline():
    registry = _registry_with_timeline()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 4, 1),
                        force_revision='fw1')
    assert msg.revision == 'fw1'
    assert msg.temperature == 21.5

    with pytest.raises(ValueError):
        pynmea2.parse(LINE, registry=registry, device='dev-A',
                      force_revision='fw9')


def test_announcement_diff_lists_changed_published_fields():
    registry = FirmwareRevisionRegistry()
    registry.announce('ACM', 'FLX', 'fw1', fields=FW1_FIELDS,
                      device='dev-A')
    diff = registry.announce(
        'ACM', 'FLX', 'fw2', fields=FW2_FIELDS,
        effective_from=SWITCH, device='dev-A', replaces='fw1')

    # only the two swapped columns are reported as changed fields
    assert diff['changed_fields'] == ['pressure', 'temperature']
    assert ('pressure', 4, 3) in diff['moved']
    assert ('temperature', 3, 4) in diff['moved']
    assert diff['added'] == []
    assert diff['removed'] == []

    history = registry.announcements('ACM', 'FLX', device='dev-A')
    assert [entry['revision'] for entry in history] == ['fw1', 'fw2']
    assert history[-1]['diff']['changed_fields'] == \
        ['pressure', 'temperature']


def test_single_announcement_diffs_against_published_previous():
    # only the fw2 notice arrives; fw1 exists only as a published class
    # definition, but the announcement must still report the swapped columns
    registry = FirmwareRevisionRegistry()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 1, 1))
    msg.apply_announcement('fw2', effective_from=SWITCH, replaces='fw1')

    history = registry.announcements('ACM', 'FLX', device='dev-A')
    assert len(history) == 1
    diff = history[0]['diff']
    assert diff['changed_fields'] == ['pressure', 'temperature']
    assert ('pressure', 4, 3) in diff['moved']
    assert ('temperature', 3, 4) in diff['moved']


def test_diff_fields_categories():
    old = (
        ('Alpha label', 'alpha'),
        ('Beta label', 'beta', int),
        ('Gamma label', 'gamma'),
    )
    new = (
        ('Alpha renamed', 'alpha'),
        ('Gamma label', 'gamma'),
        ('Beta label', 'beta', float),
        ('Delta label', 'delta'),
    )
    result = diff_fields(old, new)
    assert result['added'] == ['delta']
    assert result['removed'] == []
    assert ('gamma', 2, 1) in result['moved']
    assert ('beta', 1, 2) in result['moved']
    assert ('alpha', 'Alpha label', 'Alpha renamed') in result['relabeled']
    assert result['type_changed'][0][0] == 'beta'
    assert result['changed_fields'] == ['delta', 'gamma', 'beta', 'alpha']


def test_render_repr_and_converters_round_trip():
    registry = _registry_with_timeline()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 1, 1))

    assert msg.render() == LINE
    assert msg.manufacturer == 'ACM'
    assert msg.sentence_type == 'ACMFLX'
    assert msg.sequence == 7          # int converter
    assert msg.subtype == 'FLX'
    assert repr(msg).startswith('<ACMFLX(')
    assert "temperature=21.5" in repr(msg)


def test_date_and_epoch_observation_times():
    registry = _registry_with_timeline()
    via_date = pynmea2.parse(LINE, registry=registry, device='dev-A',
                             at=datetime.date(2026, 4, 1))
    assert via_date.revision == 'fw2'

    epoch = datetime.datetime(2026, 4, 1,
                              tzinfo=datetime.timezone.utc).timestamp()
    via_epoch = pynmea2.parse(LINE, registry=registry, device='dev-A',
                              at=epoch)
    assert via_epoch.revision == 'fw2'


def test_registry_reset_clears_timeline():
    registry = _registry_with_timeline()
    registry.reset()
    msg = pynmea2.parse(LINE, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 4, 1))
    assert msg.ambiguous is True
    assert registry.announcements('ACM', 'FLX', device='dev-A') == []


def test_checksum_mode_preserved_across_reinterpretation():
    registry = _registry_with_timeline()
    corrupt = '$PACM,FLX,7,99.9,1013.0,OK*57'  # wrong checksum
    msg = pynmea2.parse(corrupt, registry=registry, device='dev-A',
                        at=datetime.datetime(2026, 1, 1),
                        checksums='my_data_is_corrupt')
    again = msg.reinterpret()
    assert again.raw_sentence == corrupt
    assert again.temperature == 99.9


def test_standard_sentences_unaffected_by_new_arguments():
    registry = _registry_with_timeline()
    talker = pynmea2.parse(
        '$GPGGA,184353.07,1929.045,S,02410.506,E,1,04,2.6,100.00,M,'
        '-33.9,M,,0000*6D',
        registry=registry, at=datetime.datetime(2026, 1, 1),
        device='dev-A')
    assert talker.sentence_type == 'GGA'
    assert talker.altitude == 100.0

    unknown = pynmea2.parse('$PZZZABC,1,2,3')
    assert type(unknown) is pynmea2.ProprietarySentence
