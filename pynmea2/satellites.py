"""
GSA/GSV satellite reconciliation helpers.

The plain GSA/GSV sentence types in :mod:`pynmea2.types.talker` only expose the
raw fields.  This module adds the bookkeeping needed to answer questions such
as "how many distinct physical satellites did the receiver actually use?":

* the constellation is derived from the talker ID (``GP``/``GL``/``GA``/...)
  and, for NMEA 4.10+ mixed (``GN``) sentences, from the extended satellite ID
  ranges;
* NMEA 4.10+ GSV blocks may carry a signal ID, so the same physical satellite
  can show up once per frequency band.  Observations are folded back to a single
  satellite while every signal ID is retained;
* satellites are only ever merged when both constellation and satellite number
  match, so a GPS PRN 05 and a GLONASS slot 05 never collapse together;
* legacy sentences without a usable system identifier cannot be assigned to a
  constellation, in which case a provable lower/upper count and the list of
  satellite numbers that remain ambiguous are returned.
"""

from collections import OrderedDict, namedtuple

from .nmea import NMEASentence, ParseError


# Talker IDs that identify a single constellation.  ``GN`` is deliberately
# absent: a generic/mixed talker carries no constellation information on its own.
TALKER_CONSTELLATIONS = {
    'GP': 'GPS',
    'GL': 'GLONASS',
    'GA': 'Galileo',
    'GB': 'BeiDou',
    'BD': 'BeiDou',
    'GI': 'NavIC',
    'GQ': 'QZSS',
    'QZ': 'QZSS',
    'SB': 'SBAS',
    'WA': 'SBAS',
    'EG': 'SBAS',
}

# NMEA 4.10 extended satellite ID ranges, as emitted by u-blox class receivers
# in mixed (GN) sentences.  Local PRN numbers (<= 63) inside a constellation
# specific sentence are resolved through the talker instead.
EXTENDED_SATELLITE_RANGES = (
    ((1, 32), 'GPS'),
    ((33, 64), 'SBAS'),
    ((65, 96), 'GLONASS'),
    ((97, 119), 'Galileo'),
    ((120, 158), 'BeiDou'),
    ((159, 163), 'NavIC'),
    ((173, 182), 'MSS'),
    ((193, 199), 'QZSS'),
    ((201, 237), 'BeiDou'),
)

# Best-effort NMEA 4.10 signal ID names (u-blox documented subset).  The raw
# numeric ID is always preserved even when no name is known.
SIGNAL_NAMES = {
    1: 'GPS L1 C/A',
    2: 'GPS L1 P(Y)',
    3: 'GPS L2 P(Y)',
    4: 'GPS L2C-M',
    5: 'GPS L5-I',
    6: 'GPS L5-Q',
    7: 'GPS L1C-D',
    8: 'GPS L1C-P',
    9: 'GPS L1C(D+P)',
    10: 'GPS L2C-L',
    11: 'GPS L2C(M+L)',
    14: 'GLONASS L1 OF',
    15: 'GLONASS L2 OF',
    16: 'GLONASS L1 DF',
    17: 'GLONASS L2 DF',
    18: 'GLONASS L3-I',
    19: 'GLONASS L3-Q',
    20: 'GLONASS L3(I+Q)',
    22: 'Galileo E1-C',
    23: 'Galileo E1-B',
    24: 'Galileo E1(B+C)',
    25: 'Galileo E5a-I',
    26: 'Galileo E5a-Q',
    27: 'Galileo E5a(I+Q)',
    28: 'Galileo E5b-I',
    29: 'Galileo E5b-Q',
    30: 'Galileo E5b(I+Q)',
    31: 'Galileo E5ab-I',
    32: 'Galileo E5ab-Q',
    33: 'Galileo E5ab(I+Q)',
    36: 'BeiDou B1I D1',
    37: 'BeiDou B1I D2',
    38: 'BeiDou B2I D1',
    39: 'BeiDou B2I D2',
    40: 'BeiDou B1C-D',
    41: 'BeiDou B1C-P',
    43: 'BeiDou B2a-D',
    44: 'BeiDou B2a-P',
    46: 'BeiDou B2b-I',
    47: 'BeiDou B2b-Q',
    49: 'QZSS L1 C/A',
    50: 'QZSS L1S',
    51: 'QZSS L2C-M',
    52: 'QZSS L5-I',
    53: 'QZSS L5-Q',
    59: 'NavIC L5-SPS',
    61: 'SBAS L1 C/A',
    62: 'SBAS L5-I',
    63: 'SBAS L5-Q',
}

# Local (non extended) satellite number ranges per constellation.  Used to
# decide which constellations an unidentified legacy satellite number could
# provably belong to.
CONSTELLATION_NUMBER_RANGES = {
    'GPS': (1, 32),
    'GLONASS': (1, 32),
    'Galileo': (1, 36),
    'BeiDou': (1, 63),
    'NavIC': (1, 14),
    'SBAS': (33, 64),
    'MSS': (173, 182),
    'QZSS': (193, 199),
}

# A physical satellite.  Two observations only describe the same satellite when
# both fields match: this is what stops cross-constellation PRN collisions from
# being folded together.
Satellite = namedtuple('Satellite', ['constellation', 'number'])

# One GSV satellite block (one signal on one satellite).
SignalObservation = namedtuple(
    'SignalObservation',
    ['constellation', 'number', 'signal_id', 'elevation', 'azimuth', 'snr',
     'talker'])

# A complete (or best-effort) GSV message cycle for one constellation.
GSVRound = namedtuple(
    'GSVRound',
    ['round_no', 'constellation', 'declared_in_view', 'complete',
     'observations'])

# One GSA sentence, i.e. the satellite set used for a single fix.
GSARound = namedtuple(
    'GSARound',
    ['round_no', 'constellation', 'mode', 'fix_type', 'satellites',
     'pdop', 'hdop', 'vdop'])


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _satellite_sort_key(satellite):
    # constellation may be None on legacy/unidentified observations.
    return (satellite.constellation is not None,
            satellite.constellation or '',
            satellite.number)


def extended_constellation(number):
    """Return the constellation encoded by an NMEA 4.10 extended satellite ID."""
    if number is None:
        return None
    for (low, high), constellation in EXTENDED_SATELLITE_RANGES:
        if low <= number <= high:
            return constellation
    return None


def resolve_constellation(talker, number):
    """
    Resolve the constellation for a satellite number seen on a given talker.

    Returns ``None`` for legacy/mixed sentences where the constellation cannot
    be proven from the available identifiers.
    """
    # IDs above the legacy PRN space are self-describing in every context
    # (e.g. QZSS 193-199 or u-blox style BeiDou 201-237 inside GPGSA).
    if number is not None and number > 63:
        constellation = extended_constellation(number)
        if constellation is not None:
            return constellation

    talker_constellation = TALKER_CONSTELLATIONS.get(talker)
    if talker_constellation is not None:
        return talker_constellation

    # Generic GN/missing talker: only the extended ranges carry system info.
    if number is not None and number >= 33:
        return extended_constellation(number)
    return None


def signal_name(signal_id):
    """Human readable name for an NMEA 4.10 signal ID, or ``None``."""
    if signal_id is None:
        return None
    return SIGNAL_NAMES.get(signal_id)


# NMEA 4.10 signal ID ranges are constellation specific, so a signal ID on a
# mixed (GN) sentence identifies the system even when the satellite number is
# only a local ID.
SIGNAL_CONSTELLATION_RANGES = (
    ((1, 13), 'GPS'),
    ((14, 21), 'GLONASS'),
    ((22, 35), 'Galileo'),
    ((36, 48), 'BeiDou'),
    ((49, 58), 'QZSS'),
    ((59, 60), 'NavIC'),
    ((61, 64), 'SBAS'),
)


def signal_constellation(signal_id):
    """Constellation encoded by an NMEA 4.10 signal ID, or ``None``."""
    if signal_id is None:
        return None
    for (low, high), constellation in SIGNAL_CONSTELLATION_RANGES:
        if low <= signal_id <= high:
            return constellation
    return None


def _gsv_block_size(rest, signal_ids_known=False):
    """
    A GSV sentence holds up to four satellite blocks of either four fields
    (legacy) or five fields (NMEA 4.10 with trailing signal ID).
    """
    if signal_ids_known:
        return 5
    if not rest:
        return 4
    if len(rest) > 16:
        # More than four legacy blocks: the fifth field must be a signal ID.
        return 5
    if len(rest) % 5 == 0:
        # 5/10/15 trailing fields can only be whole five-field blocks.
        return 5
    return 4


def _parse_gsv(msg, signal_ids_known=False):
    """Turn a GSV sentence into (header, block_size, list[SignalObservation])."""
    data = msg.data
    num_messages = _to_int(data[0]) if len(data) > 0 else None
    msg_num = _to_int(data[1]) if len(data) > 1 else None
    declared = _to_int(data[2]) if len(data) > 2 else None

    rest = data[3:]
    block_size = _gsv_block_size(rest, signal_ids_known)

    observations = []
    usable = len(rest) - (len(rest) % block_size)
    for offset in range(0, usable, block_size):
        block = rest[offset:offset + block_size]
        number = _to_int(block[0])
        if number is None:
            continue
        signal_id = _to_int(block[4]) if block_size == 5 else None
        constellation = resolve_constellation(msg.talker, number)
        if constellation is None:
            # Mixed GN sentence with a local number: the signal ID is the only
            # remaining way to prove the constellation.
            constellation = signal_constellation(signal_id)
        observations.append(SignalObservation(
            constellation=constellation,
            number=number,
            signal_id=signal_id,
            elevation=_to_int(block[1]) if len(block) > 1 else None,
            azimuth=_to_int(block[2]) if len(block) > 2 else None,
            snr=_to_int(block[3]) if len(block) > 3 else None,
            talker=msg.talker,
        ))

    header = {
        'num_messages': num_messages,
        'msg_num': msg_num,
        'declared_in_view': declared,
        'talker': msg.talker,
    }
    return header, block_size, observations


def _parse_gsa(msg):
    """Turn a GSA sentence into a plain dict of its contents."""
    data = msg.data
    mode = data[0] if len(data) > 0 else ''
    fix_type = _to_int(data[1]) if len(data) > 1 else None

    satellites = []
    for raw_id in data[2:14]:
        number = _to_int(raw_id)
        if number is None:
            continue
        constellation = resolve_constellation(msg.talker, number)
        satellites.append(Satellite(constellation, number))

    def dop(index):
        return _to_float(data[index]) if len(data) > index else None

    return {
        'mode': mode,
        'fix_type': fix_type,
        'satellites': satellites,
        'pdop': dop(14),
        'hdop': dop(15),
        'vdop': dop(16),
        'talker': msg.talker,
    }


class SatelliteReconciler(object):
    """
    Accumulate GSA/GSV sentences and reconcile used vs. seen satellites.

    Feed parsed sentences with :meth:`feed` (or a whole iterable with
    :meth:`consume`, raw text with :meth:`from_lines`), then inspect the rounds,
    folded satellite sets and the legacy ambiguity range.
    """

    def __init__(self):
        self.gsv_rounds = []
        self.gsa_rounds = []
        self._pending_gsv = {}
        self._gsa_round_no = 0
        self._gsv_round_no = 0

    def consume(self, sentences):
        for sentence in sentences:
            self.feed(sentence)
        return self

    @classmethod
    def from_lines(cls, lines, errors='ignore'):
        """Build a reconciler from raw NMEA text lines."""
        reconciler = cls()
        for line in lines:
            try:
                reconciler.feed(NMEASentence.parse(line))
            except ParseError:
                if errors == 'raise':
                    raise
        return reconciler

    def feed(self, msg):
        sentence_type = getattr(msg, 'sentence_type', None)
        if sentence_type == 'GSV':
            self._feed_gsv(msg)
        elif sentence_type == 'GSA':
            self._feed_gsa(msg)
        return self

    def _feed_gsa(self, msg):
        parsed = _parse_gsa(msg)
        self._gsa_round_no += 1
        constellation = TALKER_CONSTELLATIONS.get(msg.talker)
        self.gsa_rounds.append(GSARound(
            round_no=self._gsa_round_no,
            constellation=constellation,
            mode=parsed['mode'],
            fix_type=parsed['fix_type'],
            satellites=parsed['satellites'],
            pdop=parsed['pdop'],
            hdop=parsed['hdop'],
            vdop=parsed['vdop'],
        ))

    def _feed_gsv(self, msg):
        constellation = TALKER_CONSTELLATIONS.get(msg.talker)
        key = constellation if constellation is not None else msg.talker

        pending = self._pending_gsv.get(key)
        signal_ids_known = bool(pending and pending.get('signal_ids'))
        header, block_size, observations = _parse_gsv(msg, signal_ids_known)

        num_messages = header['num_messages']
        msg_num = header['msg_num']

        if pending is None or msg_num == 1:
            if pending is not None:
                self._finalize_gsv(key, complete=False)
            self._gsv_round_no += 1
            pending = {
                'round_no': self._gsv_round_no,
                'constellation': constellation,
                'declared_in_view': header['declared_in_view'],
                'observations': [],
                'signal_ids': block_size == 5,
            }
            self._pending_gsv[key] = pending

        pending['observations'].extend(observations)
        if block_size == 5:
            pending['signal_ids'] = True

        if num_messages is not None and msg_num is not None and \
                msg_num >= num_messages:
            self._finalize_gsv(key, complete=True)

    def _finalize_gsv(self, key, complete):
        pending = self._pending_gsv.pop(key)
        self.gsv_rounds.append(GSVRound(
            round_no=pending['round_no'],
            constellation=pending['constellation'],
            declared_in_view=pending['declared_in_view'],
            complete=complete,
            observations=pending['observations'],
        ))

    def flush(self):
        """Finalize GSV cycles that were never closed by a last message."""
        for key in list(self._pending_gsv.keys()):
            self._finalize_gsv(key, complete=False)
        return self

    # ------------------------------------------------------------------
    # Folding and summaries
    # ------------------------------------------------------------------

    @staticmethod
    def fold_observations(observations):
        """
        Fold signal-level observations into physical satellites.

        Returns an ``OrderedDict`` of :class:`Satellite` -> sorted list of
        signal IDs.  Entries sharing constellation and number (the dual-band
        case) collapse into one key; different constellations never do.
        """
        folded = OrderedDict()
        for observation in observations:
            satellite = Satellite(observation.constellation,
                                   observation.number)
            signals = folded.setdefault(satellite, set())
            if observation.signal_id is not None:
                signals.add(observation.signal_id)
        return OrderedDict(
            (satellite, sorted(signals))
            for satellite, signals in folded.items()
        )

    @property
    def all_observations(self):
        observations = []
        for round_ in self.gsv_rounds:
            observations.extend(round_.observations)
        return observations

    @property
    def valid_gsv_rounds(self):
        """GSV rounds containing at least one observation with an SNR value."""
        return [round_ for round_ in self.gsv_rounds
                if any(obs.snr is not None for obs in round_.observations)]

    def seen_satellites(self):
        """Distinct physical satellites visible in any GSV round."""
        return set(self.fold_observations(self.all_observations).keys())

    def used_satellites(self):
        """Distinct physical satellites listed in any GSA sentence."""
        satellites = set()
        for round_ in self.gsa_rounds:
            satellites.update(round_.satellites)
        return satellites

    def used_not_seen(self):
        """GSA satellites with no matching GSV observation."""
        return self.used_satellites() - self.seen_satellites()

    def seen_not_used(self):
        """GSV satellites that never participate in a fix."""
        return self.seen_satellites() - self.used_satellites()

    def signal_summary(self):
        """Physical satellite -> sorted signal IDs observed across all rounds."""
        return self.fold_observations(self.all_observations)

    def dual_band_satellites(self):
        """Satellites observed on more than one signal/frequency."""
        return OrderedDict(
            (satellite, signals)
            for satellite, signals in self.signal_summary().items()
            if len(signals) > 1
        )

    def gsa_slot_count(self):
        """Total GSA satellite slots, counting repeats (one slot per ID)."""
        return sum(len(round_.satellites) for round_ in self.gsa_rounds)

    def gsv_signal_entry_count(self):
        """Total GSV blocks, i.e. signal-level rows before folding."""
        return len(self.all_observations)

    def declared_mismatches(self):
        """Complete GSV rounds whose declared in-view count differs from data."""
        mismatches = []
        for round_ in self.gsv_rounds:
            if not round_.complete:
                continue
            distinct = len(self.fold_observations(round_.observations))
            if round_.declared_in_view is not None and \
                    round_.declared_in_view != distinct:
                mismatches.append((round_, distinct))
        return mismatches

    # ------------------------------------------------------------------
    # Legacy ambiguity
    # ------------------------------------------------------------------

    def _all_satellite_refs(self):
        """
        Collect evidence from GSA and GSV.

        ``identified`` maps a number to the constellation(s) in which it was
        observed; ``unidentified`` maps a number to the number of unidentified
        rows carrying it and records whether GSA participates with it.
        """
        identified = {}
        unidentified = {}

        def record(satellite, used):
            if satellite.constellation is None:
                entry = unidentified.setdefault(satellite.number, {
                    'rows': 0, 'used_rows': 0})
                entry['rows'] += 1
                if used:
                    entry['used_rows'] += 1
            else:
                identified.setdefault(satellite.number, set()).add(
                    satellite.constellation)

        for observation in self.all_observations:
            record(Satellite(observation.constellation, observation.number),
                   used=False)
        for round_ in self.gsa_rounds:
            for satellite in round_.satellites:
                record(satellite, used=True)
        return identified, unidentified

    @staticmethod
    def _candidate_constellations(number, identified):
        """
        Constellations in which this number is a legal local satellite ID.

        Identified evidence for the same number is *not* used to narrow the
        candidates: seeing GPS PRN 05 does not rule out a separate GLONASS
        slot 05.  It only marks which candidate constellations are already
        occupied, which is handled by the caller.
        """
        return sorted(
            constellation
            for constellation, (low, high)
            in CONSTELLATION_NUMBER_RANGES.items()
            if low <= number <= high
        )

    def legacy_ambiguity(self):
        """
        Analyse observations whose constellation cannot be proven.

        For every unidentified satellite number the legal candidate
        constellations are derived from the local satellite ID ranges.  Let
        ``r`` be the number of unidentified rows carrying the number, ``occ``
        the constellations in which an *identified* satellite with that number
        already exists and ``free`` the candidate constellations without such
        an identified satellite.

        The unidentified rows may fold onto an already identified satellite or
        hide additional ones, so the number of *new* (unidentified) physical
        satellites behind this number is provably in:

        * minimum 0 when an identified satellite with that number exists
          (every row may fold onto it), otherwise 1 (the observations must come
          from at least one satellite);
        * maximum ``min(r, len(free))``: one constellation cannot host two
          satellites sharing a number and occupied constellations are already
          counted elsewhere.

        Different numbers never constrain each other, so the per-number ranges
        add up.  The returned ``min``/``max`` count only unidentified satellites
        and are meant to be added to the identified satellite count.
        """
        identified, unidentified = self._all_satellite_refs()
        known_constellations = set(TALKER_CONSTELLATIONS.values())

        new_minimum = 0
        new_maximum = 0
        ambiguous = []
        for number in sorted(unidentified):
            info = unidentified[number]
            rows = info['rows']
            candidates = self._candidate_constellations(number, identified)
            occupied = sorted(identified.get(number, set()))
            free = [c for c in candidates if c not in occupied]

            add_min = 0 if occupied else 1
            add_max = min(rows, len(free)) if candidates else rows

            new_minimum += add_min
            new_maximum += add_max
            ambiguous.append({
                'number': number,
                'candidates': candidates,
                'occupied_constellations': occupied,
                'free_constellations': free,
                'unresolved': not occupied,
                'rows': rows,
                'used_rows': info['used_rows'],
                'new_range': (add_min, add_max),
                'range': (len(occupied) + add_min,
                          len(occupied) + add_max),
            })

        return {
            'min': new_minimum,
            'max': new_maximum,
            'unidentified_rows': sum(
                info['rows'] for info in unidentified.values()),
            'ambiguous': ambiguous,
            'known_constellations': sorted(known_constellations),
        }

    def reconcile(self):
        """Full reconciliation result as plain data structures."""
        seen = self.seen_satellites()
        used = self.used_satellites()
        identified_refs, _ = self._all_satellite_refs()
        legacy = self.legacy_ambiguity()

        all_refs = seen | used
        identified_count = sum(
            1 for satellite in all_refs if satellite.constellation is not None)

        sort = lambda satellites: sorted(satellites, key=_satellite_sort_key)

        return {
            'gsa_rounds': len(self.gsa_rounds),
            'gsv_rounds': len(self.gsv_rounds),
            'valid_gsv_rounds': len(self.valid_gsv_rounds),
            'gsa_slot_count': self.gsa_slot_count(),
            'gsv_signal_entry_count': self.gsv_signal_entry_count(),
            'distinct_seen': sort(seen),
            'distinct_used': sort(used),
            'used_not_seen': sort(used - seen),
            'seen_not_used': sort(seen - used),
            'dual_band_satellites': list(self.dual_band_satellites().items()),
            'signals': {sat: signals
                        for sat, signals in self.signal_summary().items()},
            'identified_satellite_count': identified_count,
            'identified_refs_by_number': identified_refs,
            'legacy': legacy,
            'provable_total_range': (
                identified_count + legacy['min'],
                identified_count + legacy['max'],
            ),
            'declared_mismatches': [
                (round_.round_no, round_.declared_in_view, distinct)
                for round_, distinct in self.declared_mismatches()
            ],
        }

    def report(self):
        """Human-readable reconciliation report."""
        result = self.reconcile()
        lines = []
        lines.append('GSA rounds: {gsa_rounds}, GSV rounds: {gsv_rounds} '
                     '({valid_gsv_rounds} with valid SNR)'.format(**result))
        lines.append('GSA satellite slots: {} (one ID per slot, repeats '
                     'counted)'.format(result['gsa_slot_count']))
        lines.append('GSV signal-level entries: {} (before dual-band '
                     'folding)'.format(result['gsv_signal_entry_count']))
        lines.append('Distinct physical satellites seen in GSV: {}'
                     .format(len(result['distinct_seen'])))
        lines.append('Distinct physical satellites used in GSA: {}'
                     .format(len(result['distinct_used'])))

        for round_no, declared, actual in result['declared_mismatches']:
            lines.append('GSV round {} declares {} satellites in view but '
                         'lists {}'.format(round_no, declared, actual))

        if result['dual_band_satellites']:
            lines.append('Dual-band satellites folded back to one:')
            for satellite, signals in result['dual_band_satellites']:
                names = ', '.join(
                    signal_name(sid) or 'signal #{}'.format(sid)
                    for sid in signals)
                lines.append('  {} {}: {}'.format(
                    satellite.constellation or 'unknown',
                    satellite.number, names))

        if result['used_not_seen']:
            lines.append('Used in GSA but absent from GSV: {}'.format(
                ', '.join(map(_format_satellite, result['used_not_seen']))))
        if result['seen_not_used']:
            lines.append('Seen in GSV but not used in GSA: {}'.format(
                ', '.join(map(_format_satellite, result['seen_not_used']))))

        legacy = result['legacy']
        if legacy['ambiguous']:
            low, high = result['provable_total_range']
            lines.append('Legacy/unidentified observations: provable total '
                         'range {}-{} physical satellites'.format(low, high))
            for entry in legacy['ambiguous']:
                add_min, add_max = entry['new_range']
                total_min, total_max = entry['range']
                lines.append(
                    '  number {:02d}: {}-{} physical satellite(s) '
                    '({}-{} unidentified); could be {}'.format(
                        entry['number'], total_min, total_max,
                        add_min, add_max,
                        ', '.join(entry['candidates']) or 'no legal system'))
        else:
            lines.append('All observations carry a provable constellation.')
        return '\n'.join(lines)


def _format_satellite(satellite):
    return '{} {:02d}'.format(satellite.constellation or 'unknown',
                              satellite.number)
