from __future__ import unicode_literals

from collections import namedtuple

from . import nmea

__all__ = [
    'NMEAStreamReader',
    'NMEAChannelReader',
    'StreamRecord',
    'StreamRange',
    'PendingTail',
    'Coverage',
    'OK',
    'BAD',
    'PENDING',
    'DISCARD',
]

ERRORS = ('raise', 'yield', 'ignore')

OK = 'ok'
BAD = 'bad'
PENDING = 'pending'
DISCARD = 'discard'

_UNSET = object()

try:  # pragma: no cover - python 2 compatibility
    text_type = unicode
except NameError:  # pragma: no cover
    text_type = str


class _Bytes(bytes):
    '''Bytes that also compare equal to the equivalent latin-1 text.

    The reader accepts either bytes or text chunks.  Exposing records as
    bytes keeps offsets authoritative, while this looseness keeps the usual
    ``record.raw == line`` assertion working for text input.
    '''
    def __new__(cls, value=b''):
        return bytes.__new__(cls, _as_bytes(value))

    def __eq__(self, other):
        if isinstance(other, text_type):
            return _as_text(self) == other
        return bytes.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = bytes.__hash__

    def __str__(self):
        return _as_text(self)


def _as_bytes(data):
    '''Return *data* as bytes without changing its length.

    NMEA is an ASCII protocol, so latin-1 provides a one-byte-per-character
    mapping when callers hand us text.  Byte offsets then match exactly what
    was read from the serial port.
    '''
    if data is None:
        return b''
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, text_type):
        return data.encode('latin-1')
    if isinstance(data, memoryview):  # pragma: no cover - py2 has no tobytes
        try:
            return data.tobytes()
        except AttributeError:
            return bytes(data)
    return bytes(data)


def _as_text(data):
    return _as_bytes(data).decode('latin-1')


class PendingTail(_Bytes):
    '''The not-yet-terminated bytes buffered for one channel.'''

    def __new__(cls, value=b'', channel=None, start=0, end=None,
                generation=0, reason='incomplete'):
        obj = _Bytes.__new__(cls, value)
        obj.channel = channel
        obj.start = start
        obj.end = start + len(obj) if end is None else end
        obj.generation = generation
        obj.reason = reason
        obj.status = PENDING
        return obj

    @property
    def raw(self):
        return _Bytes(self)

    @property
    def byte_range(self):
        return (self.start, self.end)

    @property
    def length(self):
        return len(self)

    @property
    def is_empty(self):
        return len(self) == 0

    def __repr__(self):
        return '<PendingTail channel=%r range=(%d, %d)>' % (
            self.channel, self.start, self.end)


class StreamRecord(object):
    '''One classified piece of a channel's byte stream.

    ``status`` is one of:

    ``ok``
        A complete sentence that passed the channel's checksum policy.
        ``sentence`` holds the parsed message.
    ``bad``
        A complete line that could not be parsed or failed the checksum
        policy.  ``error`` explains why; its bytes count as discarded.
    ``pending``
        Only produced by ``feed(..., include_pending=True)``; the bytes are
        still buffered and a later chunk may complete them.
    ``discard``
        Buffered bytes that were dropped on reconnect or close.

    ``start`` and ``end`` are absolute byte offsets within that channel, so
    records from interleaved ports never disturb one another's positions.
    ``generation`` identifies the connection on which the bytes arrived.
    '''

    def __init__(self, channel, status, raw=b'', start=0, end=None,
                 sentence=None, error=None, generation=0, reason=None,
                 checksum_ok=None):
        self.channel = channel
        self.status = status
        self.raw = _Bytes(raw)
        self.start = start
        self.end = start + len(self.raw) if end is None else end
        self.sentence = sentence
        self.error = error
        self.generation = generation
        self.reason = reason
        self.checksum_ok = checksum_ok

    @property
    def port(self):
        return self.channel

    @property
    def byte_range(self):
        return (self.start, self.end)

    @property
    def range(self):
        return (self.start, self.end)

    @property
    def length(self):
        return self.end - self.start

    @property
    def text(self):
        return _as_text(self.raw)

    @property
    def line(self):
        return _Bytes(bytes(self.raw).rstrip(b'\r\n'))

    @property
    def message(self):
        return self.sentence

    @property
    def msg(self):
        return self.sentence

    @property
    def is_success(self):
        return self.status == OK

    @property
    def is_bad(self):
        return self.status == BAD

    @property
    def is_pending(self):
        return self.status == PENDING

    @property
    def is_discard(self):
        return self.status == DISCARD

    @property
    def ok(self):
        return self.status == OK

    @property
    def valid(self):
        return self.status == OK

    @property
    def discarded(self):
        return self.status in (BAD, DISCARD)

    @property
    def complete(self):
        return self.status in (OK, BAD, DISCARD)

    def __getattr__(self, name):
        # A successful record also answers the parsed sentence's fields.
        if name.startswith('__'):
            raise AttributeError(name)
        sentence = self.__dict__.get('sentence')
        if sentence is not None:
            return getattr(sentence, name)
        raise AttributeError(name)

    def __bool__(self):
        return self.status == OK

    __nonzero__ = __bool__

    def __str__(self):
        if self.sentence is not None:
            return str(self.sentence)
        return _as_text(self.raw)

    def __repr__(self):
        return '<StreamRecord channel=%r status=%r range=(%d, %d) reason=%r>' % (
            self.channel, self.status, self.start, self.end, self.reason)

    def as_dict(self):
        return {
            'channel': self.channel,
            'status': self.status,
            'start': self.start,
            'end': self.end,
            'raw': self.raw,
            'sentence': self.sentence,
            'error': self.error,
            'generation': self.generation,
            'reason': self.reason,
            'checksum_ok': self.checksum_ok,
        }


class StreamRange(namedtuple('_StreamRange', 'start end status channel record')):
    '''A byte range covered by one record.'''

    __slots__ = ()

    @property
    def length(self):
        return self.end - self.start

    @property
    def byte_range(self):
        return (self.start, self.end)

    @property
    def discarded(self):
        return self.status in (BAD, DISCARD)


class Coverage(dict):
    '''Coverage facts for a channel, readable as keys or attributes.'''

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class StreamBatch(list):
    '''Records produced by one feed/close call.

    This is a plain list of :class:`StreamRecord` objects, so existing
    iteration and comparison keep working.  ``pending`` carries the tail
    that is still buffered for the channel.
    '''

    def __init__(self, records=(), pending=None, channel=None):
        list.__init__(self, records)
        self.pending = pending
        self.tail = pending
        self.channel = channel

    @property
    def sentences(self):
        return [record.sentence for record in self if record.status == OK]

    @property
    def successes(self):
        return [record for record in self if record.status == OK]

    @property
    def failures(self):
        return [record for record in self if record.status == BAD]

    @property
    def discards(self):
        return [record for record in self if record.status == DISCARD]

    def __repr__(self):
        return '<StreamBatch channel=%r records=%d pending=%d>' % (
            self.channel, len(self),
            len(self.pending) if self.pending is not None else 0)


class _ChannelState(object):
    def __init__(self, channel, checksums, errors):
        self.channel = channel
        self.checksums = checksums
        self.errors = errors
        self.buffer = b''
        self.received = 0
        self.generation = 0
        self.closed = False
        self.records = []
        self.success_bytes = 0
        self.discard_bytes = 0


class NMEAStreamReader(object):
    '''
    Reads NMEA sentences from a stream.

    Without channels this is the original single-stream reader: ``next()``
    yields parsed sentences and, depending on ``errors``, ``ParseError``
    objects.

    With ``channels`` registered it is a multiplexed reader for several
    serial ports.  Each channel keeps its own byte buffer and absolute byte
    cursor; chunks may split a sentence anywhere.  Feed a chunk with
    ``feed(channel, data)`` and classify every returned :class:`StreamRecord`
    by its ``status``: ``ok`` for a complete valid sentence, ``bad`` for a
    complete line rejected by the channel's checksum policy, and ``discard``
    for bytes dropped on reconnect or close.

    `stream`
        file-like object for the legacy interface, or a mapping of
        ``channel -> stream`` for multiplexed input.
    `errors`
        legacy parse-error behavior: ``'raise'`` (default for the legacy
        interface), ``'yield'`` or ``'ignore'``.  In channel mode a bad line
        is reported as a ``bad`` record rather than aborting other ports;
        pass ``'raise'`` explicitly to make it raise instead.
    `channels`
        channel names to register; an integer registers ``0 .. n-1``.
        Unknown channels are registered on first use.
    `checksums`
        checksum policy (see ``NMEASentence.parse``); ``channel_checksums``
        can override it per channel.  ``channel_errors`` likewise overrides
        the parse-error behavior per channel.
    '''

    def __init__(self, stream=None, errors=_UNSET, channels=None,
                 checksums='check', channel_checksums=None,
                 channel_errors=None, ports=None, num_channels=None):
        if isinstance(stream, int) and not isinstance(stream, bool) \
                and channels is None:
            channels = stream
            stream = None
        if channels is None:
            channels = ports
        if channels is None:
            channels = num_channels

        self.errors = 'raise' if errors is _UNSET else errors
        self._channel_error_default = 'yield' if errors is _UNSET else errors
        if errors is not _UNSET and not isinstance(errors, dict) \
                and errors not in ERRORS:
            raise ValueError('errors must be one of {!r} (was: {!r})'
                             .format(ERRORS, errors))

        self.stream = None
        self.streams = {}
        self.buffer = ''
        self.records = []
        self._checksums = checksums
        self._channel_checksums = dict(channel_checksums or {})
        self._channel_errors = dict(channel_errors or {})
        self._states = {}
        self._channel_order = []

        if hasattr(stream, 'keys') and not hasattr(stream, 'readline'):
            self.streams = dict(stream)
            if channels is None:
                channels = list(self.streams)
        else:
            self.stream = stream

        if channels is not None:
            if isinstance(channels, int) and not isinstance(channels, bool):
                channels = list(range(channels))
            elif isinstance(channels, (text_type, bytes)):
                channels = [channels]
            for channel in channels:
                self.register_channel(channel)
            if errors is _UNSET:
                # Channel mode is multiplexed: one bad port must not abort
                # the records already produced by the other ports.
                self.errors = 'yield'

    # -- channel management ------------------------------------------------

    @property
    def channels(self):
        return list(self._channel_order)

    def register_channel(self, channel, checksums=None, errors=None):
        '''Register a channel (idempotent) and return its state.'''
        state = self._states.get(channel)
        if state is None:
            state = _ChannelState(
                channel,
                self._channel_checksums.get(channel, self._checksums),
                self._channel_errors.get(
                    channel, self._channel_error_default))
            self._states[channel] = state
            self._channel_order.append(channel)
        if checksums is not None:
            self._channel_checksums[channel] = checksums
            state.checksums = checksums
        if errors is not None:
            if errors not in ERRORS:
                raise ValueError('errors must be one of {!r} (was: {!r})'
                                 .format(ERRORS, errors))
            self._channel_errors[channel] = errors
            state.errors = errors
        return state

    add_channel = register_channel

    def _get_state(self, channel=None):
        if channel is None:
            if len(self._channel_order) == 1:
                channel = self._channel_order[0]
            elif self._channel_order:
                channel = self._channel_order[0]
            elif self.streams:
                channel = next(iter(self.streams))
            else:
                channel = 0
        return self.register_channel(channel)

    # -- reading -----------------------------------------------------------

    def _checksum_status(self, text):
        match = nmea.NMEASentence.sentence_re.match(text)
        if not match or not match.group('checksum'):
            return None
        try:
            expected = int(match.group('checksum'), 16)
            actual = nmea.NMEASentence.checksum(match.group('nmea_str'))
            return expected == actual
        except (TypeError, ValueError):  # pragma: no cover - regex constrained
            return None

    def _parse_line(self, state, raw, start, end, checksums):
        parse_text = _as_text(bytes(raw).rstrip(b'\r\n'))
        checksum_ok = self._checksum_status(parse_text)
        try:
            sentence = nmea.NMEASentence.parse(
                parse_text, checksums=checksums)
        except nmea.ChecksumError as exc:
            return StreamRecord(
                state.channel, BAD, raw, start, end, error=exc,
                generation=state.generation, reason='checksum',
                checksum_ok=False)
        except nmea.ParseError as exc:
            return StreamRecord(
                state.channel, BAD, raw, start, end, error=exc,
                generation=state.generation, reason='parse',
                checksum_ok=checksum_ok)
        return StreamRecord(
            state.channel, OK, raw, start, end, sentence=sentence,
            generation=state.generation, reason='sentence',
            checksum_ok=checksum_ok)

    def feed(self, *args, **kwargs):
        '''Feed one chunk into a channel.

        Accepted forms::

            feed(channel, data)
            feed(data, channel=channel)
            feed(channel=channel, data=data)

        Passing a mapping of ``channel -> chunk`` feeds every entry and
        returns their combined records.  ``include_pending=True`` appends a
        ``pending`` record describing the tail that is still buffered.
        '''
        include_pending = kwargs.pop('include_pending', False)
        channel = kwargs.pop('channel', None)
        checksums = kwargs.pop('checksums', None)
        errors = kwargs.pop('errors', None)
        data = kwargs.pop('data', None)
        if kwargs:
            raise TypeError('unexpected keyword argument: %r'
                            % next(iter(kwargs)))

        if len(args) == 2:
            channel, data = args
        elif len(args) == 1:
            arg = args[0]
            if isinstance(arg, dict) and channel is None and data is None:
                data = arg
            elif channel is None and data is None:
                if self._is_channel(arg):
                    channel = arg
                elif isinstance(arg, (bytes, bytearray, text_type)):
                    # ``feed(chunk)`` means the default channel.
                    data = arg
                else:
                    channel = arg
            elif channel is None:
                if isinstance(arg, (bytes, bytearray, text_type)):
                    data = arg
                else:
                    channel = arg
            elif data is None:
                data = arg
            else:
                raise TypeError('feed() got multiple data arguments')
        elif len(args) > 2:
            raise TypeError('feed() takes at most 2 positional arguments')

        if isinstance(data, dict) and channel is None:
            records = StreamBatch()
            for ch, chunk in data.items():
                records.extend(self.feed(
                    ch, chunk, include_pending=include_pending,
                    checksums=checksums, errors=errors))
            records.pending = self.pending_all()
            records.tail = records.pending
            return records

        state = self._get_state(channel)

        if data is None:
            stream = self.streams.get(state.channel, self.stream)
            data = stream.read() if stream is not None and hasattr(stream, 'read') else b''

        data = _as_bytes(data)
        state.received += len(data)
        combined = state.buffer + data
        base = state.received - len(combined)
        pos = 0
        records = StreamBatch(channel=state.channel)
        first_error = None
        mode = state.errors if errors is None else errors
        policy = state.checksums if checksums is None else checksums

        while True:
            newline = combined.find(b'\n', pos)
            if newline < 0:
                break
            raw = combined[pos:newline + 1]
            start = base + pos
            end = start + len(raw)
            record = self._parse_line(state, raw, start, end, policy)

            state.records.append(record)
            self.records.append(record)
            if record.status == OK:
                state.success_bytes += record.length
            else:
                state.discard_bytes += record.length
            if record.status == BAD:
                if mode == 'raise' and first_error is None:
                    first_error = record.error
                if mode == 'ignore':
                    pass
                else:
                    records.append(record)
            else:
                records.append(record)
            pos = newline + 1

        # Commit the tail before raising so caught exceptions still leave
        # consistent per-channel accounting.
        state.buffer = combined[pos:]
        pending = PendingTail(
            state.buffer, channel=state.channel,
            start=state.received - len(state.buffer),
            end=state.received, generation=state.generation)
        records.pending = pending
        records.tail = pending
        if state.buffer and include_pending:
            records.append(StreamRecord(
                state.channel, PENDING, state.buffer, pending.start,
                state.received, generation=state.generation,
                reason='incomplete'))
        if first_error is not None:
            raise first_error
        return records

    write = feed
    push = feed

    def _is_channel(self, value):
        try:
            return value in self._states
        except TypeError:
            return False

    def next(self, data=None, channel=None):
        '''Legacy-compatible entry point.

        Without channels it yields parsed sentences as before.  With a
        channel it returns that channel's records (and accepts
        ``next(channel, data)``).  With streams configured for several
        channels it reads one chunk from each.
        '''
        if channel is not None:
            if self._is_channel(data):
                data, channel = channel, data
            return self.feed(channel, data)

        if self._channel_order:
            if data is None and self.streams:
                records = []
                for ch in self._channel_order:
                    records.extend(self.feed(ch, None))
                return records
            if isinstance(data, dict):
                return self.feed(data)
            if self._is_channel(data):
                return self.feed(data, None)
            return self.feed(None, data)

        return self._next_legacy(data)

    __next__ = next

    def _next_legacy(self, data=None):
        if data is None:
            if self.stream:
                data = self.stream.readline()
            else:
                return

        lines = (self.buffer + data).split('\n')
        self.buffer = lines.pop()

        for line in lines:
            try:
                yield nmea.NMEASentence.parse(line)
            except nmea.ParseError as exc:
                if self.errors == 'raise':
                    raise exc
                if self.errors == 'yield':
                    yield exc

    def __iter__(self):
        return self

    # -- tails, reconnects and closing ------------------------------------

    def pending(self, channel=None):
        '''Return the buffered, not-yet-terminated bytes for a channel.'''
        if channel is None and len(self._channel_order) == 1:
            channel = self._channel_order[0]
        state = self._states.get(channel)
        if state is None:
            return PendingTail(b'', channel=channel)
        return PendingTail(
            state.buffer, channel=state.channel,
            start=state.received - len(state.buffer),
            end=state.received, generation=state.generation)

    tail = pending

    def pending_all(self):
        '''Return ``{channel: PendingTail}`` for every registered channel.'''
        return dict((ch, self.pending(ch)) for ch in self._channel_order)

    @property
    def buffers(self):
        '''Return ``{channel: buffered bytes}`` for every channel.'''
        return dict((ch, bytes(self._states[ch].buffer))
                    for ch in self._channel_order)

    @property
    def offsets(self):
        '''Return ``{channel: next byte offset}`` for every channel.'''
        return self.positions()

    def position(self, channel=None):
        '''Return the next absolute byte offset for a channel.'''
        if channel is None and len(self._channel_order) == 1:
            channel = self._channel_order[0]
        state = self._states.get(channel)
        return state.received if state is not None else 0

    def positions(self):
        return dict((ch, self._states[ch].received)
                    for ch in self._channel_order)

    def reconnect(self, channel=None, reason='reconnect', data=None):
        '''Start a new connection on one channel.

        Any buffered half sentence from the old connection is returned as a
        ``discard`` record and dropped.  The old tail therefore can never be
        joined to the first sentence of the new connection.  Byte offsets
        continue across the reconnect, while ``generation`` advances so
        records from the two connections remain distinguishable.
        '''
        if channel is None and len(self._channel_order) == 1:
            channel = self._channel_order[0]
        state = self._get_state(channel)
        record = self._discard_pending(state, reason)
        state.generation += 1
        state.closed = False
        records = StreamBatch([record] if record is not None else [],
                              channel=state.channel)
        records.pending = PendingTail(
            b'', channel=state.channel, start=state.received,
            end=state.received, generation=state.generation)
        records.tail = records.pending
        if data is not None:
            records.extend(self.feed(state.channel, data))
            records.pending = self.pending(state.channel)
            records.tail = records.pending
        return records

    reset = reconnect

    def _discard_pending(self, state, reason):
        if not state.buffer:
            return None
        start = state.received - len(state.buffer)
        record = StreamRecord(
            state.channel, DISCARD, state.buffer, start, state.received,
            generation=state.generation, reason=reason)
        state.records.append(record)
        self.records.append(record)
        state.discard_bytes += record.length
        state.buffer = b''
        return record

    def close(self, channel=None, reason='closed'):
        '''Close one channel or every channel.

        The buffered tail, if any, is returned as a ``discard`` record.
        Closing all channels returns one record per non-empty tail.  After
        closing, success plus discard ranges cover every received byte
        exactly once; see :meth:`coverage`.
        '''
        if channel is None:
            channels = list(self._channel_order)
        else:
            channels = [channel]
        records = []
        for ch in channels:
            state = self._states.get(ch)
            if state is None:
                continue
            record = self._discard_pending(state, reason)
            if record is not None:
                records.append(record)
            state.closed = True
        batch = StreamBatch(records, pending=PendingTail(b''))
        if len(channels) == 1 and channels[0] in self._states:
            batch.channel = channels[0]
        return batch

    finish = close
    close_all = close

    # -- accounting --------------------------------------------------------

    @property
    def successes(self):
        return [record for record in self.records if record.status == OK]

    @property
    def failures(self):
        return [record for record in self.records if record.status == BAD]

    @property
    def discards(self):
        return [record for record in self.records if record.status == DISCARD]

    def _coverage_for(self, state):
        ranges = [StreamRange(
            record.start, record.end, record.status,
            state.channel, record) for record in state.records]

        gaps = []
        overlaps = []
        cursor = 0
        for item in sorted(ranges, key=lambda entry: entry.start):
            if item.start > cursor:
                gaps.append((cursor, item.start))
            elif item.start < cursor:
                overlaps.append((item.start, cursor))
            cursor = max(cursor, item.end)
        if cursor < state.received:
            gaps.append((cursor, state.received))

        success_bytes = sum(item.length for item in ranges
                            if item.status == OK)
        discard_bytes = sum(item.length for item in ranges
                            if item.discarded)
        covered = success_bytes + discard_bytes
        return Coverage(
            channel=state.channel,
            received=state.received,
            covered=covered,
            success_bytes=success_bytes,
            discard_bytes=discard_bytes,
            pending_bytes=len(state.buffer),
            complete=(covered == state.received and not gaps and not overlaps),
            gaps=gaps,
            overlaps=overlaps,
            ranges=ranges,
            success_ranges=[item for item in ranges if item.status == OK],
            discard_ranges=[item for item in ranges if item.discarded],
            records=len(state.records),
            generation=state.generation,
            closed=state.closed,
        )

    def coverage(self, channel=None):
        '''Return ``{channel: Coverage}`` or one channel's coverage.'''
        if channel is None:
            return dict((ch, self._coverage_for(self._states[ch]))
                        for ch in self._channel_order)
        state = self._states.get(channel)
        if state is None:
            return Coverage(
                channel=channel, received=0, covered=0, success_bytes=0,
                discard_bytes=0, pending_bytes=0, complete=True, gaps=[],
                overlaps=[], ranges=[], success_ranges=[],
                discard_ranges=[], records=0, generation=0, closed=False)
        return self._coverage_for(state)

    def ranges(self, channel=None):
        '''Return the ordered byte ranges recorded for a channel.'''
        if channel is None:
            result = []
            for ch in self._channel_order:
                result.extend(self.ranges(ch))
            return result
        return list(self.coverage(channel)['ranges'])

    def _ranges_by_status(self, status, channel=None):
        return [item for item in self.ranges(channel) if item.status == status]

    def success_ranges(self, channel=None):
        return self._ranges_by_status(OK, channel)

    def bad_ranges(self, channel=None):
        return self._ranges_by_status(BAD, channel)

    def discard_ranges(self, channel=None):
        return [item for item in self.ranges(channel) if item.discarded]

    def stats(self, channel=None):
        '''Return per-channel counters, including bytes still pending.'''
        def one(ch):
            state = self._states.get(ch)
            if state is None:
                return {'channel': ch, 'received': 0, 'success_bytes': 0,
                        'discard_bytes': 0, 'pending_bytes': 0,
                        'records': 0, 'generation': 0, 'closed': False}
            return {
                'channel': ch,
                'received': state.received,
                'success_bytes': state.success_bytes,
                'discard_bytes': state.discard_bytes,
                'pending_bytes': len(state.buffer),
                'records': len(state.records),
                'generation': state.generation,
                'closed': state.closed,
            }
        if channel is None:
            return dict((ch, one(ch)) for ch in self._channel_order)
        return one(channel)

    channel_stats = stats

    def totals(self):
        result = {'received': 0, 'success_bytes': 0, 'discard_bytes': 0,
                  'pending_bytes': 0, 'records': 0}
        for ch in self._channel_order:
            values = self.stats(ch)
            for key in result:
                result[key] += values[key]
        return result


NMEAChannelReader = NMEAStreamReader
