import pynmea2
from pynmea2 import Satellite, SatelliteReconciler
from pynmea2.nmea import NMEASentence


def sentence(body):
    """Build a checksummed NMEA sentence from the body (without '$'/'*hh')."""
    checksum = NMEASentence.checksum(body)
    return '${}*{:02X}'.format(body, checksum)


def feed(reconciler, *bodies):
    for body in bodies:
        reconciler.feed(pynmea2.parse(sentence(body)))
    return reconciler


def test_talker_constellation_and_cross_constellation_short_number():
    # GPS PRN 05 and GLONASS slot 05 share a short number but must not merge.
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GPGSV,2,1,04,02,40,120,30,05,35,200,28,07,25,080,25',
        'GPGSV,2,2,04,09,10,300,20',
        'GLGSV,1,1,03,05,45,090,33,10,20,150,22,11,08,270,',
        'GPGSA,A,3,02,05,07,,,,,,,,,,1.5,1.2,0.9',
        'GLGSA,A,3,05,10,,,,,,,,,,,2.0,1.4,1.4',
    )

    seen = reconciler.seen_satellites()
    assert Satellite('GPS', 5) in seen
    assert Satellite('GLONASS', 5) in seen
    assert len(seen) == 7  # 4 GPS + 3 GLONASS, short number 05 counted twice

    used = reconciler.used_satellites()
    assert used == {
        Satellite('GPS', 2), Satellite('GPS', 5), Satellite('GPS', 7),
        Satellite('GLONASS', 5), Satellite('GLONASS', 10),
    }
    assert reconciler.seen_not_used() == {
        Satellite('GPS', 9), Satellite('GLONASS', 11)}
    assert reconciler.used_not_seen() == set()
    assert reconciler.gsv_rounds[0].constellation == 'GPS'
    assert reconciler.gsv_rounds[1].constellation == 'GLONASS'
    assert all(round_.complete for round_ in reconciler.gsv_rounds)


def test_dual_band_observations_fold_back_to_one_satellite():
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        # Five-field GSV blocks carry the NMEA 4.10 signal ID; the IDs prove
        # that the local numbers on the mixed GN talker belong to GPS.
        'GNGSV,2,1,04,01,45,120,30,1,01,40,110,28,5,02,30,200,25,1',
        'GNGSV,2,2,04,193,50,210,35,49,201,35,090,40,36',
        'GPGSA,A,3,01,02,,,,,,,,,,,1.2,1.0,0.7',
        'QZGSA,A,3,193,,,,,,,,,,1.2,1.0,0.7',
    )

    assert reconciler.gsv_signal_entry_count() == 5
    seen = reconciler.seen_satellites()
    assert seen == {
        Satellite('GPS', 1), Satellite('GPS', 2),
        Satellite('QZSS', 193), Satellite('BeiDou', 201),
    }

    summary = reconciler.signal_summary()
    assert summary[Satellite('GPS', 1)] == [1, 5]  # L1 C/A + L5-I
    assert summary[Satellite('QZSS', 193)] == [49]
    dual_band = reconciler.dual_band_satellites()
    assert list(dual_band) == [Satellite('GPS', 1)]
    assert pynmea2.signal_name(1) == 'GPS L1 C/A'
    assert pynmea2.signal_name(5) == 'GPS L5-I'
    assert pynmea2.signal_constellation(5) == 'GPS'
    assert pynmea2.signal_constellation(36) == 'BeiDou'

    result = reconciler.reconcile()
    assert result['declared_mismatches'] == []
    assert {sat for sat in result['distinct_used']
            if sat.constellation == 'GPS'} == {Satellite('GPS', 1),
                                                Satellite('GPS', 2)}


def test_extended_and_local_constellation_resolution():
    assert pynmea2.resolve_constellation('GP', 5) == 'GPS'
    assert pynmea2.resolve_constellation('GL', 5) == 'GLONASS'
    assert pynmea2.resolve_constellation('GA', 7) == 'Galileo'
    # Generic GN talker: local numbers cannot be assigned to a system.
    assert pynmea2.resolve_constellation('GN', 5) is None
    # Extended IDs are self-describing regardless of talker.
    assert pynmea2.resolve_constellation('GN', 65) == 'GLONASS'
    assert pynmea2.resolve_constellation('GN', 97) == 'Galileo'
    assert pynmea2.resolve_constellation('GN', 120) == 'BeiDou'
    assert pynmea2.resolve_constellation('GN', 193) == 'QZSS'
    assert pynmea2.resolve_constellation('GP', 193) == 'QZSS'
    assert pynmea2.extended_constellation(201) == 'BeiDou'
    assert pynmea2.extended_constellation(33) == 'SBAS'
    assert pynmea2.signal_name(99) is None


def test_legacy_sentences_give_provable_range_and_ambiguous_numbers():
    # Old mixed-talker sentences with local numbers only: no provable system.
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GNGSV,3,1,09,02,40,120,30,05,35,200,28,07,25,080,25',
        'GNGSV,3,2,09,09,30,150,24,11,20,060,22,13,15,300,18',
        'GNGSV,3,3,09,15,10,250,16,19,08,180,12,24,05,090,',
        # Monthly-report style: twelve satellite slots in one GSA sentence.
        'GNGSA,A,3,02,05,07,09,11,13,15,19,24,26,28,30,1.6,1.6,1.0',
    )

    result = reconciler.reconcile()
    legacy = result['legacy']
    ambiguous_numbers = [entry['number'] for entry in legacy['ambiguous']]
    assert ambiguous_numbers == [2, 5, 7, 9, 11, 13, 15, 19, 24, 26, 28, 30]

    # 12 distinct numbers, every one unresolved -> at least one satellite each.
    assert legacy['min'] == 12
    # 9 numbers appear twice (GSV + GSA), 3 numbers once (GSA only); each low
    # number may legally exist in five candidate constellations.
    assert legacy['max'] == 9 * 2 + 3
    assert result['provable_total_range'] == (12, 21)

    by_number = {entry['number']: entry for entry in legacy['ambiguous']}
    assert by_number[5]['candidates'] == [
        'BeiDou', 'GLONASS', 'GPS', 'Galileo', 'NavIC']
    assert by_number[5]['occupied_constellations'] == []
    assert by_number[5]['new_range'] == (1, 2)
    assert by_number[26]['new_range'] == (1, 1)

    # GSA-only numbers are still flagged as used but not seen in GSV.
    assert {sat.number for sat in result['used_not_seen']} == {26, 28, 30}
    assert all(sat.constellation is None for sat in result['distinct_used'])
    report = reconciler.report()
    assert 'provable total range 12-21' in report
    assert 'number 05' in report


def test_legacy_evidence_occupies_but_does_not_pin_other_systems():
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GPGSV,1,1,01,05,40,120,30',       # identified GPS PRN 05
        'GNGSV,1,1,02,05,35,200,28,10,20,150,22',  # unidentified 05 and 10
        'GNGSA,A,3,05,10,,,,,,,,,,1.5,1.2,0.9',
    )
    legacy = reconciler.legacy_ambiguity()
    by_number = {entry['number']: entry for entry in legacy['ambiguous']}

    # The unidentified 05 rows may fold onto the known GPS 05 (adds nothing),
    # but could also be a GLONASS/Galileo/BeiDou/NavIC slot 05.
    entry_05 = by_number[5]
    assert entry_05['occupied_constellations'] == ['GPS']
    assert 'GLONASS' in entry_05['free_constellations']
    assert entry_05['new_range'] == (0, 2)  # rows: one GSV + one GSA
    # Number 10 has no identified evidence and two rows as well.
    assert by_number[10]['new_range'] == (1, 2)
    assert (legacy['min'], legacy['max']) == (1, 4)


def test_short_final_gsv_message_and_snr_validity():
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GPGSV,3,1,09,12,28,063,33,14,63,000,32,22,68,150,26,25,40,109,23',
        'GPGSV,3,2,09,31,42,227,19,32,17,313,20,01,09,316,,11,08,292,',
        'GPGSV,3,3,09,24,03,046,',
    )
    assert len(reconciler.gsv_rounds) == 1
    round_ = reconciler.gsv_rounds[0]
    assert round_.complete is True
    assert round_.declared_in_view == 9
    assert len(round_.observations) == 9
    last = round_.observations[-1]
    assert last.number == 24 and last.snr is None
    assert len(reconciler.valid_gsv_rounds) == 1


def test_rounds_are_retained_and_incomplete_cycle_flushed():
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GPGSV,1,1,03,01,40,120,30,02,35,200,28,03,25,080,25',
        'GPGSV,1,1,03,01,41,121,31,02,36,201,29,04,26,081,26',
        'GPGSV,2,1,05,05,30,150,24',
    )
    assert len(reconciler.gsv_rounds) == 2
    assert len(reconciler.gsv_rounds[0].observations) == 3
    assert len(reconciler.gsv_rounds[1].observations) == 3
    assert reconciler.seen_satellites() == {
        Satellite('GPS', number) for number in (1, 2, 3, 4)}

    reconciler.flush()
    assert len(reconciler.gsv_rounds) == 3
    assert reconciler.gsv_rounds[-1].complete is False
    assert reconciler.gsv_rounds[-1].observations[0].number == 5


def test_declared_count_mismatch_after_folding():
    # Declares 4 in view, but the dual-band rows describe only 2 satellites.
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GNGSV,1,1,04,01,45,120,30,1,01,40,110,28,5,02,30,200,25,1',
    )
    result = reconciler.reconcile()
    assert result['declared_mismatches'] == [(1, 4, 2)]
    assert 'declares 4 satellites in view but lists 2' in reconciler.report()


def test_consume_and_from_lines():
    bodies = [
        'GPGSV,1,1,01,01,40,120,30',
        'GPGSA,A,3,01,,,,,,,,,,,1.5,1.2,0.9',
        'not-an-nmea-sentence',
    ]
    lines = [sentence(body) + '\n' for body in bodies[:2]]
    reconciler = SatelliteReconciler.from_lines(lines)
    assert reconciler.seen_satellites() == {Satellite('GPS', 1)}
    assert reconciler.used_satellites() == {Satellite('GPS', 1)}

    messages = [pynmea2.parse(sentence(body)) for body in bodies[:2]]
    assert SatelliteReconciler().consume(messages).gsa_slot_count() == 1


def test_gsa_slot_count_keeps_repeats_separate():
    reconciler = SatelliteReconciler()
    feed(
        reconciler,
        'GPGSA,A,3,01,02,03,,,,,,,,,,1.5,1.2,0.9',
        'GPGSA,A,3,01,02,04,,,,,,,,,,1.4,1.1,0.8',
    )
    assert reconciler.gsa_slot_count() == 6
    assert reconciler.used_satellites() == {
        Satellite('GPS', 1), Satellite('GPS', 2),
        Satellite('GPS', 3), Satellite('GPS', 4)}
    gsa = reconciler.gsa_rounds[0]
    assert gsa.pdop == 1.5 and gsa.hdop == 1.2 and gsa.vdop == 0.9
    assert gsa.fix_type == 3 and gsa.mode == 'A'


def test_gsv_without_satellite_blocks_does_not_enable_signal_mode():
    reconciler = SatelliteReconciler()
    # Header-only first message followed by a normal legacy four-block cycle.
    feed(
        reconciler,
        'GPGSV,2,1,04',
        'GPGSV,2,2,04,01,40,120,30',
    )
    assert reconciler.gsv_rounds[0].observations[0].signal_id is None
    assert reconciler.gsv_rounds[0].observations[0].number == 1
