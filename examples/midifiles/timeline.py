#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Show the whole-file tempo timeline of a MIDI file.

Tempo changes are collected from every track for type 0 and type 1 files.
If two tracks declare different tempos at the same tick, the earliest
uncertain tick is printed too.
"""

import argparse

from mido import MidiFile


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('midifile')
    return parser.parse_args()


args = parse_args()
mid = MidiFile(args.midifile)
timeline = mid.timeline

if timeline.type == 2:
    for track in timeline.tracks:
        print(f'=== Track {track.index} ({track.name})')
        for row in track.event_table:
            print(f"{row['tick']:8d} {row['seconds']:10.6f} {row['type']}")
else:
    for row in timeline.event_table:
        print(f"{row['track']:3d} {row['tick']:8d} "
              f"{row['seconds']:10.6f} {row['type']}")

conflict = timeline.first_uncertainty
if conflict is not None:
    print(f'uncertain from tick {conflict.start_tick} '
          f'({conflict.start_seconds:.6f}s), tempos {conflict.tempos}')
