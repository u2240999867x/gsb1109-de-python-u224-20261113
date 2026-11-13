#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen
#
# SPDX-License-Identifier: MIT

"""
Build a device-readable serial event table from an observed MIDI archive.

The archive records each message with an observed arrival time and an
allowed arrival window. Messages on the same track keep their order;
messages on different tracks may be re-ordered when it helps.

Two things are checked before a table is emitted:

  * every observed arrival must fall inside its window, and the message
    must finish transmitting on the shared serial line inside that window;
  * the actual on-wire length is used, including the running-status byte
    that can only be omitted between two adjacent messages with the same
    status byte.

Run this file to see:

  1. the fixed archived sample,
  2. the same sample after swapping two messages with no ordering
     requirement between them, which saves exactly one status byte, and
  3. a sample whose windows cannot all be satisfied, with the conflicting
     message group reported as a witness.
"""

from mido import Message, ScheduleConflict, SerialEvent, schedule_events


def show(title, events):
    print(title)
    print('-' * len(title))
    try:
        schedule = schedule_events(events)
    except ScheduleConflict as conflict:
        print('  no feasible table; conflicting messages: '
              f'{conflict.indices}')
        if conflict.required_seconds is not None:
            print(f'  need {conflict.required_seconds:.6f}s of line time but '
                  f'only {conflict.available_seconds:.6f}s is available')
        print()
        return

    print(f'  {"label":<14}{"send":>12}{"arrival":>12}{"bytes":>7}'
          f'{"  running":>9}')
    for item in schedule:
        name = item.label if item.label is not None else str(item.index)
        saved = 'yes' if item.running_status else 'no'
        print(f'  {name:<14}{item.send_time:>12.6f}'
              f'{item.arrival_time:>12.6f}{item.bytes:>7}{saved:>9}')
    print(f'  total on-wire bytes: {schedule.total_bytes}')
    print(f'  last arrival:        {schedule.finish_time:.6f}s')
    print()


cc_1 = Message('control_change', channel=0, control=1, value=10)
note = Message('note_on', channel=0, note=60)
cc_2 = Message('control_change', channel=0, control=2, value=20)

# Fixed archived sample. cc-1 and cc-2 are on the same track, so their
# relative order is fixed; the note on the other track sits between them.
# Each window is a +/- 5 ms tolerance around the observed arrival.
archived = [
    SerialEvent(cc_1, track=1, earliest=-0.004, latest=0.006,
                observed=0.001, label='cc-1'),
    SerialEvent(note, track=2, earliest=-0.003, latest=0.007,
                observed=0.002, label='note'),
    SerialEvent(cc_2, track=1, earliest=-0.002, latest=0.008,
                observed=0.003, label='cc-2'),
]

# Re-archived sample. The note and cc-2 have no ordering requirement
# between them, so the two control changes can be made adjacent.
re_archived = [
    SerialEvent(cc_1, track=1, earliest=-0.004, latest=0.006,
                observed=0.001, label='cc-1'),
    SerialEvent(cc_2, track=1, earliest=-0.003, latest=0.007,
                observed=0.002, label='cc-2'),
    SerialEvent(note, track=2, earliest=-0.002, latest=0.008,
                observed=0.003, label='note'),
]

# A SysEx message that takes longer than its whole arrival window.
impossible = [
    SerialEvent(
        Message('sysex', data=tuple(range(4))),
        track=1,
        earliest=0.0,
        latest=0.001,
        label='long-sysex',
    ),
]

show('Archived sample (ordering fixed inside track 1)', archived)
show('Re-archived sample (note and cc-2 swapped)', re_archived)
show('Unschedulable sample', impossible)
