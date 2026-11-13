'''
Test the DatabaseULog module.
'''

import unittest
import os
import sqlite3
import tempfile
from unittest.mock import patch
import numpy as np
from ddt import ddt, data

from pyulog import ULog
from pyulog.db import DatabaseULog
from pyulog.migrate_db import migrate_db

TEST_PATH = os.path.dirname(os.path.abspath(__file__))

@ddt
class TestDatabaseULog(unittest.TestCase):
    '''
    Test that the DatabaseULog successfully reads a ULog, writes it to database
    and then is able to read from it without losing any data.
    '''

    def setUp(self):
        '''
        Set up the test database.
        '''
        self.db_path = os.path.join(TEST_PATH, 'pyulog_test.sqlite3')
        self.db_handle = DatabaseULog.get_db_handle(self.db_path)
        migrate_db(self.db_path)


    def tearDown(self):
        '''
        Remove the test database after use.
        '''
        os.remove(self.db_path)

    @data('sample_log_small',
          'sample_appended_multiple',
          'sample_appended',
          'sample',
          'sample_logging_tagged_and_default_params')
    def test_parsing(self, test_case):
        '''
        Verify that log files written and read from the database are
        identical to the original ulog file.
        '''
        test_file = os.path.join(TEST_PATH, f'{test_case}.ulg')
        log_path = os.path.join(TEST_PATH, test_file)

        ulog = ULog(log_path)
        dbulog_saved = DatabaseULog(self.db_handle, log_file=test_file)
        dbulog_saved.save()
        primary_key = dbulog_saved.primary_key
        dbulog_loaded = DatabaseULog(self.db_handle, primary_key=primary_key, lazy=False)
        self.assertEqual(ulog, dbulog_loaded)

    def test_lazy(self):
        '''
        Verify that when lazy loading is enabled (which is the default
        behaviour), then the datasets are only retrieved when get_dataset is
        explicitly called.
        '''
        test_file = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        log_path = os.path.join(TEST_PATH, test_file)

        ulog = ULog(log_path)
        dbulog_saved = DatabaseULog(self.db_handle, log_file=test_file)
        dbulog_saved.save()
        primary_key = dbulog_saved.primary_key
        dbulog_loaded = DatabaseULog(self.db_handle, primary_key=primary_key)
        for dataset in ulog.data_list:
            db_dataset = next(ds for ds in dbulog_loaded.data_list
                              if ds.name == dataset.name and ds.multi_id == dataset.multi_id)
            self.assertEqual(len(db_dataset.data), 0)
            self.assertNotEqual(len(dataset.data), 0)
            ulog_dataset = ulog.get_dataset(dataset.name,
                                            multi_instance=dataset.multi_id)
            dbulog_dataset = dbulog_loaded.get_dataset(dataset.name,
                                                       multi_instance=dataset.multi_id)
            self.assertEqual(ulog_dataset, dbulog_dataset)


    def test_data_caching(self):
        '''
        Verify that the caching of dataset data works as expected.
        '''
        test_file = os.path.join(TEST_PATH, 'sample_log_small.ulg')

        dbulog_saved = DatabaseULog(self.db_handle, log_file=test_file)
        dbulog_saved.save()
        primary_key = dbulog_saved.primary_key
        dbulog_loaded = DatabaseULog(self.db_handle, primary_key=primary_key, lazy=True)
        for dataset in dbulog_loaded.data_list:
            cache_miss = dbulog_loaded.get_dataset(dataset.name,
                                                   multi_instance=dataset.multi_id,
                                                   caching=True)
            cache_hit = dbulog_loaded.get_dataset(dataset.name,
                                                  multi_instance=dataset.multi_id,
                                                  caching=True)
            uncached = dbulog_loaded.get_dataset(dataset.name,
                                                 multi_instance=dataset.multi_id,
                                                 caching=False)

            self.assertEqual(cache_miss, cache_hit)
            self.assertEqual(cache_miss, uncached)
            self.assertIs(cache_miss, cache_hit)
            self.assertIsNot(cache_miss, uncached)

    def test_save(self):
        '''
        Test that save() twice raises an error, since we currently do not
        support updating the database.
        '''
        log_path = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        dbulog = DatabaseULog(self.db_handle, log_file=log_path)
        dbulog.save()
        with self.assertRaises(KeyError):
            dbulog.save()

    def test_load(self):
        ''' Test that load() on an unknown primary key raises an error.'''
        with self.assertRaises(KeyError):
            _ = DatabaseULog(self.db_handle, primary_key=100)

    def test_unapplied_migrations(self):
        '''
        Test that we get get an error when trying to initialize a DatabaseULog
        if there are unapplied migrations, i.e. the SCHEMA_VERSION of
        DatabaseULog is larger than user_version in the database.
        '''
        migrate_db(self.db_path)
        log_file = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        _ = DatabaseULog(self.db_handle, log_file=log_file)
        with self.assertRaises(ValueError):
            # Increment SCHEMA_VERSION so the database is seemingly out of date
            with patch.object(DatabaseULog, 'SCHEMA_VERSION', DatabaseULog.SCHEMA_VERSION+1):
                _ = DatabaseULog(self.db_handle, log_file=log_file)

    @data('sample_log_small')
    def test_sha256sum(self, test_case):
        '''
        Verify that the sha256sum set on save can be used to find the same file
        again, using any of the approved file input methods.
        '''

        test_file = os.path.join(TEST_PATH, f'{test_case}.ulg')
        dbulog = DatabaseULog(self.db_handle, log_file=test_file)
        dbulog.save()
        digest = DatabaseULog.calc_sha256sum(test_file)
        self.assertEqual(digest, dbulog.sha256sum)

        test_file_handle = open(test_file, 'rb') # pylint: disable=consider-using-with
        open_digest = DatabaseULog.calc_sha256sum(test_file_handle)
        self.assertEqual(digest, open_digest)

        test_file_handle.close()
        closed_digest = DatabaseULog.calc_sha256sum(test_file_handle)
        self.assertEqual(digest, closed_digest)

        pk_from_digest = DatabaseULog.primary_key_from_sha256sum(self.db_handle, digest)
        self.assertEqual(pk_from_digest, dbulog.primary_key)

        dbulog_duplicate = DatabaseULog(self.db_handle, log_file=test_file)
        with self.assertRaises(KeyError):
            dbulog_duplicate.save()

    def test_delete(self):
        '''
        Verify that the delete method completely deletes the relevant ulog from
        the database, and nothing else, by looking at the size of the database
        before and after deleting.
        '''

        def db_size():
            '''
            Get the size in bytes of the database file, after VACUUMING to make
            sure that deleted rows are cleaned up.
            '''
            with self.db_handle() as con:
                con.execute('VACUUM')
            return os.path.getsize(self.db_path)

        # We pre-populate the database with a log to detect if delete() just
        # wipes everything
        test_file1 = os.path.join(TEST_PATH, 'sample.ulg')
        DatabaseULog(self.db_handle, log_file=test_file1).save()
        initial_size = db_size()

        test_file2 = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        dbulog = DatabaseULog(self.db_handle, log_file=test_file2)
        dbulog.save()
        self.assertNotEqual(db_size(), initial_size)

        dbulog.delete()
        self.assertEqual(db_size(), initial_size)

    def test_json(self):
        '''
        Verify that the storage of JSON rows allows for reproduction of the
        datasets.
        '''
        test_file = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        log_path = os.path.join(TEST_PATH, test_file)

        dbulog = DatabaseULog(self.db_handle, log_file=log_path)
        dbulog.save(append_json=True)

        with self.db_handle() as con:
            cur = con.cursor()
            for dataset in dbulog.data_list:
                for field_name, values in dataset.data.items():
                    cur.execute('''
                        SELECT j.key, j.value
                        FROM ULogField uf, json_each(uf.ValueJson) j
                            JOIN ULogDataset uds ON uf.DatasetId = uds.Id
                        WHERE uds.DatasetName = ?
                            AND uds.MultiId = ?
                            AND uf.TopicName = ?
                            AND uds.ULogId = ?
                        ORDER BY j.key ASC
                    ''', (dataset.name, dataset.multi_id, field_name, dbulog.primary_key))
                    results = np.array(cur.fetchall(), dtype=float)
                    db_timestamps = results[:,0].flatten()
                    db_values = results[:,1].flatten()

                    # We must filter out None, nan and inf values since JSON
                    # doesn't support nan and inf.
                    db_values_finite = db_values[np.isfinite(db_values)]
                    values_finite = values[np.isfinite(values)]

                    # We test for approximate equality since we are comparing
                    # string-formatted floats.
                    self.assertEqual(len(db_values_finite), len(values_finite))
                    if len(db_values_finite) > 0:
                        np.testing.assert_allclose(db_values_finite, values_finite)

                    if field_name == 'timestamp':
                        self.assertEqual(len(db_timestamps), len(values))
                        np.testing.assert_allclose(db_timestamps, values)
            cur.close()

    @data('sample',
          'sample_appended',
          'sample_appended_multiple',
          'sample_logging_tagged_and_default_params')
    def test_write_ulog(self, base_name):
        '''
        Test that the write_ulog method successfully replicates all relevant data.
        '''
        with tempfile.TemporaryDirectory() as tmpdirname:
            ulog_file_name = os.path.join(TEST_PATH, base_name + '.ulg')
            written_ulog_file_name = os.path.join(tmpdirname, base_name + '_copy.ulg')

            dbulog = DatabaseULog(self.db_handle, log_file=ulog_file_name)
            dbulog.save()

            lazy_loaded_dbulog = DatabaseULog(
                self.db_handle,
                primary_key=dbulog.primary_key,
                lazy=True
            )
            with self.assertRaises(ValueError):
                lazy_loaded_dbulog.write_ulog(written_ulog_file_name)

            loaded_dbulog = DatabaseULog(self.db_handle, primary_key=dbulog.primary_key, lazy=False)
            loaded_dbulog.write_ulog(written_ulog_file_name)
            copied = ULog(written_ulog_file_name)

        # Some fields are not copied but dropped, so we cheat by modifying the original
        loaded_dbulog._sync_seq_cnt = 0  # pylint: disable=protected-access
        loaded_dbulog._appended_offsets = []  # pylint: disable=protected-access
        loaded_dbulog._incompat_flags[0] &= 0xFE  # pylint: disable=protected-access
        assert copied == loaded_dbulog


class TestDatabaseULogWindowed(unittest.TestCase):
    '''
    Test lazy timestamp-window queries, the optional context sample,
    batching, per-instance ordering and shared external connections.
    '''

    TEST_FILE = os.path.join(TEST_PATH, 'sample_log_small.ulg')

    def setUp(self):
        self.db_path = os.path.join(TEST_PATH, 'pyulog_window_test.sqlite3')
        self.db_handle = DatabaseULog.get_db_handle(self.db_path)
        migrate_db(self.db_path)
        self.ulog = ULog(self.TEST_FILE)
        dbulog = DatabaseULog(self.db_handle, log_file=self.TEST_FILE)
        dbulog.save()
        self.primary_key = dbulog.primary_key
        self._insert_synthetic_datasets()

    def tearDown(self):
        os.remove(self.db_path)

    def _insert_synthetic_datasets(self):
        '''
        Insert datasets with same-timestamp duplicate rows directly into the
        database, so ordering and duplicate retention can be asserted
        deterministically.
        '''
        series = {
            0: ([10, 20, 20, 30, 40, 40, 50], [1., 2., 3., 4., 5., 6., 7.]),
            1: ([15, 15, 25], [10., 11., 12.]),
        }
        with self.db_handle() as con:
            cur = con.cursor()
            for multi_id, (timestamps, values) in series.items():
                cur.execute('''
                    INSERT INTO ULogDataset
                    (DatasetName, MultiId, MessageId, TimestampIndex, ULogId)
                    VALUES ('synth_topic', ?, 999, 0, ?)
                    ''', (multi_id, self.primary_key))
                dataset_id = cur.lastrowid
                ts_array = np.array(timestamps, dtype=np.uint64)
                val_array = np.array(values, dtype=np.float32)
                cur.execute('''
                    INSERT INTO ULogField
                    (TopicName, DataType, ValueArray, DatasetId)
                    VALUES ('timestamp', 'uint64_t', ?, ?)
                    ''', (ts_array.tobytes(), dataset_id))
                cur.execute('''
                    INSERT INTO ULogField
                    (TopicName, DataType, ValueArray, DatasetId)
                    VALUES ('value', 'float', ?, ?)
                    ''', (val_array.tobytes(), dataset_id))
                for index, timestamp in enumerate(timestamps):
                    cur.execute('''
                        INSERT INTO ULogFieldSample
                        (DatasetId, TopicName, SeriesIndex, Timestamp, Value)
                        VALUES (?, 'timestamp', ?, ?, ?)
                        ''', (dataset_id, index, timestamp,
                              ts_array[index:index + 1].tobytes()))
                    cur.execute('''
                        INSERT INTO ULogFieldSample
                        (DatasetId, TopicName, SeriesIndex, Timestamp, Value)
                        VALUES (?, 'value', ?, ?, ?)
                        ''', (dataset_id, index, timestamp,
                              val_array[index:index + 1].tobytes()))
            cur.close()

    def _window_reference(self, name, multi_instance, start, end):
        dataset = self.ulog.get_dataset(name, multi_instance=multi_instance)
        timestamps = dataset.data['timestamp']
        mask = (timestamps >= start) & (timestamps < end)
        return dataset, mask

    def test_half_open_window_matches_ulog(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)
        dataset = self.ulog.get_dataset('sensor_combined')
        timestamps = dataset.data['timestamp']
        start, end = int(timestamps[100]), int(timestamps[200])
        _, mask = self._window_reference('sensor_combined', 0, start, end)

        windowed = dbulog.get_dataset_window('sensor_combined', start, end)

        self.assertEqual(windowed.start_timestamp, start)
        self.assertEqual(windowed.end_timestamp, end)
        self.assertEqual(windowed.sample_count, int(mask.sum()))
        for field in dataset.field_data:
            np.testing.assert_array_equal(
                windowed.data[field.field_name],
                dataset.data[field.field_name][mask])

    def test_context_is_last_point_before_window(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)
        dataset = self.ulog.get_dataset('sensor_combined')
        timestamps = dataset.data['timestamp']
        start, end = int(timestamps[100]), int(timestamps[200])

        windowed = dbulog.get_dataset_window(
            'sensor_combined', start, end, include_context=True)
        context_index = int(np.where(timestamps < start)[0][-1])

        self.assertIsNotNone(windowed.context)
        self.assertEqual(len(windowed.context.data['timestamp']), 1)
        np.testing.assert_array_equal(
            windowed.context.data['timestamp'],
            timestamps[context_index:context_index + 1])
        # The context must not be counted among the in-window samples.
        _, mask = self._window_reference('sensor_combined', 0, start, end)
        self.assertEqual(windowed.sample_count, int(mask.sum()))
        self.assertNotIn(
            windowed.context.data['timestamp'][0],
            windowed.data['timestamp'])

        # Without an earlier sample there is no context.
        first = dbulog.get_dataset_window(
            'sensor_combined', int(timestamps[0]), int(timestamps[3]),
            include_context=True)
        self.assertIsNone(first.context)
        self.assertEqual(first.sample_count, 3)

    def test_duplicates_and_order_per_instance(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)

        windowed = dbulog.get_dataset_window('synth_topic', 20, 40,
                                             multi_instance=0)
        np.testing.assert_array_equal(
            windowed.data['timestamp'], np.array([20, 20, 30], dtype=np.uint64))
        np.testing.assert_array_equal(
            windowed.data['value'], np.array([2., 3., 4.], dtype=np.float32))
        self.assertEqual(windowed.sample_count, 3)

        # A second instance keeps its own time order and duplicates.
        other = dbulog.get_dataset_window('synth_topic', 0, 100,
                                          multi_instance=1)
        np.testing.assert_array_equal(
            other.data['timestamp'], np.array([15, 15, 25], dtype=np.uint64))
        self.assertEqual(other.sample_count, 3)

    def test_batching_preserves_order_across_boundaries(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)
        batches = list(dbulog.iter_dataset_window(
            'synth_topic', 20, 40, multi_instance=0, batch_size=2))

        self.assertEqual([batch.sample_count for batch in batches], [2, 1])
        self.assertTrue(all(batch.context is None for batch in batches))
        np.testing.assert_array_equal(
            np.concatenate([batch.data['timestamp'] for batch in batches]),
            np.array([20, 20, 30], dtype=np.uint64))

        # Context is only attached to the first batch, even across batches.
        with_context = list(dbulog.iter_dataset_window(
            'synth_topic', 20, 40, multi_instance=0,
            include_context=True, batch_size=2))
        self.assertIsNotNone(with_context[0].context)
        self.assertTrue(all(batch.context is None for batch in with_context[1:]))
        np.testing.assert_array_equal(
            with_context[0].context.data['timestamp'],
            np.array([10], dtype=np.uint64))

    def test_empty_window_still_delivers_context(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)
        batches = list(dbulog.iter_dataset_window(
            'synth_topic', 20, 20, multi_instance=0, include_context=True))
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].sample_count, 0)
        self.assertIsNotNone(batches[0].context)
        np.testing.assert_array_equal(
            batches[0].context.data['timestamp'],
            np.array([10], dtype=np.uint64))

    def test_lazy_stream_does_not_cache_full_series(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key,
                              lazy=True)
        for dataset in dbulog.data_list:
            self.assertEqual(len(dataset.data), 0)

        dataset = self.ulog.get_dataset('sensor_combined')
        timestamps = dataset.data['timestamp']
        batches = list(dbulog.iter_dataset_window(
            'sensor_combined', int(timestamps[10]), int(timestamps[40]),
            batch_size=5))

        self.assertEqual(sum(b.sample_count for b in batches), 30)
        self.assertTrue(all(b.sample_count <= 5 for b in batches))
        # Window reads must not populate the ordinary dataset cache.
        cached = next(ds for ds in dbulog.data_list if ds.name == 'sensor_combined')
        self.assertEqual(len(cached.data), 0)

    def test_external_connection_reused_after_early_close(self):
        connection = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        connection.execute('PRAGMA foreign_keys=on')
        try:
            dbulog = DatabaseULog(connection, primary_key=self.primary_key)
            self.assertTrue(dbulog._external_connection)  # pylint: disable=protected-access

            timestamps = self.ulog.get_dataset('sensor_combined').data['timestamp']
            generator = dbulog.iter_dataset_window(
                'sensor_combined', int(timestamps[0]), int(timestamps[-1]) + 1,
                batch_size=3)
            first_batch = next(generator)
            self.assertEqual(first_batch.sample_count, 3)
            generator.close()

            # The external connection must stay open and serve other topics.
            self.assertEqual(connection.execute('SELECT 1').fetchone()[0], 1)
            other = DatabaseULog(connection, primary_key=self.primary_key)
            cpuload = other.get_dataset_window(
                'cpuload', 0,
                int(self.ulog.get_dataset('cpuload').data['timestamp'][-1]) + 1)
            self.assertEqual(
                cpuload.sample_count,
                len(self.ulog.get_dataset('cpuload').data['timestamp']))
        finally:
            connection.close()

    def test_backfill_samples_from_blob(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)
        dataset = self.ulog.get_dataset('sensor_combined')
        timestamps = dataset.data['timestamp']
        start, end = int(timestamps[10]), int(timestamps[40])

        with self.db_handle() as con:
            dataset_id = con.execute('''
                SELECT Id FROM ULogDataset
                WHERE DatasetName = 'sensor_combined' AND ULogId = ?
                ''', (self.primary_key,)).fetchone()[0]
            con.execute('DELETE FROM ULogFieldSample WHERE DatasetId = ?',
                        (dataset_id,))

        self.assertEqual(
            dbulog.get_dataset_window('sensor_combined', start, end).sample_count,
            0)

        dbulog.backfill_samples()
        _, mask = self._window_reference('sensor_combined', 0, start, end)
        windowed = dbulog.get_dataset_window('sensor_combined', start, end)
        self.assertEqual(windowed.sample_count, int(mask.sum()))
        np.testing.assert_array_equal(
            windowed.data['timestamp'], timestamps[mask])

        # Backfilling again must not duplicate any rows.
        with self.db_handle() as con:
            count_before = con.execute(
                'SELECT COUNT(*) FROM ULogFieldSample').fetchone()[0]
        dbulog.backfill_samples()
        with self.db_handle() as con:
            count_after = con.execute(
                'SELECT COUNT(*) FROM ULogFieldSample').fetchone()[0]
        self.assertEqual(count_before, count_after)

    def test_invalid_arguments(self):
        dbulog = DatabaseULog(self.db_handle, primary_key=self.primary_key)
        with self.assertRaises(ValueError):
            dbulog.get_dataset_window('sensor_combined', 100, 50)
        with self.assertRaises(ValueError):
            dbulog.iter_dataset_window('sensor_combined', 0, 10, batch_size=0)
        with self.assertRaises(KeyError):
            dbulog.get_dataset_window('does_not_exist', 0, 10)

    def test_streaming_memory_is_bounded_by_window(self):
        '''
        A small window of a large dataset must be read in small batches
        without ever touching the packed full-series BLOB columns, and the
        streamed rows must not be retained on the DatabaseULog afterwards.
        '''
        big_path = os.path.join(TEST_PATH, 'sample_appended.ulg')
        big_ulog = ULog(big_path)
        DatabaseULog(self.db_handle, log_file=big_path).save()
        pk = DatabaseULog.primary_key_from_sha256sum(
            self.db_handle, DatabaseULog.calc_sha256sum(big_path))
        dbulog = DatabaseULog(self.db_handle, primary_key=pk, lazy=True)

        dataset = big_ulog.get_dataset('sensor_combined')
        timestamps = dataset.data['timestamp']
        total_samples = len(timestamps)
        start, end = int(timestamps[100]), int(timestamps[140])
        mask = (timestamps >= start) & (timestamps < end)

        statements = []
        with self.db_handle() as traced:
            traced.set_trace_callback(statements.append)
            reader = DatabaseULog(traced, primary_key=pk, lazy=True)
            batches = list(reader.iter_dataset_window(
                'sensor_combined', start, end, batch_size=10))

        self.assertEqual(sum(b.sample_count for b in batches), int(mask.sum()))
        self.assertTrue(all(b.sample_count <= 10 for b in batches))
        self.assertLess(max(b.sample_count for b in batches), total_samples)

        # Only per-sample rows and field metadata may be read, never the
        # BLOB/JSON columns that hold the whole series.
        self.assertTrue(statements)
        self.assertFalse(
            any('ValueArray' in sql or 'ValueJson' in sql for sql in statements))

        cached = next(ds for ds in reader.data_list
                      if ds.name == 'sensor_combined')
        self.assertEqual(len(cached.data), 0)

