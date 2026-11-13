# pynmea2

`pynmea2` is a python library for the [NMEA 0183](http://en.wikipedia.org/wiki/NMEA_0183) protocol

`pynmea2` is based on [`pynmea`](https://code.google.com/p/pynmea/) by Becky Lewis

The `pynmea2` homepage is located at <http://github.com/Knio/pynmea2>

## Compatibility

`pynmea2` is compatable with Python 2.7 and Python 3.4+

![Python version](https://img.shields.io/pypi/pyversions/pynmea2.svg?style=flat)
[![Build status](https://github.com/Knio/pynmea2/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Knio/pynmea2/actions/workflows/ci.yml?query=branch%3Amaster+)
[![Coverage status](https://img.shields.io/coveralls/github/Knio/pynmea2/master.svg?style=flat)](https://coveralls.io/r/Knio/pynmea2?branch=master)

## Installation

The recommended way to install `pynmea2` is with
[pip](http://pypi.python.org/pypi/pip/):

```bash
pip install pynmea2
```

[![PyPI version](https://img.shields.io/pypi/v/pynmea2.svg?style=flat)](https://pypi.org/project/pynmea2/)
[![PyPI downloads](https://img.shields.io/pypi/dm/pynmea2.svg?style=flat)](https://pypi.org/project/pynmea2/)

## Parsing

You can parse individual NMEA sentences using the `parse(data)` function, which takes a string containing a
NMEA 0183 sentence and returns a `NMEASentence` object. Note that the leading '$' is optional and trailing whitespace is ignored when parsing a sentence.

Example:

```python
>>> import pynmea2
>>> msg = pynmea2.parse("$GPGGA,184353.07,1929.045,S,02410.506,E,1,04,2.6,100.00,M,-33.9,M,,0000*6D")
>>> msg
<GGA(timestamp=datetime.time(18, 43, 53), lat='1929.045', lat_dir='S', lon='02410.506', lon_dir='E', gps_qual='1', num_sats='04', horizontal_dil='2.6', altitude=100.0, altitude_units='M', geo_sep='-33.9', geo_sep_units='M', age_gps_data='', ref_station_id='0000')>
```

The `NMEASentence` object has different properties, depending on its sentence type.
The `GGA` message has the following properties:

```python
>>> msg.timestamp
datetime.time(18, 43, 53)
>>> msg.lat
'1929.045'
>>> msg.lat_dir
'S'
>>> msg.lon
'02410.506'
>>> msg.lon_dir
'E'
>>> msg.gps_qual
'1'
>>> msg.num_sats
'04'
>>> msg.horizontal_dil
'2.6'
>>> msg.altitude
100.0
>>> msg.altitude_units
'M'
>>> msg.geo_sep
'-33.9'
>>> msg.geo_sep_units
'M'
>>> msg.age_gps_data
''
>>> msg.ref_station_id
'0000'
```

Additional properties besides the ones explicitly in the message data may also exist.

For example, `latitude` and `longitude` properties exist as helpers to access the geographic coordinates as python floats ([DD](http://en.wikipedia.org/wiki/Decimal_degrees), "decimal degrees") instead of the DDDMM.MMMM ("Degrees, minutes, seconds") format used in the NMEA protocol. `latitude_minutes`, `latitude_seconds`, `longitude_minutes`, and `longitude_seconds` are also supported and allow easy creation of differently formatted location strings.

```python
>>> msg.latitude
-19.4840833333
>>> msg.longitude
24.1751
>>> '%02d°%07.4f′' % (msg.latitude, msg.latitude_minutes)
'-19°29.0450′'
>>> '%02d°%02d′%07.4f″' % (msg.latitude, msg.latitude_minutes, msg.latitude_seconds)
"-19°29′02.7000″"
```

Sentence types that pynmea2 has no definition for are still parsed: a talker sentence is returned as a plain `TalkerSentence` and a proprietary sentence as a plain `ProprietarySentence`, with the raw field values in `data`. Input that is not an NMEA sentence still raises `ParseError`.


## Archiving: resolving UTC dates

Most sentences only carry an UTC time of day; full dates arrive occasionally in RMC/ZDA sentences.  `pynmea2.archive.DateResolver` walks one voyage in arrival order and, given the out-of-order `max_delay` tolerated by the archive, returns every sentence with the set of UTC dates that are still possible for it.

Two adjacent sentences are assumed less than a day apart, so the clock may fall back by at most `max_delay` and cross at most one midnight per step.  A `23:59` record arriving *after* a `00:01` record therefore keeps the earlier date instead of being pushed a day forward.

```python
import datetime
import pynmea2
from pynmea2.archive import DateResolver

voyage = [
    pynmea2.parse('$GPGGA,000100,3926.276,N,07739.361,W,1,04,2.6,100.0,M,-33.9,M,,*77'),
    pynmea2.parse('$GPGGA,235900,3926.276,N,07739.361,W,1,04,2.6,100.0,M,-33.9,M,,*7B'),
    pynmea2.parse('$GPRMC,000200,A,3926.276,N,07739.361,W,0.0,0.0,100326,,E*4D'),
]

resolver = DateResolver(datetime.timedelta(minutes=5))
for resolution in resolver.resolve(voyage):
    print(resolution.possible_dates)
# [datetime.date(2026, 3, 9), datetime.date(2026, 3, 10)]
# [datetime.date(2026, 3, 9)]
# [datetime.date(2026, 3, 10)]
```

The late `23:59` record stays on 9 March rather than being pushed forward.  The `00:01` record is genuinely ambiguous inside the five-minute window, so both feasible dates are returned; a strict `DateResolver()` (zero delay) pins it to 9 March alone.

When the truth is ambiguous every feasible date is returned; a single-element list means the date is pinned (also available via `resolution.date` and `resolution.datetime`).  A voyage without any RMC/ZDA anchor cannot be dated and returns empty lists rather than guessing.

Adjacent date anchors that contradict each other (for example the second anchor dates backwards) cannot be reconciled.  `resolve()` raises a `DateConflict` whose `sentences` attribute holds the raw NMEA strings of the affected segment.  Use `inspect()` to keep the resolvable records and collect the conflicts instead, or `resolve_voyages()` to process several voyages so a contradiction in one voyage never blocks the others.  The conflicting RMC/ZDA anchors themselves remain fully dated (their own date is factual); only the time-only records between them lose their date and are flagged.

## Checksums

parse has an optional argument:

`parse(data, checksums='check' (DEFAULT) | 'required' | 'my_data_is_corrupt')`

| checksums value | `'required'` | `'check'` (default value) | `'my_data_is_corrupt'` |
|----|----|----|----|
| sentence with valid checksum | accepted | accepted | accepted |
| sentence without checkum  | `ChecksumError` | accepted | accepted |
| sentence with invalid checksum  | `ChecksumError` | `ChecksumError` | accepted |

A note about invalid checksums: if a checksum is invalid, it is more likely than not that the sentence values themselves are corrupt, so be wary of using them without further validation. The corruption may also cause parsing errors which could raise `ParseError` even before the `checksums=` parameter takes effect, so you will still have to handle an exception


## Generating

You can create a `NMEASentence` object by calling the constructor with talker, message type, and data fields:

```python
>>> import pynmea2
>>> msg = pynmea2.GGA('GP', 'GGA', ('184353.07', '1929.045', 'S', '02410.506', 'E', '1', '04', '2.6', '100.00', 'M', '-33.9', 'M', '', '0000'))
```

and generate a NMEA string from a `NMEASentence` object:

```python
>>> str(msg)
'$GPGGA,184353.07,1929.045,S,02410.506,E,1,04,2.6,100.00,M,-33.9,M,,0000*6D'
```

## File reading example

See [examples/read_file.py](/examples/read_file.py)

```python
import pynmea2

file = open('examples/data.log', encoding='utf-8')

for line in file.readlines():
    try:
        msg = pynmea2.parse(line)
        print(repr(msg))
    except pynmea2.ParseError as e:
        print('Parse error: {}'.format(e))
        continue
```

## `pySerial` device example

See [examples/read_serial.py](/examples/read_serial.py)

```python
import io

import pynmea2
import serial


ser = serial.Serial('/dev/ttyS1', 9600, timeout=5.0)
sio = io.TextIOWrapper(io.BufferedRWPair(ser, ser))

while 1:
    try:
        line = sio.readline()
        msg = pynmea2.parse(line)
        print(repr(msg))
    except serial.SerialException as e:
        print('Device error: {}'.format(e))
        break
    except pynmea2.ParseError as e:
        print('Parse error: {}'.format(e))
        continue
```
