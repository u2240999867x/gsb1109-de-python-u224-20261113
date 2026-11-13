#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 The mido contributors
#
# SPDX-License-Identifier: MIT

"""
Merge marker tables across tracks that are copies of each other.

Track 0 is the primary director track and track 1 is a replica on
which the same markers were copied with small timing differences. The
first replica marker sits within the tolerance of two adjacent primary
markers, so that row stays unresolved with both interpretations.
"""
from mido import MetaMessage, MidiFile

mid = MidiFile(ticks_per_beat=480)

primary = mid.add_track('director')
primary.append(MetaMessage('marker', text='scene A', time=100))
primary.append(MetaMessage('marker', text='scene A', time=100))
primary.append(MetaMessage('marker', text='scene B', time=200))

replica = mid.add_track('replica')
replica.append(MetaMessage('marker', text='scene A', time=150))
replica.append(MetaMessage('marker', text='scene B', time=255))
replica.append(MetaMessage('marker', text='scene C', time=495))

table = mid.merge_markers(replica_groups=[(0, 1)], tolerance=60)

for group in table:
    status = 'resolved' if group.resolved else 'UNRESOLVED'
    print('{} {!r} at tick {} ({})'.format(
        status, group.name, group.time, group.tracks))
    for copy in group.copies:
        print('  copy on track {} at tick {}'.format(
            copy.track, copy.time))
    for candidate in group.candidates:
        print('  alternative: tick {}'.format(candidate.time))

resolved, unresolved = table.counts
print('settled: {} certain, {} pending'.format(resolved, unresolved))
