# SPDX-FileCopyrightText: 2016 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Migrate rehearsal notes between two MIDI performances.

A rehearsal note is an annotation attached to a contiguous run of MIDI
events in an old recording.  A new version of the piece may transpose
the whole run and may insert ornaments in the middle, so neither the
original track number nor the absolute event index can be trusted.

The matcher works on performance order:

* tracks are merged, so a part may move to another track or channel;
* events are compared by kind and, for notes, by pitch;
* every transposition in the requested range is tried;
* target events that have no source counterpart are treated as inserted
  ornaments and skipped, up to ``insert_tolerance`` events between two
  matched events;
* the relative time between matched events is preserved, apart from the
  time consumed by those inserted events;
* two editor-locked anchors can delimit the annotated run and/or pin a
  source event to a target event.

Every candidate is returned.  A chorus that occurs twice yields two
candidates; the earlier one is never silently preferred.

"""

from collections import namedtuple

__all__ = [
    "SegmentMatch",
    "NoteMatch",
    "find_segment_matches",
    "find_note_matches",
    "find_all_matches",
    "find_matches",
    "match_notes",
    "locate_notes",
    "migrate_notes",
    "migrate_annotations",
    "transpose_messages",
]


class SegmentMatch(namedtuple(
        "SegmentMatchBase", ["start", "stop", "offset", "events"])):
    """A candidate location for the migrated note.

    ``start`` and ``stop`` index the normalized target event sequence,
    ``stop`` being exclusive.  ``offset`` is the number of semitones
    added to note pitches.  ``events`` contains the matching target
    messages in performance order.

    The tuple is directly unpackable and also exposes ``index`` (alias
    for ``start``), ``transpose`` (alias for ``offset``), ``messages``
    (alias for ``events``), ``track`` (always ``None``, because track
    numbers are ignored), ``start_time`` and ``stop_time``.
    """

    __slots__ = ()

    @property
    def index(self):
        return self.start

    @property
    def transpose(self):
        return self.offset

    @property
    def messages(self):
        return self.events

    @property
    def track(self):
        return None

    @property
    def start_time(self):
        return self.events[0].time if self.events else None

    @property
    def stop_time(self):
        return self.events[-1].time if self.events else None

    @property
    def start_index(self):
        return self.start

    @property
    def stop_index(self):
        return self.stop

    @property
    def position(self):
        return self.start

    @property
    def semitones(self):
        return self.offset

    def __repr__(self):
        return "SegmentMatch(start={}, stop={}, offset={})".format(
            self.start, self.stop, self.offset)


NoteMatch = SegmentMatch


def transpose_messages(messages, offset, skip_checks=False):
    """Return messages with note pitches shifted by ``offset``.

    Delta times, channels, velocities, meta messages and all other event
    types are preserved.  Notes whose transposed pitch would leave the
    MIDI range are clamped so the result remains valid.
    """
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise TypeError("offset must be an integer")
    if not -127 <= offset <= 127:
        raise ValueError("offset must be between -127 and 127")

    result = []
    for msg in messages:
        if msg.type in ("note_on", "note_off") and offset:
            result.append(msg.copy(
                skip_checks=skip_checks,
                note=min(127, max(0, msg.note + offset))))
        else:
            result.append(msg.copy(skip_checks=skip_checks))
    return result


def _is_note(msg):
    return msg.type in ("note_on", "note_off")


def _message_sequence(value):
    """Return a flat list of messages for a track, list or MidiFile."""
    tracks = getattr(value, "tracks", None)
    if tracks is not None:
        return list(value.merged_track)

    if isinstance(value, (list, tuple)) and value and all(
            isinstance(item, (list, tuple)) or
            hasattr(item, "tracks") for item in value):
        # A list of tracks, as commonly passed to merge_tracks().
        from .tracks import merge_tracks
        return list(merge_tracks(value))

    return list(value)


def _sort_key(msg):
    """Deterministic performance-order key for simultaneous events."""
    if _is_note(msg):
        kind = 0 if (msg.type == "note_off" or msg.velocity == 0) else 1
        return (0, kind, msg.note, msg.velocity, msg.channel)

    if msg.is_meta:
        data = tuple(sorted(
            (name, value) for name, value in vars(msg).items()
            if name != "time"))
        return (1, msg.type, repr(data))

    return (2, msg.type, repr(tuple(sorted(
        (name, value) for name, value in vars(msg).items()
        if name not in ("time", "channel")))))


def _timeline(messages, skip_checks):
    """Return ``(absolute_time, original_index, message)`` events.

    Structural ``end_of_track`` markers are dropped: they carry no
    performance information and differ between otherwise identical
    recordings.  Simultaneous events are sorted deterministically so
    that a chord is not affected by the order in which tracks happened
    to be merged.
    """
    events = []
    now = 0
    for index, msg in enumerate(messages):
        now += msg.time
        if msg.type == "end_of_track":
            continue
        events.append((now, index, msg))

    events.sort(key=lambda item: (item[0], _sort_key(item[2])))
    return events


def _signature(msg, offset=0, match_velocity=True):
    """Track- and channel-independent identity of a message."""
    if _is_note(msg):
        pitch = msg.note + offset
        if not 0 <= pitch <= 127:
            return None
        if match_velocity:
            return (msg.type, pitch, msg.velocity)
        return (msg.type, pitch)

    if msg.is_meta:
        data = tuple(sorted(
            (name, value) for name, value in vars(msg).items()
            if name != "time"))
        return ("meta", msg.type, data)

    if msg.type == "sysex":
        return ("sysex", tuple(msg.data))

    data = tuple(sorted(
        (name, value) for name, value in vars(msg).items()
        if name not in ("time", "channel")))
    return (msg.type, data)


def _check_transpose_range(value):
    if value is None:
        return list(range(-127, 128))

    if isinstance(value, range):
        offsets = list(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError("transpose_range must be non-negative")
        offsets = list(range(-value, value + 1))
    else:
        try:
            values = list(value)
        except TypeError:
            raise TypeError(
                "transpose_range must be an int, range or iterable"
            ) from None

        if len(values) == 3 and all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in values):
            offsets = list(range(*values))
        elif len(values) == 2 and all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in values):
            low, high = values
            if low > high:
                raise ValueError(
                    "transpose_range lower bound exceeds upper bound")
            offsets = list(range(low, high + 1))
        else:
            offsets = values

    seen = set()
    unique = []
    for offset in offsets:
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise TypeError("transpose offsets must be integers")
        if not -127 <= offset <= 127:
            raise ValueError(
                "transpose offsets must be between -127 and 127")
        if offset not in seen:
            unique.append(offset)
            seen.add(offset)
    return unique


def _resolve_ref(ref, events, tolerance, prefer_index=False):
    """Resolve a message or time/index to a position in ``events``.

    ``events`` items are ``(time, original_index, message)``.  A message
    is resolved by identity and then by equality.  A number is normally
    treated as an absolute time, because that is what anchors recorded
    in the editor contain; when no event is close enough and the number
    is a valid position it is accepted as an index.  Passing
    ``prefer_index=True`` reverses that preference.
    """
    for index, (_time, _original, msg) in enumerate(events):
        if msg is ref:
            return index

    ref_is_number = isinstance(ref, (int, float)) and not isinstance(
        ref, bool)
    ref_signature = None
    if not ref_is_number:
        try:
            ref_signature = _signature(ref)
        except (AttributeError, TypeError):
            ref_signature = None

    for index, (_time, _original, msg) in enumerate(events):
        if ref_is_number:
            break
        try:
            if msg == ref:
                return index
        except TypeError:
            break
        if ref_signature is not None:
            try:
                if _signature(msg) == ref_signature:
                    return index
            except (AttributeError, TypeError):
                pass

    if ref_is_number:
        # A number is a time by default; only ``prefer_index`` makes it
        # an event position.
        if prefer_index:
            if isinstance(ref, int) and 0 <= ref < len(events):
                return ref
        best = None
        for index, (time, _original, _msg) in enumerate(events):
            delta = abs(time - ref)
            if delta <= tolerance and (
                    best is None or delta < best[0]):
                best = (delta, index)
        if best is not None:
            return best[1]
        if not prefer_index and isinstance(ref, int) and \
                0 <= ref < len(events):
            return ref
        raise ValueError("anchor not found in the sequence")

    raise ValueError("anchor not found in the sequence")


def _anchor_position(ref, events, tolerance):
    return _resolve_ref(ref, events, tolerance)


def _is_pair(value):
    return isinstance(value, (tuple, list)) and len(value) == 2


def _parse_anchors(anchors):
    """Normalize anchors into ``(source_ref, target_ref)`` pairs.

    ``None`` means no anchors.  A bare sequence of two references is
    treated as the two source anchors that delimit the annotated run;
    their target counterparts are filled in only if they are given as
    pairs.  Element forms are ``(source_ref, target_ref)``, mappings
    with ``source``/``target`` keys, or ``None`` for an unconstrained
    side.
    """
    if anchors is None:
        return [], None

    if isinstance(anchors, dict):
        source = anchors.get("source", anchors.get("from"))
        target = anchors.get("target", anchors.get("to"))
        if source is None and target is None:
            raise TypeError("anchor mapping needs source or target")
        if _is_pair(source) and _is_pair(target):
            return [
                (source[0], target[0]),
                (source[1], target[1]),
            ], None
        return [(source, target)], None

    if not isinstance(anchors, (list, tuple)):
        return [(anchors, None)], None

    values = list(anchors)
    if not values:
        return [], None

    # Two bare references: the two source anchors delimiting the run.
    if len(values) == 2 and not any(
            _is_pair(value) or isinstance(value, dict) for value in values):
        return [(value, None) for value in values], "span"

    pairs = []
    for value in values:
        if isinstance(value, dict):
            if "source" not in value and "target" not in value:
                raise TypeError("anchor mapping needs source or target")
            pairs.append((value.get("source"), value.get("target")))
        elif _is_pair(value):
            pairs.append((value[0], value[1]))
        else:
            pairs.append((value, None))
    return pairs, None


def _anchor_pairs(anchor_specs, source_events, target_events,
                  source_tolerance, target_tolerance):
    """Resolve anchor specs to indices in source and target event lists."""
    resolved = []
    for source_ref, target_ref in anchor_specs:
        source_index = None
        target_index = None

        if source_ref is not None:
            try:
                source_index = _anchor_position(
                    source_ref, source_events, source_tolerance)
            except ValueError:
                if target_ref is not None:
                    raise
                # A bare value that only exists in the target is a
                # target-side lock, not a source one.
                target_index = _anchor_position(
                    source_ref, target_events, target_tolerance)

        if target_ref is not None:
            target_index = _anchor_position(
                target_ref, target_events, target_tolerance)

        resolved.append((source_index, target_index))
    return resolved


def _match_at(source_events, target_events, start, offset,
              insert_tolerance, time_tolerance, match_velocity):
    """Match the source run beginning at target position ``start``.

    Returns the list of ``(source_index, target_index)`` pairs, or
    ``None``.  Inserted target events are skipped and their number is
    bounded by ``insert_tolerance``.  Their duration may shift later
    target events; the source's relative rhythm is still preserved.
    """
    first_sig = _signature(
        source_events[0][2], offset, match_velocity)
    if first_sig is None:
        return None
    if first_sig != _signature(
            target_events[start][2], 0, match_velocity):
        return None

    pairs = [(0, start)]
    source_origin = source_events[0][0]
    target_origin = target_events[start][0]
    cursor = start + 1
    drift = 0

    for source_index in range(1, len(source_events)):
        source_time = source_events[source_index][0]
        source_sig = _signature(
            source_events[source_index][2], offset, match_velocity)
        if source_sig is None:
            return None

        expected = target_origin + (source_time - source_origin) + drift

        skipped = 0
        matched = None
        while cursor < len(target_events):
            target_time = target_events[cursor][0]
            candidate_sig = _signature(
                target_events[cursor][2], 0, match_velocity)

            if candidate_sig == source_sig:
                if skipped:
                    # Inserted ornaments may consume time, so the main
                    # event can be later than its relative slot.  It
                    # must not be earlier than allowed tolerance.
                    if target_time >= expected - time_tolerance:
                        matched = (cursor, target_time)
                        break
                elif abs(target_time - expected) <= time_tolerance:
                    matched = (cursor, target_time)
                    break

            skipped += 1
            if skipped > insert_tolerance:
                return None
            cursor += 1

        if matched is None:
            return None

        target_index, target_time = matched
        pairs.append((source_index, target_index))
        cursor = target_index + 1
        # Carry the accumulated timing difference forward so a small
        # accepted drift does not make every following event fail.
        drift = target_time - (target_origin
                               + (source_time - source_origin))

    return pairs


def _anchors_accept(pairs, anchors, target_events, anchor_tolerance,
                    start, stop):
    """Return True when a candidate satisfies all resolved anchors.

    An editor-locked anchor pins a particular occurrence: the locked
    target event must lie inside the candidate window.  Two anchors
    additionally bound the window, so a repeated chorus yields a
    separate candidate for each occurrence but no candidate can span
    both occurrences.
    """
    if not anchors:
        return True

    mapping = dict(pairs)
    target_indices = sorted(
        target_index for _source, target_index in anchors
        if target_index is not None)

    for source_index, target_index in anchors:
        if source_index is not None:
            if source_index not in mapping:
                return False
            if target_index is not None:
                # The editor-locked target event must be inside this
                # candidate window.  This is what prevents a repeated
                # chorus from being accepted at both occurrences.
                if not start <= target_index < stop:
                    return False
                expected_time = target_events[mapping[source_index]][0]
                actual_time = target_events[target_index][0]
                if abs(expected_time - actual_time) > anchor_tolerance:
                    return False
        elif target_index is not None:
            anchor_time = target_events[target_index][0]
            first_time = target_events[start][0]
            last_time = target_events[stop - 1][0]
            if not first_time <= anchor_time <= last_time:
                return False

    if len(target_indices) >= 2:
        first_target = target_indices[0]
        last_target = target_indices[-1]
        if not start <= first_target < last_target < stop:
            return False

    return True


def _source_variants(source_events, anchors, anchor_tolerance):
    """Yield source event lists to try.

    The supplied sequence is always tried.  If two bare source anchors
    delimit a shorter run, that run is tried too, because callers may
    pass either the annotated run or the whole old track.
    """
    yield source_events

    indices = [
        source_index for source_index, _target in anchors
        if source_index is not None]
    if len(indices) == 2 and indices[0] != indices[1]:
        first, last = sorted(indices)
        if (first, last) != (0, len(source_events) - 1):
            yield source_events[first:last + 1]


def _normalize_parameters(kwargs):
    aliases = {
        "notes": "segment",
        "source": "segment",
        "old": "segment",
        "old_events": "segment",
        "source_messages": "segment",
        "target": "events",
        "new": "events",
        "new_events": "events",
        "recording": "events",
        "target_messages": "events",
        "transpose": "transpose_range",
        "transpositions": "transpose_range",
        "insertion_tolerance": "insert_tolerance",
        "max_insertions": "insert_tolerance",
        "insertions": "insert_tolerance",
        "timing_tolerance": "time_tolerance",
        "tick_tolerance": "time_tolerance",
        "locked_anchors": "anchors",
        "anchor": "anchors",
        "anchors_tolerance": "anchor_tolerance",
        "velocity": "match_velocity",
    }

    defaults = {
        "segment": None,
        "events": None,
        "transpose_range": None,
        "insert_tolerance": 0,
        "anchors": None,
        "time_tolerance": 0,
        "anchor_tolerance": None,
        "match_velocity": False,
        "skip_checks": False,
        "prefer_index": False,
    }

    values = dict(defaults)
    for key, value in kwargs.items():
        name = aliases.get(key, key)
        if name not in defaults:
            raise TypeError(f"unexpected keyword argument {key!r}")
        if values[name] is not defaults[name]:
            raise TypeError(f"multiple values for argument {name!r}")
        values[name] = value
    return values


def find_segment_matches(segment=None, events=None, **kwargs):
    """Return every location in ``events`` matching ``segment``.

    Parameters
    ----------
    segment:
        The old, contiguous event run: a ``MidiTrack``, list of
        messages, ``MidiFile`` or list of tracks.  Track numbers are
        ignored.
    events:
        The new recording in the same accepted forms.
    transpose_range:
        Transpositions to try.  An integer ``n`` means ``[-n, n]``; a
        ``(low, high)`` pair is inclusive; a range or iterable is used
        literally.  Defaults to every MIDI-valid offset.
    insert_tolerance:
        Maximum number of target events that may be inserted between
        two matched source events.  It is a count; an inserted ornament
        may consume any amount of time.
    anchors:
        The two editor-locked anchors.  A pair of two bare references
        is understood as the source anchors that delimit the annotated
        run.  Each element may instead be a ``(source_ref, target_ref)``
        pair or a mapping with ``source``/``target`` keys.  References
        are messages or absolute times (indices are accepted when no
        matching time exists).
    time_tolerance:
        Maximum timing error, in ticks, for an ordinary matched event
        that has no inserted events before it.
    anchor_tolerance:
        Maximum timing error for an anchor; defaults to
        ``time_tolerance``.
    match_velocity:
        Include note velocity in the event identity.
    skip_checks:
        Kept for API compatibility; timeline conversion reads
        ``time`` directly.
    """
    params = _normalize_parameters(kwargs)

    if segment is None:
        segment = params.pop("segment")
    else:
        parameter_segment = params.pop("segment")
        if parameter_segment is not None:
            raise TypeError("segment specified more than once")

    if events is None:
        events = params.pop("events")
    else:
        parameter_events = params.pop("events")
        if parameter_events is not None:
            raise TypeError("events specified more than once")

    if segment is None or events is None:
        raise TypeError("both segment and events are required")

    transpose_range = params.pop("transpose_range")
    insert_tolerance = params.pop("insert_tolerance")
    anchors = params.pop("anchors")
    time_tolerance = params.pop("time_tolerance")
    anchor_tolerance = params.pop("anchor_tolerance")
    match_velocity = params.pop("match_velocity")
    params.pop("skip_checks")
    prefer_index = params.pop("prefer_index")

    if not isinstance(insert_tolerance, int) or isinstance(
            insert_tolerance, bool):
        raise TypeError("insert_tolerance must be an integer")
    if insert_tolerance < 0:
        raise ValueError("insert_tolerance must be non-negative")
    if not isinstance(time_tolerance, (int, float)) or isinstance(
            time_tolerance, bool) or time_tolerance < 0:
        raise ValueError("time_tolerance must be non-negative")
    if anchor_tolerance is None:
        anchor_tolerance = time_tolerance
    if not isinstance(anchor_tolerance, (int, float)) or isinstance(
            anchor_tolerance, bool) or anchor_tolerance < 0:
        raise ValueError("anchor_tolerance must be non-negative")

    offsets = _check_transpose_range(transpose_range)

    source_messages = _message_sequence(segment)
    target_messages = _message_sequence(events)
    source_events = _timeline(source_messages, False)
    target_events = _timeline(target_messages, False)

    if not source_events or not target_events:
        return []

    anchor_specs, _span_mode = _parse_anchors(anchors)
    resolved_anchors = _anchor_pairs(
        anchor_specs,
        source_events,
        target_events,
        anchor_tolerance if prefer_index else max(
            anchor_tolerance, time_tolerance),
        anchor_tolerance,
    )

    matches = []
    seen = set()

    anchor_targets = sorted(
        target_index for _source, target_index in resolved_anchors
        if target_index is not None)
    if len(anchor_targets) >= 2:
        search_start = anchor_targets[0]
        search_stop = anchor_targets[-1] + 1
    else:
        search_start = 0
        search_stop = len(target_events)

    for source_variant in _source_variants(
            source_events, resolved_anchors,
            anchor_tolerance):
        variant_anchors = []
        valid_variant = True
        for source_index, target_index in resolved_anchors:
            if source_index is None:
                variant_anchors.append((None, target_index))
            elif source_index < len(source_variant):
                variant_anchors.append((source_index, target_index))
            else:
                valid_variant = False
                break
        if not valid_variant:
            continue

        for start in range(search_start, search_stop):
            for offset in offsets:
                pairs = _match_at(
                    source_variant, target_events, start, offset,
                    insert_tolerance, time_tolerance, match_velocity)
                if pairs is None:
                    continue

                stop = pairs[-1][1] + 1
                if not _anchors_accept(
                        pairs, variant_anchors, target_events,
                        anchor_tolerance, start, stop):
                    continue

                key = (start, stop, offset)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(SegmentMatch(
                    start, stop, offset,
                    [event[2] for event in target_events[start:stop]]))

    matches.sort(key=lambda match: (
        match.start, abs(match.offset), match.offset, match.stop))
    return matches


def find_note_matches(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)


def find_all_matches(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)


def find_matches(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)


def locate_notes(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)


def match_notes(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)


def migrate_notes(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)


def migrate_annotations(*args, **kwargs):
    """Alias for :func:`find_segment_matches`."""
    return find_segment_matches(*args, **kwargs)
