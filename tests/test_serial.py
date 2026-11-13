# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen
#
# SPDX-License-Identifier: MIT

import pytest
from pytest import raises

from mido import Message
from mido.serial import (
    DEFAULT_BAUD_RATE,
    ScheduleConflict,
    SerialEvent,
    schedule_events,
)

BYTE_TIME = 10 / DEFAULT_BAUD_RATE


def event(message, earliest, latest=None, track=0, observed=None, label=None):
    return SerialEvent(
        message,
        track=track,
        earliest=earliest,
        latest=latest,
        observed=observed,
        label=label,
    )


def test_single_message_send_and_arrival_times():
    note = event(Message('note_on', note=60), 0.0, 0.01)

    schedule = schedule_events([note])

    assert schedule.total_bytes == 3
    assert schedule.events[0].send_time == 0.0
    assert schedule.events[0].arrival_time == 3 * BYTE_TIME
    assert schedule.events[0].wire_bytes == (0x90, 60, 64)
    assert schedule.events[0].running_status is False


def test_running_status_is_used_only_between_adjacent_matches():
    first = Message('note_on', channel=1, note=60)
    second = Message('note_on', channel=1, note=62)
    other = Message('control_change', channel=1, control=7, value=10)

    schedule = schedule_events([
        event(first, 0.0, 1.0, label='first'),
        event(second, 0.0, 1.0, label='second'),
        event(other, 0.0, 1.0, label='other'),
        event(second.copy(), 0.0, 1.0, label='fourth'),
    ])

    assert [item.bytes for item in schedule.events] == [3, 2, 3, 3]
    assert [item.running_status for item in schedule.events] == [
        False, True, False, False,
    ]
    assert schedule.total_bytes == 11
    assert schedule.events[1].wire_bytes == (62, 64)
    assert schedule.events[2].send_time == pytest.approx(5 * BYTE_TIME)


def test_realtime_message_preserves_running_status():
    note = Message('note_on', note=60)
    clock = Message('clock')

    schedule = schedule_events([
        event(note, 0.0, 1.0),
        event(clock, 0.0, 1.0),
        event(note.copy(), 0.0, 1.0),
    ])

    assert [item.bytes for item in schedule.events] == [3, 1, 2]
    assert [item.running_status for item in schedule.events] == [
        False, False, True,
    ]


def test_system_common_message_cancels_running_status():
    note = Message('note_on', note=60)
    tune = Message('tune_request')

    schedule = schedule_events([
        event(note, 0.0, 1.0),
        event(tune, 0.0, 1.0),
        event(note.copy(), 0.0, 1.0),
    ])

    assert [item.bytes for item in schedule.events] == [3, 1, 3]
    assert [item.running_status for item in schedule.events] == [
        False, False, False,
    ]


def test_track_order_is_preserved_while_tracks_interleave():
    schedule = schedule_events([
        event(Message('program_change', program=1), 0.0, 1.0,
              track='a', observed=2 * BYTE_TIME, label='a0'),
        event(Message('program_change', program=2), 0.0, 1.0,
              track='b', observed=4 * BYTE_TIME, label='b0'),
        event(Message('program_change', program=3), 0.0, 1.0,
              track='a', observed=6 * BYTE_TIME, label='a1'),
    ])

    labels = [item.label for item in schedule.events]
    assert labels == ['a0', 'b0', 'a1']
    assert labels.index('a0') < labels.index('a1')
    assert [item.arrival_time for item in schedule.events] == [
        2 * BYTE_TIME, 4 * BYTE_TIME, 6 * BYTE_TIME,
    ]


def test_earliest_window_can_delay_sending():
    note = event(Message('note_on', note=60), 0.01, 0.02)

    schedule = schedule_events([note])

    assert schedule.events[0].arrival_time == 0.01
    assert schedule.events[0].send_time == 0.01 - 3 * BYTE_TIME


def test_message_arrival_stays_inside_its_window():
    first = event(Message('note_on', note=60), 0.0, 3 * BYTE_TIME)
    second = event(
        Message('note_off', note=60),
        6 * BYTE_TIME,
        6 * BYTE_TIME,
    )

    schedule = schedule_events([first, second])

    assert schedule.events[0].arrival_time == 3 * BYTE_TIME
    assert schedule.events[1].arrival_time == 6 * BYTE_TIME
    assert schedule.events[1].send_time == 3 * BYTE_TIME


def test_conflict_reports_a_minimal_witness():
    long_sysex = Message('sysex', data=tuple(range(4)))
    tight = event(long_sysex, 0.0, 0.0, label='tight')

    with raises(ScheduleConflict) as info:
        schedule_events([tight])

    assert info.value.indices == (0,)
    assert info.value.events[0].label == 'tight'
    assert info.value.required_seconds > info.value.available_seconds


def test_observed_time_outside_window_is_rejected():
    note = event(
        Message('note_on'), earliest=0.0, latest=0.001, observed=0.002,
    )

    with raises(ScheduleConflict, match='observed time'):
        schedule_events([note])


def test_solver_waits_for_a_later_urgent_event():
    long_message = Message('sysex', data=tuple(range(4)))
    urgent = Message('program_change', program=2)

    schedule = schedule_events(
        [
            event(long_message, 0.0, 1.1, track='a', label='long'),
            event(urgent, 0.3, 0.5, track='b', label='urgent'),
        ],
        baud_rate=100,
        bits_per_byte=10,
    )

    assert [item.label for item in schedule.events] == ['urgent', 'long']
    assert schedule.events[0].send_time == pytest.approx(0.1)
    assert schedule.events[0].arrival_time == pytest.approx(0.3)
    assert schedule.events[1].arrival_time <= 1.1


def test_observed_arrival_is_reproduced():
    note = event(
        Message('note_on', note=60),
        earliest=0.0,
        latest=0.01,
        observed=0.004,
    )

    schedule = schedule_events([note])

    assert schedule.events[0].arrival_time == 0.004
    assert schedule.events[0].send_time == 0.004 - 3 * BYTE_TIME


def test_swap_of_independent_events_can_save_one_byte():
    first = Message('control_change', channel=0, control=1, value=10)
    second = Message('note_on', channel=0, note=60)
    third = Message('control_change', channel=0, control=2, value=20)
    window = 0.0, 1.0

    original = schedule_events([
        event(first, *window, track='a', observed=0.001, label='cc-1'),
        event(second, *window, track='b', observed=0.002, label='note'),
        event(third, *window, track='a', observed=0.003, label='cc-2'),
    ])

    swapped = schedule_events([
        event(first, *window, track='a', observed=0.001, label='cc-1'),
        event(third, *window, track='b', observed=0.002,
              label='cc-2-moved'),
        event(second, *window, track='c', observed=0.003, label='note'),
    ])

    assert original.total_bytes == 9
    assert swapped.total_bytes == 8
    assert [item.message.control for item in swapped.events[:2]] == [1, 2]
    assert swapped.events[1].running_status is True
    assert [item.arrival_time for item in original] == [0.001, 0.002, 0.003]


def test_exact_search_minimizes_wire_bytes():
    cc1 = Message('control_change', control=1)
    note = Message('note_on', note=60)
    cc2 = Message('control_change', control=2)

    schedule = schedule_events([
        event(cc1, 0.0, 1.0, track='a', label='cc1'),
        event(note, 0.0, 1.0, track='b', label='note'),
        event(cc2, 0.0, 1.0, track='a', label='cc2'),
    ])

    labels = [item.label for item in schedule]
    assert labels.index('cc1') < labels.index('cc2')
    assert schedule.total_bytes == 8
    assert any(item.running_status for item in schedule)


def test_swap_cannot_reverse_events_from_same_track():
    first = Message('control_change', control=1)
    second = Message('control_change', control=2)
    third = Message('note_on', note=60)

    schedule = schedule_events([
        event(first, 0.0, 1.0, track='a', label='first'),
        event(second, 0.0, 1.0, track='a', label='second'),
        event(third, 0.0, 1.0, track='b', label='third'),
    ])

    labels = [item.label for item in schedule.events]
    assert labels.index('first') < labels.index('second')


def test_schedule_is_recomputable_from_dicts():
    note = event(Message('note_on', note=60), 0.0, 0.01)
    schedule = schedule_events([note])

    data = schedule.as_dict()

    assert data['byte_time'] == BYTE_TIME
    assert data['events'][0]['send_time'] == 0.0
    assert data['events'][0]['bytes'] == 3
    assert data['events'][0]['wire_bytes'] == [0x90, 60, 64]
    assert schedule.as_dicts()[0]['arrival_time'] == 3 * BYTE_TIME


def test_empty_schedule():
    schedule = schedule_events([])

    assert schedule.events == ()
    assert schedule.total_bytes == 0
    assert schedule.finish_time == 0.0


def test_observed_time_before_start_is_infeasible():
    note = event(
        Message('note_on'),
        earliest=-1.0,
        latest=1.0,
        observed=-0.001,
    )

    with raises(ScheduleConflict):
        schedule_events([note])


def test_earliest_before_start_is_clamped_to_the_line_start():
    note = event(Message('note_on'), earliest=-1.0, latest=1.0)

    schedule = schedule_events([note], start_time=0.5)

    assert schedule.events[0].send_time == 0.5
    assert schedule.events[0].arrival_time == pytest.approx(
        0.5 + 3 * BYTE_TIME,
    )
    assert schedule.start_time == 0.5


def test_large_set_conflict_always_reports_a_witness():
    # 12 observed events whose first send deadline is already in the past at
    # start_time: the greedy witness can be empty, so the fallback must still
    # return a non-empty conflicting group instead of failing internally.
    events = [
        event(
            Message('note_on', note=i),
            earliest=-1.0,
            latest=1.0,
            observed=-0.001,
            label=f'late-{i}',
        )
        for i in range(12)
    ]

    with raises(ScheduleConflict) as info:
        schedule_events(events)

    assert len(info.value.events) >= 1

