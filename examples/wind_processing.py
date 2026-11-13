"""
Geographic wind observations from interleaved NMEA streams.

The heading (HDT), speed/course (RMC) and wind (MWV) sentences arrive at
different rates and in any order.  WindProcessor keeps the latest sample of
each stream, remembers where every number came from, and turns relative MWV
sentences into true-north wind while leaving native true-wind MWV untouched.
"""
from __future__ import print_function

import pynmea2
from pynmea2 import WindProcessor


LOG = """
$HEHDT,90.0,T*00
$GPRMC,120000,A,0.0,N,0.0,E,10.0,90.0,010123,,,A*00
$WIMWV,0.0,R,20.0,N,A*00
$WIMWV,200.0,T,15.0,N,A*00
$GPVTG,90.0,T,,M,10.0,N,,K,A*00
$WIMWV,45.0,R,10.0,N,A*00
"""


def main():
    processor = WindProcessor()

    for line in LOG.strip().splitlines():
        msg = pynmea2.parse(line, checksums='my_data_is_corrupt')
        observation = processor.update(msg)
        if observation is None:
            continue

        print('%s ->' % observation.mwv_source)
        if observation.wind_direction is not None:
            print('  true wind: %.1f deg at %.2f knots'
                  % (observation.wind_direction,
                     observation.wind_speed_knots))
        if observation.apparent_wind_direction_true is not None:
            print('  apparent wind from %.1f deg true'
                  % observation.apparent_wind_direction_true)
        if observation.heading_source:
            print('  heading %.1f from %s'
                  % (observation.heading, observation.heading_source))
        if observation.speed_source:
            print('  speed %.2f knots from %s (%s)'
                  % (observation.speed_knots, observation.speed_source,
                     observation.speed_reference))
        for note in observation.notes:
            print('  note: %s' % note)
        for item in observation.missing:
            print('  missing: %s' % item)


if __name__ == '__main__':
    main()
