'''
Module for segmenting ULog datasets by physical device identity.

When a bench acquisition process restarts, it may re-subscribe to topics
starting from instance 0, so the same physical sensor can reappear under a
different multi-instance id, and two different sensors can end up merged
under a single (topic, instance) pair. This module segments each dataset
into stretches that belong to a single physical device, combining:

- the topic (dataset name) and instance (multi id),
- known device serial numbers (a DeviceRegistry),
- observed serial numbers in the data (e.g. a "device_id" field),
- replacement records logged by the bench operators.

Every segment keeps the rationale that led to its creation. A short dropout
of the data stream alone is never sufficient to declare a device
replacement. Segments whose device cannot be confirmed carry a list of
candidate devices instead, and single-device drift must not be computed for
them (compute_segment_drift refuses).
'''

import numpy as np

#: Gaps longer than this (in microseconds) are mentioned in the segment
#: rationale. A gap alone, no matter how long, never splits a segment: a
#: brief dropout of the acquisition process is not sufficient evidence to
#: declare a device replacement.
DEFAULT_GAP_THRESHOLD_US = 5000000

#: Default dataset field carrying the device serial number.
DEFAULT_SERIAL_FIELD = 'device_id'


def _normalize_serial(value):
    '''
    Normalize a raw serial value to a string, or None if it means "unknown".
    '''
    if value is None:
        return None
    if isinstance(value, str):
        return value if value else None
    value = int(value)
    return str(value) if value != 0 else None


class DeviceReplacement:
    '''
    A record of a physical device replacement on the test bench.

    "timestamp" is in microseconds since epoch, matching the ULog
    timestamps. "instance" may be None, meaning the record applies to the
    topic regardless of which instance the device shows up on.
    '''
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(self, timestamp, topic, new_serial, old_serial=None,
                 instance=None, note=''):
        self.timestamp = int(timestamp)
        self.topic = topic
        self.new_serial = _normalize_serial(new_serial)
        self.old_serial = _normalize_serial(old_serial)
        self.instance = instance
        self.note = note

    def __eq__(self, other):
        if not isinstance(other, DeviceReplacement):
            return NotImplemented
        return self.__dict__ == other.__dict__

    def __repr__(self):
        return (f'DeviceReplacement(timestamp={self.timestamp}, topic={self.topic!r}, '
                f'new_serial={self.new_serial!r}, old_serial={self.old_serial!r}, '
                f'instance={self.instance!r}, note={self.note!r})')


class DeviceRegistry:
    '''
    Known device serial numbers and replacement records.

    The registry represents bench knowledge that exists outside of any
    single log: which devices (serial numbers) exist for which topic, and
    when devices were physically swapped.
    '''

    def __init__(self):
        self.devices = {}  # serial -> {'topic': topic or None, 'notes': str}
        self.replacements = []  # list of DeviceReplacement

    def register_device(self, serial, topic=None, notes=''):
        '''
        Register a known device serial number, optionally associated with a
        topic it is usually attached to.
        '''
        serial = _normalize_serial(serial)
        if serial is None:
            raise ValueError('Cannot register an empty serial number.')
        self.devices[serial] = {'topic': topic, 'notes': notes}

    def add_replacement(self, timestamp, topic, new_serial, old_serial=None,
                        instance=None, note=''):
        '''
        Add a replacement record: at "timestamp", the device on "topic"
        (optionally restricted to "instance") was replaced by the device
        with serial "new_serial".
        '''
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        record = DeviceReplacement(timestamp, topic, new_serial,
                                   old_serial=old_serial, instance=instance,
                                   note=note)
        self.replacements.append(record)
        return record

    def serials_for_topic(self, topic):
        '''
        Return the sorted list of known serial numbers registered for the
        given topic. Devices registered without a topic are included for
        every topic, since they cannot be ruled out.
        '''
        return sorted(serial for serial, info in self.devices.items()
                      if info['topic'] in (None, topic))

    def replacements_for(self, topic, instance):
        '''
        Return the replacement records applying to the given topic and
        instance, sorted by timestamp.
        '''
        records = [record for record in self.replacements
                   if record.topic == topic
                   and (record.instance is None or record.instance == instance)]
        return sorted(records, key=lambda record: record.timestamp)


class DeviceSegment:
    '''
    A contiguous time range of one dataset attributed to a single device.

    If "confirmed" is True, "serial" holds the confirmed device serial
    number. Otherwise "serial" is None and "candidates" lists the devices
    that might correspond to this segment given the available records.
    "rationale" is a list of human-readable strings documenting the
    evidence that led to this segmentation.
    '''
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(self, topic, instance, start_timestamp, end_timestamp,
                 serial=None, confirmed=False, candidates=None, rationale=None):
        self.topic = topic
        self.instance = instance
        self.start_timestamp = int(start_timestamp)
        self.end_timestamp = int(end_timestamp)
        self.serial = serial
        self.confirmed = confirmed
        self.candidates = list(candidates) if candidates else []
        self.rationale = list(rationale) if rationale else []

    def add_rationale(self, message):
        '''
        Append a human-readable justification for this segment.
        '''
        self.rationale.append(message)

    def __eq__(self, other):
        if not isinstance(other, DeviceSegment):
            return NotImplemented
        return self.__dict__ == other.__dict__

    def __repr__(self):
        return (f'DeviceSegment(topic={self.topic!r}, instance={self.instance!r}, '
                f'start={self.start_timestamp}, end={self.end_timestamp}, '
                f'serial={self.serial!r}, confirmed={self.confirmed!r}, '
                f'candidates={self.candidates!r})')


def _find_gaps(timestamps, lo, hi, gap_threshold_us):
    '''
    Return a list of (index, duration) of gaps within timestamps[lo:hi] that
    exceed gap_threshold_us. The index refers to the sample after the gap.
    '''
    gaps = []
    for i in range(lo + 1, hi):
        duration = int(timestamps[i]) - int(timestamps[i - 1])
        if duration > gap_threshold_us:
            gaps.append((i, duration))
    return gaps


# pylint: disable=too-many-locals,too-many-branches
def segment_dataset(dataset, registry=None, gap_threshold_us=DEFAULT_GAP_THRESHOLD_US,
                    serial_field=DEFAULT_SERIAL_FIELD):
    '''
    Segment a single dataset (one topic and instance) into device segments.

    Splits happen where the observed serial number changes, and at
    replacement records from the registry. Data gaps alone never cause a
    split; they are only recorded in the rationale.
    '''
    timestamps = np.asarray(dataset.data['timestamp'])
    if timestamps.size == 0:
        return []
    name = dataset.name
    instance = dataset.multi_id

    serials = None
    if serial_field in dataset.data:
        serials = [_normalize_serial(value) for value in dataset.data[serial_field]]

    records = registry.replacements_for(name, instance) if registry is not None else []

    # Split where the serial changes or a replacement record applies.
    splits = {}  # sample index -> reason
    if serials is not None:
        for i in range(1, len(serials)):
            if serials[i] != serials[i - 1]:
                gap = int(timestamps[i]) - int(timestamps[i - 1])
                reason = (f'serial changed from {serials[i - 1] or "unknown"} '
                          f'to {serials[i] or "unknown"}')
                if gap > gap_threshold_us:
                    reason += f' after a {gap / 1e6:.1f}s dropout'
                splits[i] = reason
    # Maps the segment start index to the replacement record that applies to
    # it (records before the first sample apply to the first segment).
    record_at_start = {}
    for record in records:
        idx = int(np.searchsorted(timestamps, record.timestamp))
        if idx >= len(timestamps):
            continue  # The record is after the end of the log.
        if idx > 0:
            splits.setdefault(idx, 'replacement record: device '
                                   f'{record.new_serial or "unknown"} installed')
        record_at_start[idx] = record

    bounds = [0] + sorted(splits) + [len(timestamps)]
    segments = []
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        segment = DeviceSegment(topic=name, instance=instance,
                                start_timestamp=timestamps[lo],
                                end_timestamp=timestamps[hi - 1])
        if lo in splits:
            segment.add_rationale('segment start: ' + splits[lo])

        if serials is None:
            segment.add_rationale(
                f"dataset has no '{serial_field}' field; serial not observable")
            observed = []
            unknown_count = 0
        else:
            observed = sorted({s for s in serials[lo:hi] if s is not None})
            unknown_count = (hi - lo) - sum(1 for s in serials[lo:hi] if s is not None)

        if len(observed) == 1:
            segment.serial = observed[0]
            segment.confirmed = True
            segment.add_rationale(
                f"serial {observed[0]} observed on topic '{name}' instance {instance}")
            if unknown_count:
                segment.add_rationale(
                    f'serial unknown for {unknown_count} sample(s) within the segment')
        elif len(observed) > 1:
            # Should not happen since we split on serial changes, but stay
            # conservative: never confirm a segment with mixed serials.
            segment.candidates = observed
            segment.add_rationale(f'multiple serials observed: {observed}')

        for gap_index, duration in _find_gaps(timestamps, lo, hi, gap_threshold_us):
            segment.add_rationale(
                f'dropout of {duration / 1e6:.1f}s at t={int(timestamps[gap_index])}: '
                'insufficient alone to declare a replacement')

        if lo in record_at_start:
            record = record_at_start[lo]
            note = f' ({record.note})' if record.note else ''
            segment.add_rationale(
                f'replacement record at t={record.timestamp}: device '
                f'{record.new_serial or "unknown"} installed{note}')
            if not segment.confirmed and record.new_serial is not None:
                segment.candidates.insert(0, record.new_serial)

        if not segment.confirmed and registry is not None:
            for serial in registry.serials_for_topic(name):
                if serial not in segment.candidates:
                    segment.candidates.append(serial)
            if segment.candidates:
                segment.add_rationale(
                    f'possible devices from registry: {segment.candidates}')
            else:
                segment.add_rationale('no known devices registered for this topic')

        segments.append(segment)
    return segments


def segment_ulog(ulog, registry=None, gap_threshold_us=DEFAULT_GAP_THRESHOLD_US,
                 serial_field=DEFAULT_SERIAL_FIELD):
    '''
    Segment all datasets of a ULog by device identity.

    Devices are recognized across instances: if the same serial shows up on
    several instances (e.g. because the acquisition process restarted and
    re-subscribed from instance 0), the segments are recognized as belonging
    to the same device and say so in their rationale.

    Unconfirmed segments get their candidate lists refined: serials that are
    confirmed on another segment of the same topic overlapping in time are
    excluded.
    '''
    segments = []
    for dataset in ulog.data_list:
        segments.extend(segment_dataset(dataset, registry=registry,
                                        gap_threshold_us=gap_threshold_us,
                                        serial_field=serial_field))

    instances_by_serial = {}
    for segment in segments:
        if segment.confirmed:
            instances_by_serial.setdefault(segment.serial, set()).add(segment.instance)
    for segment in segments:
        if segment.confirmed and len(instances_by_serial[segment.serial]) > 1:
            others = sorted(instances_by_serial[segment.serial] - {segment.instance})
            segment.add_rationale(
                f'serial {segment.serial} also observed on instance(s) {others}; '
                'recognized as the same device')

    for segment in segments:
        if segment.confirmed or not segment.candidates:
            continue
        accounted = {other.serial for other in segments
                     if other.confirmed and other.topic == segment.topic
                     and other.start_timestamp <= segment.end_timestamp
                     and other.end_timestamp >= segment.start_timestamp}
        excluded = [c for c in segment.candidates if c in accounted]
        if excluded:
            segment.candidates = [c for c in segment.candidates if c not in accounted]
            segment.add_rationale(
                f'excluded {excluded}: confirmed on another segment overlapping in time')
    return segments


def compute_segment_drift(dataset, segment, field):
    '''
    Compute the single-device drift of "field" over the given segment as a
    linear fit, returning a dict with the drift rate per second.

    Raises a ValueError for unconfirmed segments: computing a single-sensor
    drift over a segment whose device is not confirmed would silently mix
    several devices.
    '''
    if not segment.confirmed:
        candidates = segment.candidates if segment.candidates else 'none known'
        raise ValueError(
            f"Refusing to compute single-device drift for unconfirmed segment of "
            f"'{segment.topic}' instance {segment.instance} "
            f'(t={segment.start_timestamp}..{segment.end_timestamp}); '
            f'candidate devices: {candidates}')

    timestamps = np.asarray(dataset.data['timestamp'], dtype=np.int64)
    values = np.asarray(dataset.data[field], dtype=np.float64)
    mask = (timestamps >= segment.start_timestamp) & (timestamps <= segment.end_timestamp)
    t_sel = timestamps[mask].astype(np.float64) / 1e6
    x_sel = values[mask]
    if t_sel.size < 2:
        raise ValueError(
            f'Not enough samples ({t_sel.size}) to compute drift for segment of '
            f"'{segment.topic}' instance {segment.instance}.")

    slope, intercept = np.polyfit(t_sel - t_sel[0], x_sel, 1)
    return {
        'serial': segment.serial,
        'topic': segment.topic,
        'instance': segment.instance,
        'field': field,
        'start_timestamp': segment.start_timestamp,
        'end_timestamp': segment.end_timestamp,
        'samples': int(t_sel.size),
        'drift_per_s': float(slope),
        'offset': float(intercept),
    }
