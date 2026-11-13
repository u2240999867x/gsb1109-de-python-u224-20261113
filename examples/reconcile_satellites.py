"""
Reconcile GSA (satellites used for the fix) against GSV (satellites in view).

The first log is a modern multi-constellation receiver whose GSV cycles carry
NMEA 4.10 signal IDs: dual-band observations of the same satellite must fold
back to one physical satellite, while equal short numbers across different
constellations must not.

The second log mimics a legacy receiver that only emits generic GN sentences
with local numbers: the constellation cannot be proven, so a provable count
range and the remaining ambiguous numbers are reported instead.
"""

from pynmea2 import NMEASentence, SatelliteReconciler


def checksummed(body):
    checksum = NMEASentence.checksum(body)
    return '${}*{:02X}'.format(body, checksum)


modern_log = [
    # GPS GSV cycle: PRN 02 is observed on two frequency bands.
    'GNGSV,2,1,05,02,40,120,30,1,02,38,118,27,5,05,35,200,28,1,07,25,080,25,1',
    # QZSS 193 and BeiDou 201 use extended, self-describing satellite IDs.
    'GNGSV,2,2,05,193,50,210,35,49,201,35,090,40,36',
    # GLONASS cycle: slot 05 must NOT merge with GPS PRN 05.
    'GLGSV,1,1,03,05,45,090,33,10,20,150,22,11,08,270,18',
    # Fix uses twelve satellite slots; only nine distinct numbers appear above.
    'GPGSA,A,3,02,05,07,,,,,,,,,,1.5,1.2,0.9',
    'GLGSA,A,3,05,10,11,,,,,,,,,,2.0,1.4,1.4',
    'QZGSA,A,3,193,194,195,,,,,,,,,1.1,0.9,0.6',
    'GAGSA,A,3,07,08,09,,,,,,,,,,1.3,1.0,0.8',
]

legacy_log = [
    # Generic GN talker, four-field GSV blocks, local numbers only: there is no
    # system identifier anywhere in these sentences.
    'GNGSV,3,1,09,02,40,120,30,05,35,200,28,07,25,080,25',
    'GNGSV,3,2,09,09,30,150,24,11,20,060,22,13,15,300,18',
    'GNGSV,3,3,09,15,10,250,16,19,08,180,12,24,05,090,',
    # The monthly report counts twelve used satellite slots.
    'GNGSA,A,3,02,05,07,09,11,13,15,19,24,26,28,30,1.6,1.6,1.0',
]


def run(title, log):
    print('=' * 72)
    print(title)
    print('=' * 72)
    reconciler = SatelliteReconciler.from_lines(
        checksummed(line) + '\n' for line in log)
    print(reconciler.report())
    print('')
    return reconciler


modern = run('Modern log: constellation and signal are provable', modern_log)
legacy = run('Legacy log: no system identifier, only a provable range', legacy_log)

# Distinct physical satellites behind the modern GSV signal-level entries.
print('Modern GSV signal blocks: {} -> {} distinct physical satellites'.format(
    modern.gsv_signal_entry_count(), len(modern.seen_satellites())))
print('GSA slots used: {}'.format(modern.gsa_slot_count()))
print('Used but never seen in GSV: {}'.format(
    sorted(modern.used_not_seen(), key=lambda sat: (sat.constellation,
                                                     sat.number))))

ambiguous = [entry['number'] for entry in legacy.legacy_ambiguity()['ambiguous']]
print('Legacy numbers that still cannot be assigned to a system: {}'.format(
    ambiguous))
