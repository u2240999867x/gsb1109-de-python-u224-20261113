from itertools import combinations
from math import isfinite

from .messages import Message
from .midifiles.meta import MetaMessage

DEFAULT_BAUD_RATE = 31250
DEFAULT_BITS_PER_BYTE = 10
_EXACT_SEARCH_LIMIT = 10
_EPSILON = 1e-12


class SerialEvent:
    """A MIDI message with an allowed serial arrival window."""

    __slots__ = (
        'message', 'track', 'earliest', 'latest', 'observed', 'label',
        'index',
    )

    def __init__(self, message, track=0, earliest=0.0, latest=None,
                 observed=None, label=None, index=None):
        if latest is None:
            latest = earliest
        self.message = message
        self.track = track
        self.earliest = float(earliest)
        self.latest = float(latest)
        self.observed = None if observed is None else float(observed)
        self.label = label
        self.index = index

    def __repr__(self):
        name = self.label if self.label is not None else self.index
        return f'<SerialEvent {name!r} track={self.track!r}>'


class ScheduledEvent:
    """A message placed at a concrete position on the serial line."""

    __slots__ = (
        'index', 'track', 'label', 'message', 'send_time', 'arrival_time',
        'byte_offset', 'bytes', 'wire_bytes', 'running_status',
        'earliest', 'latest', 'observed',
    )

    def __init__(self, event, send_time, arrival_time, byte_offset,
                 wire_bytes, running_status):
        self.index = event.index
        self.track = event.track
        self.label = event.label
        self.message = event.message
        self.send_time = send_time
        self.arrival_time = arrival_time
        self.byte_offset = byte_offset
        self.bytes = len(wire_bytes)
        self.wire_bytes = tuple(wire_bytes)
        self.running_status = running_status
        self.earliest = event.earliest
        self.latest = event.latest
        self.observed = event.observed

    def as_dict(self):
        return {
            'index': self.index,
            'track': self.track,
            'label': self.label,
            'message_type': self.message.type,
            'message_hex': self.message.hex(),
            'send_time': self.send_time,
            'arrival_time': self.arrival_time,
            'byte_offset': self.byte_offset,
            'bytes': self.bytes,
            'wire_bytes': list(self.wire_bytes),
            'running_status': self.running_status,
            'earliest': self.earliest,
            'latest': self.latest,
            'observed': self.observed,
        }


class SerialSchedule:
    """A complete, recomputable serial event table."""

    __slots__ = (
        'events', 'total_bytes', 'finish_time', 'baud_rate',
        'bits_per_byte', 'byte_time', 'start_time',
    )

    def __init__(self, events, baud_rate, bits_per_byte, byte_time,
                 start_time):
        self.events = tuple(events)
        self.total_bytes = sum(event.bytes for event in self.events)
        self.finish_time = (
            self.events[-1].arrival_time if self.events else start_time
        )
        self.baud_rate = baud_rate
        self.bits_per_byte = bits_per_byte
        self.byte_time = byte_time
        self.start_time = start_time

    def __iter__(self):
        return iter(self.events)

    def __len__(self):
        return len(self.events)

    def as_dicts(self):
        return [event.as_dict() for event in self.events]

    def as_dict(self):
        return {
            'baud_rate': self.baud_rate,
            'bits_per_byte': self.bits_per_byte,
            'byte_time': self.byte_time,
            'start_time': self.start_time,
            'finish_time': self.finish_time,
            'total_bytes': self.total_bytes,
            'events': self.as_dicts(),
        }


class ScheduleConflict(ValueError):
    """Raised when one or more events cannot meet their arrival windows."""

    def __init__(self, events, reason, required_seconds=None,
                 available_seconds=None):
        self.events = tuple(events)
        self.indices = tuple(event.index for event in events)
        self.required_seconds = required_seconds
        self.available_seconds = available_seconds
        details = ', '.join(
            str(event.label if event.label is not None else event.index)
            for event in events
        )
        message = f'window conflict for events [{details}]: {reason}'
        super().__init__(message)


def _coerce_event(raw, index):
    if isinstance(raw, SerialEvent):
        event = SerialEvent(
            raw.message,
            track=raw.track,
            earliest=raw.earliest,
            latest=raw.latest,
            observed=raw.observed,
            label=raw.label,
            index=index,
        )
    elif isinstance(raw, Message):
        event = SerialEvent(raw, index=index)
    elif isinstance(raw, dict):
        event = SerialEvent(
            raw['message'],
            track=raw.get('track', 0),
            earliest=raw.get('earliest', 0.0),
            latest=raw.get('latest'),
            observed=raw.get('observed'),
            label=raw.get('label'),
            index=index,
        )
    else:
        raise TypeError('events must be SerialEvent, Message or dict')

    if isinstance(event.message, MetaMessage):
        raise ValueError('meta messages cannot be sent on a serial MIDI line')
    if not isinstance(event.message, Message):
        raise TypeError('event message must be a mido.Message')
    for name in ('earliest', 'latest'):
        value = getattr(event, name)
        if not isfinite(value):
            raise ValueError(f'{name} must be finite')
    if event.earliest > event.latest + _EPSILON:
        raise ValueError('earliest cannot be later than latest')
    if event.observed is not None and not isfinite(event.observed):
        raise ValueError('observed time must be finite')
    return event


def _encode_event(event, previous_status):
    raw = event.message.bytes()
    status = raw[0]
    if status < 0xF0 and status == previous_status:
        return raw[1:], status, True
    if status < 0xF0:
        next_status = status
    elif status >= 0xF8:
        next_status = previous_status
    else:
        next_status = None
    return raw, next_status, False


def _candidate(event, previous_status, byte_time):
    wire_bytes, next_status, running_status = _encode_event(
        event, previous_status,
    )
    duration = len(wire_bytes) * byte_time
    if event.observed is None:
        threshold = event.earliest - duration
    else:
        threshold = event.observed - duration
    return {
        'event': event,
        'wire_bytes': wire_bytes,
        'next_status': next_status,
        'running_status': running_status,
        'duration': duration,
        'threshold': threshold,
    }


def _send_time(event, now, duration):
    if event.observed is None:
        return max(now, event.earliest - duration)
    return event.observed - duration


def _is_available(candidate, now):
    event = candidate['event']
    if event.observed is None:
        return candidate['threshold'] <= now + _EPSILON
    return abs(now - candidate['threshold']) <= _EPSILON


def _ready_time(candidate):
    return candidate['threshold']


def _next_wait(candidates, now):
    future = [
        _ready_time(candidate) for candidate in candidates
        if _ready_time(candidate) > now + _EPSILON
    ]
    if not future:
        return None
    return min(future)


def _candidate_key(candidate, track_order):
    event = candidate['event']
    return (
        event.latest,
        0 if candidate['running_status'] else 1,
        event.earliest,
        track_order[event.track],
        event.index,
    )


def _greedy_order(queues, track_order, byte_time, start_time):
    heads = {track: 0 for track in queues}
    remaining = sum(len(queue) for queue in queues.values())
    now = start_time
    previous_status = None
    records = []

    while remaining:
        candidates = []
        for track, queue in queues.items():
            head = heads[track]
            if head < len(queue):
                candidates.append(_candidate(
                    queue[head], previous_status, byte_time,
                ))

        available = [
            candidate for candidate in candidates
            if _is_available(candidate, now)
        ]
        if not available:
            wait_until = _next_wait(candidates, now)
            if wait_until is None:
                return None, records
            now = wait_until
            continue

        available.sort(key=lambda item: _candidate_key(item, track_order))
        candidate = available[0]
        event = candidate['event']
        send_time = _send_time(event, now, candidate['duration'])
        arrival_time = send_time + candidate['duration']
        record = (event, send_time, arrival_time, candidate['wire_bytes'],
                  candidate['running_status'])
        records.append(record)

        if arrival_time > event.latest + _EPSILON:
            return None, records

        heads[event.track] += 1
        remaining -= 1
        now = arrival_time
        previous_status = candidate['next_status']

    return records, None


def _exact_order(events, byte_time, start_time):
    if len(events) > _EXACT_SEARCH_LIMIT:
        return None

    queues = {}
    track_order = {}
    for event in events:
        if event.track not in queues:
            queues[event.track] = []
            track_order[event.track] = len(track_order)
        queues[event.track].append(event)

    tracks = list(queues)
    frontier = {}
    best_order = None
    best_bytes = None

    def is_dominated(state_key, now, bytes_used):
        for stored_now, stored_bytes in frontier.get(state_key, ()):
            if stored_now <= now + _EPSILON and stored_bytes <= bytes_used:
                return True
        return False

    def remember(state_key, now, bytes_used):
        entries = [
            (stored_now, stored_bytes)
            for stored_now, stored_bytes in frontier.get(state_key, ())
            if not (now <= stored_now + _EPSILON
                    and bytes_used <= stored_bytes)
        ]
        entries.append((now, bytes_used))
        frontier[state_key] = entries

    def search(heads, now, previous_status, order, bytes_used):
        nonlocal best_order, best_bytes

        if all(head == len(queues[track])
               for head, track in zip(heads, tracks)):
            if best_bytes is None or bytes_used < best_bytes:
                best_bytes = bytes_used
                best_order = list(order)
            return

        state_key = (tuple(heads), previous_status)
        if is_dominated(state_key, now, bytes_used):
            return
        remember(state_key, now, bytes_used)

        while True:
            candidates = []
            for position, track in enumerate(tracks):
                head = heads[position]
                if head < len(queues[track]):
                    candidate = _candidate(
                        queues[track][head], previous_status, byte_time,
                    )
                    candidate['position'] = position
                    candidates.append(candidate)

            available = [
                candidate for candidate in candidates
                if _is_available(candidate, now)
            ]
            for candidate in sorted(
                available, key=lambda item: _candidate_key(item, track_order),
            ):
                event = candidate['event']
                send_time = _send_time(event, now, candidate['duration'])
                arrival_time = send_time + candidate['duration']
                if arrival_time > event.latest + _EPSILON:
                    continue
                if arrival_time < now - _EPSILON:
                    continue

                next_heads = list(heads)
                next_heads[candidate['position']] += 1
                order.append((
                    event, send_time, arrival_time,
                    candidate['wire_bytes'], candidate['running_status'],
                ))
                search(
                    tuple(next_heads), arrival_time,
                    candidate['next_status'], order,
                    bytes_used + len(candidate['wire_bytes']),
                )
                order.pop()

            wait_until = _next_wait(candidates, now)
            if wait_until is None:
                return
            now = wait_until

    search(tuple(0 for _ in tracks), start_time, None, [], 0)
    return best_order


def _minimum_wire_seconds(events, byte_time):
    minimum_bytes = 0
    has_channel_message = False
    for event in events:
        raw = event.message.bytes()
        minimum_bytes += len(raw)
        if raw[0] < 0xF0:
            has_channel_message = True
            minimum_bytes -= 1
    if has_channel_message:
        minimum_bytes += 1
    return minimum_bytes * byte_time, minimum_bytes


def _minimal_infeasible_subset(events, byte_time, start_time):
    for size in range(1, len(events) + 1):
        for subset in combinations(events, size):
            subset_events = list(subset)
            if _exact_order(subset_events, byte_time, start_time) is None:
                return subset_events
    return list(events)


def _raise_conflict(events, byte_time, start_time):
    subset = _minimal_infeasible_subset(events, byte_time, start_time)
    required_seconds, required_bytes = _minimum_wire_seconds(
        subset, byte_time,
    )
    available_start = max(start_time, min(event.earliest for event in subset))
    available_seconds = (
        min(event.latest for event in subset) - available_start
    )
    if available_seconds < required_seconds - _EPSILON:
        reason = (
            f'at least {required_bytes} wire bytes require '
            f'{required_seconds:.9f}s, but the shared serial line only has '
            f'{available_seconds:.9f}s'
        )
    else:
        reason = (
            'exhaustive ordering search found no way to satisfy every window'
        )
    raise ScheduleConflict(
        subset, reason, required_seconds, available_seconds,
    )


def _build_schedule(records, events, baud_rate, bits_per_byte, byte_time,
                    start_time):
    scheduled = []
    byte_offset = 0
    by_index = {event.index: event for event in events}

    for event, send_time, arrival_time, wire_bytes, running_status in records:
        scheduled.append(ScheduledEvent(
            by_index[event.index], send_time, arrival_time,
            byte_offset, wire_bytes, running_status,
        ))
        byte_offset += len(wire_bytes)

    return SerialSchedule(
        scheduled, baud_rate, bits_per_byte, byte_time, start_time,
    )


def schedule_events(events, baud_rate=DEFAULT_BAUD_RATE,
                    bits_per_byte=DEFAULT_BITS_PER_BYTE, start_time=0.0):
    """Build a serial event table while preserving order inside each track.

    ``earliest`` and ``latest`` bound the time by which the complete message
    must have arrived. Sending starts at ``start_time`` at the earliest.
    Running status omits a status byte only between adjacent channel messages
    with the same status byte. System common and SysEx messages cancel
    running status; real-time messages may be interleaved without cancelling
    it.
    """
    if baud_rate <= 0 or bits_per_byte <= 0:
        raise ValueError('baud_rate and bits_per_byte must be positive')
    if not isfinite(start_time):
        raise ValueError('start_time must be finite')

    normalized = [
        _coerce_event(event, index)
        for index, event in enumerate(events)
    ]

    for event in normalized:
        if event.observed is not None and not (
            event.earliest - _EPSILON
            <= event.observed
            <= event.latest + _EPSILON
        ):
            raise ScheduleConflict(
                [event],
                'observed time is outside the allowed arrival window',
            )

    byte_time = bits_per_byte / baud_rate
    queues = {}
    track_order = {}
    for event in normalized:
        if event.track not in queues:
            queues[event.track] = []
            track_order[event.track] = len(track_order)
        queues[event.track].append(event)

    if len(normalized) <= _EXACT_SEARCH_LIMIT:
        records = _exact_order(normalized, byte_time, start_time)
        if records is not None:
            return _build_schedule(
                records, normalized, baud_rate, bits_per_byte,
                byte_time, start_time,
            )
        _raise_conflict(normalized, byte_time, start_time)

    records, failure = _greedy_order(
        queues, track_order, byte_time, start_time,
    )
    if failure is None:
        return _build_schedule(
            records, normalized, baud_rate, bits_per_byte,
            byte_time, start_time,
        )

    busy_events = [record[0] for record in failure]
    if not busy_events:
        busy_events = normalized
    if len(busy_events) <= _EXACT_SEARCH_LIMIT:
        if _exact_order(busy_events, byte_time, start_time) is None:
            _raise_conflict(busy_events, byte_time, start_time)

    required_seconds, required_bytes = _minimum_wire_seconds(
        busy_events, byte_time,
    )
    available_start = max(
        start_time, min(event.earliest for event in busy_events),
    )
    available_seconds = (
        min(event.latest for event in busy_events) - available_start
    )
    if available_seconds < required_seconds - _EPSILON:
        reason = (
            f'at least {required_bytes} wire bytes require '
            f'{required_seconds:.9f}s, but only {available_seconds:.9f}s of '
            'shared serial-line time is available'
        )
    else:
        reason = (
            'the earliest-deadline schedule could not satisfy every window; '
            f'the witness contains at least {required_bytes} wire bytes'
        )
    raise ScheduleConflict(
        busy_events, reason, required_seconds, available_seconds,
    )
