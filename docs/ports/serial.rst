.. SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen
..
.. SPDX-License-Identifier: CC-BY-4.0

Serial Event Scheduling
-----------------------

A classic MIDI serial line transmits one byte at a time. The default MIDI
baud rate is 31,250 bits per second and a serial byte uses one start bit,
eight data bits and one stop bit, so each byte takes 10/31250 seconds.

``mido.serial`` turns messages with allowed arrival windows into a
device-readable event table. Each event records:

* the message to transmit,
* the earliest and latest accepted arrival time,
* an optional observed arrival time from the archive,
* a track identifier.

Messages on the same track always keep their order. Messages on different
tracks may be interleaved or re-ordered when that produces a feasible table.

The scheduler uses the actual on-wire length of each message. Running status
omits a status byte only when the adjacent channel messages have the same
status byte. System common and SysEx messages cancel running status; real-time
messages may be inserted without cancelling it.

Building a table
^^^^^^^^^^^^^^^^

.. code-block:: python

    from mido import Message, SerialEvent, schedule_events

    events = [
        SerialEvent(
            Message('note_on', note=60),
            track='melody',
            earliest=0.0,
            latest=0.01,
            observed=0.002,
        ),
        SerialEvent(
            Message('note_off', note=60),
            track='melody',
            earliest=0.1,
            latest=0.2,
            observed=0.12,
        ),
    ]

    schedule = schedule_events(events)

    for item in schedule:
        print(
            item.label,
            item.send_time,
            item.arrival_time,
            item.wire_bytes,
            item.running_status,
        )

When ``observed`` is set, the scheduler reproduces that arrival exactly,
provided it lies inside the event's window and the line can be free in time.
Without ``observed``, it chooses the earliest arrival that satisfies all
windows.

Use ``as_dict()`` or ``as_dicts()`` for a recomputable JSON-like table. The
dictionary includes send time, arrival time, byte offset, wire bytes and the
running-status decision for every event.

Conflicts
^^^^^^^^^

If no ordering and timing can satisfy all windows, ``schedule_events()``
raises :class:`~mido.serial.ScheduleConflict`. The exception contains a small
witness group of events that cannot all meet their windows, along with the
minimum required line time and the available shared-line time.

A complete fixed sample, including a two-message swap that saves exactly one
status byte, is available in ``examples/serial_schedule.py``.
