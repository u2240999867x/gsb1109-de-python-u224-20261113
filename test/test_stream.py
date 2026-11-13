import pytest

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

import pynmea2

DATA = "$GPGGA,184353.07,1929.045,S,02410.506,E,1,04,2.6,100.00,M,-33.9,M,,0000*6D\n"


def test_stream():
    sr = pynmea2.NMEAStreamReader()
    assert len(list(sr.next(''))) == 0
    assert len(list(sr.next(DATA))) == 1
    assert len(list(sr.next(DATA))) == 1

    sr = pynmea2.NMEAStreamReader()
    assert len(list(sr.next(DATA))) == 1
    assert len(list(sr.next(DATA[:10]))) == 0
    assert len(list(sr.next(DATA[10:]))) == 1

    sr = pynmea2.NMEAStreamReader()
    assert list(sr.next()) == []

    f = StringIO(DATA * 2)
    sr = pynmea2.NMEAStreamReader(f)
    assert len(list(sr.next())) == 1
    assert len(list(sr.next())) == 1
    assert len(list(sr.next())) == 0


def test_iter():
    sr = pynmea2.NMEAStreamReader(StringIO(DATA))
    for batch in sr:
        for msg in batch:
            assert isinstance(msg, pynmea2.GGA)
            break
        break


def test_raise_errors():
    sr = pynmea2.NMEAStreamReader(errors='raise')
    assert list(sr.next('foobar')) == []
    with pytest.raises(pynmea2.ParseError):
        assert list(sr.next('foo\n'))


def test_yield_errors():
    sr = pynmea2.NMEAStreamReader(errors='yield')
    assert list(sr.next('foobar')) == []
    data = list(sr.next('foo\n' + DATA))
    assert len(data) == 2
    assert isinstance(data[0], pynmea2.ParseError)
    assert isinstance(data[1], pynmea2.GGA)


def test_ignore_errors():
    sr = pynmea2.NMEAStreamReader(errors='ignore')
    assert list(sr.next('foobar')) == []
    data = list(sr.next('foo\n' + DATA))
    assert len(data) == 1
    assert isinstance(data[0], pynmea2.GGA)


def test_bad_error_value():
    with pytest.raises(ValueError):
        sr = pynmea2.NMEAStreamReader(errors='bad')


# ---------------------------------------------------------------------------
# Multiplexed serial ports
# ---------------------------------------------------------------------------

BAD_CHECKSUM = DATA.replace('*6D', '*00')
GARBAGE = 'not an nmea sentence\n'


def test_channels_keep_independent_partial_tails():
    reader = pynmea2.NMEAStreamReader(channels=4)

    assert reader.feed(0, DATA[:10]) == []
    assert reader.pending(0) == DATA[:10]
    assert reader.pending(0).byte_range == (0, 10)

    assert len(reader.feed(1, DATA)) == 1
    assert reader.pending(0) == DATA[:10]

    record, = reader.feed(0, DATA[10:])
    assert record.status == 'ok'
    assert record.sentence.sentence_type == 'GGA'
    assert record.byte_range == (0, len(DATA))
    assert reader.pending(0) == ''
    assert reader.pending(1) == ''


def test_interleaved_chunks_classify_ok_bad_and_pending():
    reader = pynmea2.NMEAStreamReader(channels=['gps', 'ais', 'wind'])

    assert reader.feed('gps', DATA[:20]) == []
    assert reader.feed('ais', BAD_CHECKSUM)[0].status == 'bad'
    assert reader.feed('wind', GARBAGE)[0].status == 'bad'

    record, = reader.feed('gps', DATA[20:])
    assert record.status == 'ok'


def test_bad_sentence_reports_reason_and_checksum_state():
    reader = pynmea2.NMEAStreamReader(channels=1)

    checksum, = reader.feed(0, BAD_CHECKSUM)
    assert checksum.status == 'bad'
    assert isinstance(checksum.error, pynmea2.ChecksumError)
    assert checksum.checksum_ok is False

    garbage, = reader.feed(0, GARBAGE)
    assert garbage.status == 'bad'
    assert isinstance(garbage.error, pynmea2.ParseError)
    assert not isinstance(garbage.error, pynmea2.ChecksumError)
    assert garbage.checksum_ok is None


def test_per_channel_checksum_policy():
    reader = pynmea2.NMEAStreamReader(
        channels=['strict', 'lenient'],
        channel_checksums={'strict': 'required',
                           'lenient': 'my_data_is_corrupt'})
    assert reader.feed('strict', BAD_CHECKSUM)[0].status == 'bad'
    assert reader.feed('lenient', BAD_CHECKSUM)[0].status == 'ok'


def test_reconnect_discards_old_tail_and_never_joins_it_to_new_sentence():
    reader = pynmea2.NMEAStreamReader(channels=['gps', 'ais'])
    assert reader.feed('gps', DATA[:15]) == []
    assert reader.feed('ais', DATA[:20]) == []

    lost, = reader.reconnect('gps')
    assert lost.status == 'discard'
    assert lost.reason == 'reconnect'
    assert lost.raw == DATA[:15]
    assert lost.byte_range == (0, 15)
    assert reader.pending('gps') == ''
    # Other ports are untouched by the reconnect.
    assert reader.pending('ais') == DATA[:20]

    record, = reader.feed('gps', DATA)
    assert record.status == 'ok'
    assert record.generation == 1
    assert lost.generation == 0
    assert len(record.sentence.data) == 14


def test_close_each_port_discards_tail_and_accounting_covers_all_bytes():
    reader = pynmea2.NMEAStreamReader(channels=['a', 'b', 'c'])
    reader.feed('a', DATA)
    reader.feed('a', DATA[:12])
    reader.feed('b', BAD_CHECKSUM + GARBAGE)
    reader.feed('c', DATA[:5])

    assert [record.channel for record in reader.close('a')] == ['a']
    assert reader.close('a') == []
    assert reader.pending('a') == ''

    closed = reader.close()
    assert len(closed) == 1
    assert closed[0].channel == 'c'
    assert closed[0].status == 'discard'
    assert closed[0].raw == DATA[:5]

    for channel in ('a', 'b', 'c'):
        coverage = reader.coverage(channel)
        assert coverage.received == coverage.covered
        assert coverage.gaps == []
        assert coverage.overlaps == []
        assert coverage.complete is True

    assert reader.coverage('a').success_bytes == len(DATA)
    assert reader.coverage('a').discard_bytes == 12
    assert reader.coverage('b').success_bytes == 0
    assert reader.coverage('b').discard_bytes == len(BAD_CHECKSUM) + len(GARBAGE)


def test_success_and_discard_ranges_are_disjoint_and_ordered():
    reader = pynmea2.NMEAStreamReader(channels=['a', 'b'])
    reader.feed('a', GARBAGE + DATA + DATA[:9])
    reader.feed('b', DATA + BAD_CHECKSUM)

    for channel in ('a', 'b'):
        ranges = sorted(reader.ranges(channel), key=lambda item: item.start)
        assert ranges[0].start == 0
        pending = reader.pending(channel)
        assert ranges[-1].end == pending.start
        assert pending.end == reader.position(channel)
        for previous, current in zip(ranges, ranges[1:]):
            assert previous.end == current.start

        success = reader.success_ranges(channel)
        discarded = reader.discard_ranges(channel)
        assert sum(item.length for item in success + discarded) + len(pending) == (
            reader.position(channel))
        success_starts = {(item.start, item.end) for item in success}
        assert success_starts.isdisjoint(
            (item.start, item.end) for item in discarded)


def test_byte_input_offsets_match_bytes_received():
    reader = pynmea2.NMEAStreamReader(channels=1)
    data = DATA.encode('ascii')

    assert reader.feed(0, data[:17]) == []
    record, = reader.feed(0, data[17:])
    assert record.byte_range == (0, len(data))
    assert record.raw == data
    assert record.length == len(data)


def test_channel_mapping_batch_feeds_every_port():
    reader = pynmea2.NMEAStreamReader(channels=['a', 'b'])
    records = reader.feed({'a': DATA, 'b': DATA[:9]})

    assert [record.channel for record in records] == ['a']
    assert reader.pending('a') == ''
    assert reader.pending('b') == DATA[:9]
    assert reader.positions() == {'a': len(DATA), 'b': 9}


def test_legacy_stream_interface_is_unchanged():
    reader = pynmea2.NMEAStreamReader()
    assert list(reader.next('')) == []
    assert len(list(reader.next(DATA))) == 1
    assert list(reader.next(DATA[:10])) == []
    assert len(list(reader.next(DATA[10:]))) == 1
    assert list(reader.next()) == []
