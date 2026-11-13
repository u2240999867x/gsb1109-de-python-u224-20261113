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


## Compass deviation table audit

`pynmea2.compass` audits a magnetic compass deviation table (罗经自差表)
against recorded NMEA data. The table is organised by ship's heading band;
each `HDG` sentence supplies the magnetic sensor heading plus deviation and
variation (each tagged E/W), while `HDT` (or the true course in `RMC`)
supplies the reference true heading.

Angle arithmetic is always wrapped, so the 359 deg -> 1 deg seam never
produces a spurious 358 deg error. East deviation/variation is positive,
west is negative. References are only paired inside a configurable time
window, and a turn where several references match one `HDG` is reported as
an error range rather than a single value.

```python
from pynmea2 import compass

# {band centre in degrees: signed deviation}, E positive / W negative
table = {0: 2.0, 90: 7.0, 180: -13.0, 270: 7.0}

audit = compass.audit_file('compass.log', table, step=15,
                           max_time_gap=5.0, tolerance=2.0,
                           spread_tolerance=2.0)
print(compass.format_report(audit))
```

Per-band and overall verdicts distinguish four situations:

- `ok` — the tabulated correction reproduces the reference true heading;
- `constant_bias` — every populated band shows the same residual, i.e. a
  heading-independent offset rather than an east/west problem;
- `sign_flipped` — residuals match the prediction of reversing the E/W
  letter (residual = -2 × tabulated deviation);
- `inconclusive` — turn-induced range too wide, mixed evidence, or a single
  populated band, so bias versus sign reversal cannot currently be separated.

A command line front end is also available; the table file lists
`heading magnitude E/W` (or a signed deviation) per line:

```bash
python -m pynmea2.compass compass.log --table deviation.txt --step 15 --window 5
```

See `examples/compass_audit.py` for a self-contained worked example.

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
