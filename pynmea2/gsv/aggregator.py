"""
GSV cycle assembly.

A GSV ("Satellites in view") sentence only carries part of the visible
satellite list: a receiver splits one observation round (cycle) into
``num_messages`` numbered pages.  On a real bus several receivers can be
multiplexed onto one stream, pages from different rounds or receivers can
arrive interleaved, and a page can be re-sent with updated values before the
round is over.

This module reassembles complete per-round lists while keeping the evidence
intact:

* pages are grouped by the physical input (``source``), the constellation
  (talker id, e.g. ``GP``/``GL``/``GA``/``GB``) and the signal type carried
  by the talker id (e.g. QZSS ``QZ`` vs. GPS ``GP``);
* page 1 marks the start of a round: seeing it again closes the previous
  round and opens a new one, so pages are never borrowed from a previous
  round (a page arriving before the first page 1 of its round is kept in a
  provisional round that is closed as soon as that page 1 appears);
* a change of the advertised page count likewise closes the current round
  as incomplete before a new one is opened;
* inside a round, a changed re-sent page replaces the earlier copy (the
  replacement is recorded as a conflict); an identical re-sent page is
  ignored;
* a round is only reported as complete when every advertised page has been
  received from the same round, without conflicts, and the number of
  satellites matches ``num_sv_in_view``.  Every other report keeps the exact
  list of missing page numbers, conflicts and a human-readable reason.
"""

from collections import namedtuple


# Talker ids that can appear in front of a GSV sentence.  The talker id
# identifies both the constellation and, for the multi-constellation ids, the
# signal.  Sources: NMEA 0183 v4.10 talker identifier table.
TALKER_SIGNALS = {
    'GP': ('GPS', 'L1 C/A'),
    'GL': ('GLONASS', 'L1 OF'),
    'GA': ('Galileo', 'E1-B/C'),
    'GB': ('BeiDou', 'B1I'),
    'QZ': ('QZSS', 'L1 C/A'),
    'GN': ('GNSS', 'mixed'),
    'BD': ('BeiDou', 'B1I'),
}


Satellite = namedtuple(
    'Satellite',
    ('prn', 'elevation', 'azimuth', 'snr', 'page', 'slot'))


class GSVReport(object):
    """
    The visible-satellite list reconstructed for one GSV cycle (one round of
    one receiver/constellation/signal combination).

    ``complete`` is only ``True`` when every page advertised by the receiver
    was observed, in the same round, without conflicting retransmissions and
    with a satellite count matching ``num_sv_in_view``.  Whenever it is
    ``False``, ``missing_pages`` / ``conflicts`` / ``reason`` explain exactly
    what evidence is missing, so an operator can tell which page was lost.
    """

    def __init__(self, source, talker, constellation, signal, total_pages,
                 num_sv_in_view, satellites, received_pages, conflicts,
                 complete, reason):
        self.source = source
        self.talker = talker
        self.constellation = constellation
        self.signal = signal
        self.total_pages = total_pages
        self.num_sv_in_view = num_sv_in_view
        self.satellites = tuple(satellites)
        self.received_pages = tuple(received_pages)
        self.missing_pages = tuple(
            page for page in range(1, (total_pages or 0) + 1)
            if page not in set(received_pages))
        self.conflicts = tuple(conflicts)
        self.complete = complete
        self.reason = reason

    @property
    def key(self):
        return (self.source, self.talker, self.signal)

    def __repr__(self):
        return ('<GSVReport(source={!r}, talker={!r}, signal={!r}, '
                'complete={!r}, pages={}/{}, satellites={})>').format(
                    self.source, self.talker, self.signal, self.complete,
                    len(self.received_pages), self.total_pages,
                    len(self.satellites))


class _Cycle(object):
    """In-progress assembly of one GSV round for one grouping key."""

    def __init__(self, key, talker, total_pages, num_sv_in_view):
        self.key = key
        self.talker = talker
        self.total_pages = total_pages
        self.num_sv_in_view = num_sv_in_view
        # page number -> list of Satellite slots (up to 4)
        self.pages = {}
        # page number -> raw signature, used to spot changed retransmissions
        self._signatures = {}
        self.conflicts = []

    def signature(self, msg):
        signature = []
        for slot in range(4):
            base = 3 + slot * 4
            signature.append(tuple(
                msg.data[i] if i < len(msg.data) else ''
                for i in range(base, base + 4)))
        return tuple(signature)

    def add_page(self, msg, page_num):
        satellites = []
        for slot in range(4):
            base = 3 + slot * 4
            prn = msg.data[base] if base < len(msg.data) else ''
            if not prn:
                continue
            satellites.append(Satellite(
                prn=prn,
                elevation=_to_int(msg.data, base + 1),
                azimuth=_to_int(msg.data, base + 2),
                snr=_to_int(msg.data, base + 3),
                page=page_num,
                slot=slot + 1,
            ))

        signature = self.signature(msg)
        if page_num in self.pages:
            if self._signatures[page_num] != signature:
                self.conflicts.append(
                    'page {0} re-sent with different satellite data; '
                    'the earlier copy was discarded'.format(page_num))
            else:
                # identical retransmission: keep the first copy, no conflict
                return
        self.pages[page_num] = satellites
        self._signatures[page_num] = signature
        # num_sv_in_view is repeated on every page; trust the newest value
        self.num_sv_in_view = _to_int(msg.data, 2)

    def all_pages_present(self):
        if not self.total_pages:
            return False
        return all(page in self.pages
                   for page in range(1, self.total_pages + 1))

    def build_report(self, complete, reason):
        constellation, signal = TALKER_SIGNALS.get(
            self.talker, ('unknown', 'unknown'))
        satellites = []
        for page_num in sorted(self.pages):
            satellites.extend(self.pages[page_num])
        return GSVReport(
            source=self.key[0],
            talker=self.talker,
            constellation=constellation,
            signal=signal,
            total_pages=self.total_pages,
            num_sv_in_view=self.num_sv_in_view,
            satellites=satellites,
            received_pages=sorted(self.pages),
            conflicts=self.conflicts,
            complete=complete,
            reason=reason,
        )


class GSVAggregator(object):
    """
    Stateful reassembler for multiplexed GSV streams.

    Feed every parsed NMEA sentence (including non-GSV ones, which are
    ignored) together with an identifier of the physical input it came from:

        >>> agg = GSVAggregator()
        >>> for line in bus:
        ...     msg = pynmea2.parse(line)
        ...     for report in agg.feed(msg, source='receiver-a'):
        ...         print(report)

    Every call returns the list of rounds finalized by this sentence (usually
    empty or one element).  A round that is superseded before completion is
    finalized with ``complete=False`` and an explicit ``missing_pages`` list,
    so incomplete rounds are never silently mixed with complete ones.  Every
    finalized report is also kept, in order, in :attr:`reports`.
    """

    def __init__(self):
        # key -> _Cycle currently being assembled
        self._cycles = {}
        # all finalized reports, complete and incomplete, in finalization order
        self.reports = []

    def feed(self, msg, source=None):
        """
        Process one parsed NMEA sentence.

        Returns a list of the :class:`GSVReport` objects finalized by this
        sentence (possibly empty).  It can contain two reports when the
        sentence starts a new round while closing an unfinished previous one.
        All returned reports are also appended to :attr:`reports`.
        """
        if getattr(msg, 'sentence_type', None) != 'GSV':
            return []

        finalized = []
        talker = msg.talker
        signal = TALKER_SIGNALS.get(talker, ('unknown', 'unknown'))[1]
        key = (source, talker, signal)

        try:
            total_pages = int(msg.num_messages)
            page_num = int(msg.msg_num)
        except (TypeError, ValueError):
            raise ValueError(
                'GSV sentence has invalid page numbers: {!r}'.format(msg))
        if total_pages < 1 or not (1 <= page_num <= total_pages):
            raise ValueError(
                'GSV sentence has out-of-range page numbers: {!r}'.format(msg))

        cycle = self._cycles.get(key)

        if cycle is not None and cycle.total_pages != total_pages:
            # The receiver changed the round size mid-stream: the old round
            # can never be completed, close it as incomplete first.
            finalized.append(self._abandon(
                cycle, 'advertised page count changed from {0} to '
                       '{1}'.format(cycle.total_pages, total_pages)))
            cycle = None

        if cycle is None:
            cycle = _Cycle(key, talker, total_pages,
                           _to_int(msg.data, 2))
            self._cycles[key] = cycle
        elif page_num == 1:
            # Page 1 again means the previous round is over.  Flush whatever
            # was collected (with its missing pages) and start a fresh round;
            # pages from the old round are never reused.
            finalized.append(self._abandon(
                cycle, 'next round started before all pages were received'))
            cycle = _Cycle(key, talker, total_pages,
                           _to_int(msg.data, 2))
            self._cycles[key] = cycle

        cycle.add_page(msg, page_num)

        if cycle.all_pages_present():
            finalized.append(self._complete(cycle))
        return finalized

    def flush(self):
        """
        Finalize every round still in progress (e.g. at end of input).

        Returns the list of finalized :class:`GSVReport` objects; rounds that
        were still missing pages are reported with ``complete=False``.
        """
        flushed = []
        for key in list(self._cycles):
            cycle = self._cycles.pop(key)
            if not cycle.pages:
                continue
            flushed.append(self._abandon(
                cycle, 'input ended before all pages were received'))
        return flushed

    def _complete(self, cycle):
        del self._cycles[cycle.key]
        reason = None
        complete = True
        if cycle.conflicts:
            complete = False
            reason = '; '.join(cycle.conflicts)
        elif (cycle.num_sv_in_view is not None
              and cycle.num_sv_in_view != self._satellite_count(cycle)):
            complete = False
            reason = ('num_sv_in_view ({0}) does not match the number of '
                      'satellites on the received pages ({1})').format(
                          cycle.num_sv_in_view,
                          self._satellite_count(cycle))
        report = cycle.build_report(complete, reason)
        self.reports.append(report)
        return report

    def _abandon(self, cycle, reason):
        self._cycles.pop(cycle.key, None)
        report = cycle.build_report(False, reason)
        self.reports.append(report)
        return report

    @staticmethod
    def _satellite_count(cycle):
        return sum(len(page) for page in cycle.pages.values())


def _to_int(data, index):
    if index >= len(data) or data[index] == '':
        return None
    try:
        return int(data[index])
    except ValueError:
        return None
