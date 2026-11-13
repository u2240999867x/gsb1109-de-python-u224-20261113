"""Read four interleaved serial ports with per-port NMEA buffering.

Each port receives chunks independently and the chunks can split sentences
anywhere.  This example feeds the same bytes through several ports, then
reconnects one port and closes the others to show how the byte accounting
covers every received byte exactly once.
"""

import pynmea2

SENTENCES = [
    "$GPGGA,184353.07,1929.045,S,02410.506,E,1,04,2.6,100.00,M,-33.9,M,,0000*6D\n",
    "$GPRMC,181031.576,V,3926.276,N,07739.361,W,99.7,18.30,250915,,E*79\n",
]


def describe(records):
    return [(record.channel, record.status, record.byte_range)
            for record in records]


def main():
    ports = ['gps', 'ais', 'wind', 'depth']
    reader = pynmea2.NMEAStreamReader(channels=ports)

    for index, port in enumerate(ports):
        data = SENTENCES[index % len(SENTENCES)]
        # Split each chunk in the middle to exercise the tails.
        reader.feed(port, data[:17])
        reader.feed(port, data[17:])

    print('success:', describe(reader.successes))
    print('bad:', describe(reader.failures))

    # The gps port reconnects; its old tail is dropped, never joined.
    reader.feed('gps', SENTENCES[0][:9])
    reader.reconnect('gps')

    # Close every port; remaining tails become explicit discard records.
    reader.close()

    for port in ports:
        coverage = reader.coverage(port)
        print(port, 'received=%d covered=%d complete=%s' % (
            coverage.received, coverage.covered, coverage.complete))


if __name__ == '__main__':
    main()
