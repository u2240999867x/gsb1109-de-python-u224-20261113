'''
Test the device_segments module and its integration into DatabaseULog.
'''

import unittest
import os
import numpy as np

from pyulog.db import DatabaseULog
from pyulog.migrate_db import migrate_db
from pyulog.device_segments import (
    DeviceRegistry,
    DeviceSegment,
    segment_dataset,
    segment_ulog,
    compute_segment_drift,
)

TEST_PATH = os.path.dirname(os.path.abspath(__file__))


class FakeDataset:
    '''Minimal stand-in for ULog.Data, providing name, multi_id and data.'''

    def __init__(self, name, multi_id, data):
        self.name = name
        self.multi_id = multi_id
        self.data = data


class FakeULog:
    '''Minimal stand-in for ULog, providing data_list.'''

    def __init__(self, data_list):
        self.data_list = data_list


def _timestamps(*values_s):
    '''Timestamps in microseconds from values in seconds.'''
    return np.array([int(v * 1e6) for v in values_s], dtype=np.uint64)


class TestSegmentDataset(unittest.TestCase):
    '''
    Test the segmentation of a single dataset.
    '''

    def test_serial_change_splits(self):
        '''A change of the observed serial splits the dataset.'''
        dataset = FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 3, 4),
            'device_id': np.array([111, 111, 222, 222]),
        })
        segments = segment_dataset(dataset)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].serial, '111')
        self.assertEqual(segments[1].serial, '222')
        self.assertTrue(all(segment.confirmed for segment in segments))
        self.assertEqual(segments[0].start_timestamp, 1000000)
        self.assertEqual(segments[0].end_timestamp, 2000000)
        self.assertEqual(segments[1].start_timestamp, 3000000)
        self.assertTrue(any('serial changed' in reason
                            for reason in segments[1].rationale))

    def test_short_dropout_does_not_split(self):
        '''A brief dropout alone is not enough to declare a replacement.'''
        dataset = FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 10, 11),
            'device_id': np.array([111, 111, 111, 111]),
        })
        segments = segment_dataset(dataset, gap_threshold_us=5000000)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].serial, '111')
        self.assertTrue(any('dropout' in reason and 'insufficient' in reason
                            for reason in segments[0].rationale))

    def test_serial_change_after_dropout(self):
        '''A serial change after a dropout splits and keeps the rationale.'''
        dataset = FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 10, 11),
            'device_id': np.array([111, 111, 222, 222]),
        })
        segments = segment_dataset(dataset, gap_threshold_us=5000000)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1].serial, '222')
        self.assertTrue(any('dropout' in reason
                            for reason in segments[1].rationale))

    def test_replacement_record_splits_without_serial(self):
        '''A replacement record splits even if no serial field exists.'''
        registry = DeviceRegistry()
        registry.register_device('aaa', topic='sensor_accel')
        registry.register_device('bbb', topic='sensor_accel')
        registry.add_replacement(5 * 1000000, 'sensor_accel', 'bbb',
                                 old_serial='aaa', note='swap on bench')
        dataset = FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 6, 7),
        })
        segments = segment_dataset(dataset, registry=registry)
        self.assertEqual(len(segments), 2)
        self.assertFalse(segments[0].confirmed)
        self.assertFalse(segments[1].confirmed)
        self.assertEqual(segments[1].candidates[0], 'bbb')
        self.assertTrue(any('replacement record' in reason
                            for reason in segments[1].rationale))

    def test_unconfirmed_segment_lists_candidates(self):
        '''Without serial data, possible devices are listed as candidates.'''
        registry = DeviceRegistry()
        registry.register_device('aaa', topic='sensor_accel')
        registry.register_device('bbb', topic='sensor_accel')
        registry.register_device('ccc', topic='sensor_gyro')
        dataset = FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 3),
        })
        segments = segment_dataset(dataset, registry=registry)
        self.assertEqual(len(segments), 1)
        self.assertFalse(segments[0].confirmed)
        self.assertIsNone(segments[0].serial)
        self.assertEqual(segments[0].candidates, ['aaa', 'bbb'])

    def test_replacement_record_for_other_instance_ignored(self):
        '''Records restricted to another instance do not cause a split.'''
        registry = DeviceRegistry()
        registry.add_replacement(5 * 1000000, 'sensor_accel', 'bbb', instance=1)
        dataset = FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 6, 7),
            'device_id': np.array([111, 111, 111, 111]),
        })
        segments = segment_dataset(dataset, registry=registry)
        self.assertEqual(len(segments), 1)


class TestSegmentULog(unittest.TestCase):
    '''
    Test log-level segmentation across instances.
    '''

    def test_device_recognized_on_other_instance(self):
        '''The same serial on another instance is the same device.'''
        ulog = FakeULog([
            FakeDataset('sensor_accel', 0, {
                'timestamp': _timestamps(1, 2),
                'device_id': np.array([111, 111]),
            }),
            FakeDataset('sensor_accel', 1, {
                'timestamp': _timestamps(3, 4),
                'device_id': np.array([111, 111]),
            }),
        ])
        segments = segment_ulog(ulog)
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(segment.confirmed for segment in segments))
        self.assertEqual({segment.serial for segment in segments}, {'111'})
        for segment in segments:
            self.assertTrue(any('recognized as the same device' in reason
                                for reason in segment.rationale))

    def test_restart_resubscribe_from_instance_zero(self):
        '''A bench restart re-subscribing from instance 0 does not merge devices.'''
        ulog = FakeULog([
            FakeDataset('sensor_accel', 0, {
                'timestamp': _timestamps(1, 2),
                'device_id': np.array([111, 111]),
            }),
            # After the restart, instance 0 carries the other device.
            FakeDataset('sensor_accel', 0, {
                'timestamp': _timestamps(3, 4),
                'device_id': np.array([222, 222]),
            }),
        ])
        segments = segment_ulog(ulog)
        self.assertEqual(len(segments), 2)
        self.assertEqual({segment.serial for segment in segments}, {'111', '222'})

    def test_candidates_exclude_accounted_devices(self):
        '''Candidates of unconfirmed segments exclude confirmed serials.'''
        registry = DeviceRegistry()
        registry.register_device('111', topic='sensor_accel')
        registry.register_device('222', topic='sensor_accel')
        ulog = FakeULog([
            FakeDataset('sensor_accel', 0, {
                'timestamp': _timestamps(1, 2),
                'device_id': np.array([111, 111]),
            }),
            FakeDataset('sensor_accel', 1, {
                'timestamp': _timestamps(1, 2),
            }),
        ])
        segments = segment_ulog(ulog, registry=registry)
        unconfirmed = [s for s in segments if not s.confirmed]
        self.assertEqual(len(unconfirmed), 1)
        self.assertEqual(unconfirmed[0].candidates, ['222'])


class TestComputeSegmentDrift(unittest.TestCase):
    '''
    Test that drift is only computed for confirmed segments.
    '''

    def _dataset(self):
        return FakeDataset('sensor_accel', 0, {
            'timestamp': _timestamps(1, 2, 3, 4),
            'x': np.array([1.0, 2.0, 3.0, 4.0]),
        })

    def test_drift_on_confirmed_segment(self):
        '''Confirmed segments yield a linear drift estimate.'''
        segment = DeviceSegment('sensor_accel', 0, 1000000, 4000000,
                                serial='111', confirmed=True)
        drift = compute_segment_drift(self._dataset(), segment, 'x')
        self.assertEqual(drift['serial'], '111')
        self.assertAlmostEqual(drift['drift_per_s'], 1.0)
        self.assertEqual(drift['samples'], 4)

    def test_drift_refused_on_unconfirmed_segment(self):
        '''Unconfirmed segments must not get a single-device drift.'''
        segment = DeviceSegment('sensor_accel', 0, 1000000, 4000000,
                                candidates=['111', '222'])
        with self.assertRaises(ValueError) as context:
            compute_segment_drift(self._dataset(), segment, 'x')
        self.assertIn('111', str(context.exception))
        self.assertIn('222', str(context.exception))


class TestDatabaseDeviceSegments(unittest.TestCase):
    '''
    Test the DatabaseULog integration: persistence of the registry and of
    the device segments.
    '''

    def setUp(self):
        '''
        Set up the test database.
        '''
        self.db_path = os.path.join(TEST_PATH, 'pyulog_devices_test.sqlite3')
        self.db_handle = DatabaseULog.get_db_handle(self.db_path)
        migrate_db(self.db_path)

    def tearDown(self):
        '''
        Remove the test database after use.
        '''
        os.remove(self.db_path)

    def test_registry_round_trip(self):
        '''A stored registry is loaded back unchanged.'''
        registry = DeviceRegistry()
        registry.register_device('111', topic='sensor_accel', notes='bench unit')
        registry.register_device('222')
        registry.add_replacement(5 * 1000000, 'sensor_accel', '222',
                                 old_serial='111', instance=0, note='swap')
        DatabaseULog.save_device_registry(self.db_handle, registry)
        loaded = DatabaseULog.load_device_registry(self.db_handle)
        self.assertEqual(loaded.devices, registry.devices)
        self.assertEqual(loaded.replacements, registry.replacements)

    def test_segments_round_trip(self):
        '''Segments saved during save() are loaded back unchanged.'''
        log_path = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        registry = DeviceRegistry()
        registry.register_device('111', topic='sensor_accel')

        dbulog = DatabaseULog(self.db_handle, log_file=log_path)
        dbulog.save(device_registry=registry)
        primary_key = dbulog.primary_key
        sort_key = lambda s: (s.topic, s.instance, s.start_timestamp)  # pylint: disable=unnecessary-lambda
        expected = sorted(dbulog.segment_devices(registry), key=sort_key)

        dbulog_loaded = DatabaseULog(self.db_handle, primary_key=primary_key)
        loaded_segments = dbulog_loaded.load_device_segments()
        self.assertEqual(loaded_segments, expected)
        for segment in loaded_segments:
            self.assertGreater(len(segment.rationale), 0)

    def test_lazy_loaded_segment_devices(self):
        '''segment_devices also works on a lazy-loaded log.'''
        log_path = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        dbulog = DatabaseULog(self.db_handle, log_file=log_path)
        dbulog.save()
        primary_key = dbulog.primary_key

        dbulog_loaded = DatabaseULog(self.db_handle, primary_key=primary_key, lazy=True)
        segments = dbulog_loaded.segment_devices()
        self.assertGreater(len(segments), 0)
        self.assertTrue(all(isinstance(segment, DeviceSegment) for segment in segments))

    def test_save_device_segments_requires_save(self):
        '''Device segments cannot be stored before the log itself.'''
        log_path = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        dbulog = DatabaseULog(self.db_handle, log_file=log_path)
        with self.assertRaises(KeyError):
            dbulog.save_device_segments(DeviceRegistry())

    def test_delete_cascades_segments(self):
        '''Deleting a log also deletes its device segments.'''
        log_path = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        dbulog = DatabaseULog(self.db_handle, log_file=log_path)
        dbulog.save(device_registry=DeviceRegistry())
        primary_key = dbulog.primary_key
        dbulog.delete()
        with self.db_handle() as con:
            cur = con.cursor()
            cur.execute('SELECT COUNT(*) FROM ULogDeviceSegment WHERE ULogId = ?',
                        (primary_key,))
            (count,) = cur.fetchone()
            cur.close()
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
