'''
Test the DatabaseULog module.
'''

import unittest
import os
import tempfile
from unittest.mock import patch
import numpy as np
from ddt import ddt, data

from pyulog import ULog
from pyulog.db import DatabaseULog
from pyulog.exceptions import ULogMergeConflict
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


class TestDatabaseULogSupplements(unittest.TestCase):
    '''
    Test importing a readable export first and completing it later from a
    full export of the same acquisition.
    '''

    def setUp(self):
        self.db_path = os.path.join(TEST_PATH, 'pyulog_supplement_test.sqlite3')
        self.db_handle = DatabaseULog.get_db_handle(self.db_path)
        migrate_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    @staticmethod
    def _write_prefix(source_path, target_path, fraction):
        with open(source_path, 'rb') as source:
            data = source.read()
        with open(target_path, 'wb') as target:
            target.write(data[:int(len(data) * fraction)])
        return target_path

    def test_partial_then_full_matches_original(self):
        '''
        A readable export that lacks samples is completed by the full ULog;
        the result must equal the original log exactly.
        '''
        source = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        with tempfile.TemporaryDirectory() as tmpdir:
            partial_path = self._write_prefix(
                source, os.path.join(tmpdir, 'partial.ulg'), 0.25)
            partial = DatabaseULog(self.db_handle, log_file=partial_path)
            partial.save()
            full_path = os.path.join(TEST_PATH, 'sample_log_small.ulg')
            full = DatabaseULog(self.db_handle, log_file=full_path)
            full.save()

            self.assertEqual(full.primary_key, partial.primary_key)
            report = full.last_merge_report
            self.assertEqual(report['status'], 'merged')
            self.assertGreater(report['added_samples'], 0)

            loaded = DatabaseULog(self.db_handle,
                                  primary_key=partial.primary_key,
                                  lazy=False)
            self.assertEqual(ULog(full_path), loaded)

    def test_all_samples_merge_back_to_original(self):
        '''
        Complete every bundled sample from a readable prefix and compare.
        This covers topics that legitimately repeat timestamps and logs with
        appended data.
        '''
        for base_name in ('sample',
                          'sample_log_small',
                          'sample_appended',
                          'sample_appended_multiple',
                          'sample_logging_tagged_and_default_params',
                          'sample_px4_events'):
            self.setUp()
            source = os.path.join(TEST_PATH, base_name + '.ulg')
            with tempfile.TemporaryDirectory() as tmpdir:
                partial_path = self._write_prefix(
                    source, os.path.join(tmpdir, 'partial.ulg'), 0.3)
                partial = DatabaseULog(self.db_handle, log_file=partial_path)
                partial.save()
                full = DatabaseULog(self.db_handle, log_file=source)
                full.save()
                loaded = DatabaseULog(self.db_handle,
                                      primary_key=partial.primary_key,
                                      lazy=False)
                self.assertEqual(ULog(source), loaded, base_name)
            self.tearDown()

    def test_resubmitting_full_file_is_idempotent(self):
        '''
        Re-submitting an already merged complete file must not add rows,
        datasets or samples.
        '''
        source = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        with tempfile.TemporaryDirectory() as tmpdir:
            partial_path = self._write_prefix(
                source, os.path.join(tmpdir, 'partial.ulg'), 0.25)
            first = DatabaseULog(self.db_handle, log_file=partial_path)
            first.save()
            DatabaseULog(self.db_handle, log_file=source).save()

            def counts():
                with self.db_handle() as con:
                    cur = con.cursor()
                    result = []
                    for table in ('ULog', 'ULogDataset', 'ULogField',
                                  'ULogSupplement'):
                        cur.execute('''
                            SELECT COUNT(*) FROM {}
                            '''.format(table))
                        result.append(cur.fetchone()[0])
                    cur.close()
                return result

            before = counts()
            again = DatabaseULog(self.db_handle, log_file=source)
            again.save()
            self.assertEqual(again.primary_key, first.primary_key)
            self.assertEqual(counts(), before)

    def test_conflict_keeps_archive_and_reports_first_topic(self):
        '''
        A complete file that contradicts stored samples may not change the
        archive; the error names the first conflicting topic and timestamp.
        '''
        source = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        with tempfile.TemporaryDirectory() as tmpdir:
            partial_path = self._write_prefix(
                source, os.path.join(tmpdir, 'partial.ulg'), 0.25)
            partial = DatabaseULog(self.db_handle, log_file=partial_path)
            partial.save()

            modified = ULog(source)
            dataset = next(d for d in modified.data_list
                           if d.name == 'actuator_armed')
            field_name = next(name for name in dataset.data
                              if name != 'timestamp')
            dataset.data[field_name] = dataset.data[field_name].copy()
            dataset.data[field_name][0] = dataset.data[field_name][0] + 1
            modified_path = os.path.join(tmpdir, 'modified.ulg')
            modified.write_ulog(modified_path)

            with self.db_handle() as con:
                cur = con.cursor()
                cur.execute('''
                    SELECT COUNT(*), SUM(LENGTH(ValueArray))
                    FROM ULogField f JOIN ULogDataset d ON f.DatasetId = d.Id
                    WHERE d.ULogId = ?
                    ''', (partial.primary_key,))
                before = cur.fetchone()
                cur.close()

            conflicting = DatabaseULog(self.db_handle, log_file=modified_path)
            with self.assertRaises(ULogMergeConflict) as raised:
                conflicting.save()
            error = raised.exception
            self.assertEqual(error.conflict_topic, 'actuator_armed')
            self.assertEqual(error.conflict_multi_id, 0)
            self.assertEqual(
                error.conflict_timestamp,
                int(dataset.data['timestamp'][0]))

            with self.db_handle() as con:
                cur = con.cursor()
                cur.execute('''
                    SELECT COUNT(*), SUM(LENGTH(ValueArray))
                    FROM ULogField f JOIN ULogDataset d ON f.DatasetId = d.Id
                    WHERE d.ULogId = ?
                    ''', (partial.primary_key,))
                self.assertEqual(cur.fetchone(), before)
                cur.execute('SELECT COUNT(*) FROM ULog WHERE Id = ?',
                            (partial.primary_key,))
                self.assertEqual(cur.fetchone()[0], 1)
                cur.close()

    def test_identical_time_range_from_other_run_is_separate(self):
        '''
        Two different acquisitions with exactly the same start/end time must
        not be attached to the same record.
        '''
        stored = os.path.join(TEST_PATH, 'sample_appended.ulg')
        other = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        stored_log = ULog(stored)
        other_log = ULog(other)
        for key in ('sys_uuid', 'ver_hw', 'ver_sw', 'sys_name'):
            if key in stored_log.msg_info_dict:
                other_log._msg_info_dict[key] = stored_log.msg_info_dict[key]
                other_log._msg_info_dict_types[key] = \
                    stored_log._msg_info_dict_types[key]
        other_log._start_timestamp = stored_log.start_timestamp
        other_log._last_timestamp = stored_log.last_timestamp

        with tempfile.TemporaryDirectory() as tmpdir:
            other_path = os.path.join(tmpdir, 'other.ulg')
            other_log.write_ulog(other_path)
            stored_db = DatabaseULog(self.db_handle, log_file=stored)
            stored_db.save()
            other_db = DatabaseULog(self.db_handle, log_file=other_path)
            other_db.save()

            self.assertNotEqual(stored_db.primary_key, other_db.primary_key)
            self.assertIsNone(other_db.last_merge_report)

    def test_contained_partial_is_not_duplicated(self):
        '''
        Importing the full export first and the readable prefix afterwards
        must not duplicate anything.
        '''
        source = os.path.join(TEST_PATH, 'sample_log_small.ulg')
        with tempfile.TemporaryDirectory() as tmpdir:
            partial_path = self._write_prefix(
                source, os.path.join(tmpdir, 'partial.ulg'), 0.35)
            full = DatabaseULog(self.db_handle, log_file=source)
            full.save()
            partial = DatabaseULog(self.db_handle, log_file=partial_path)
            partial.save()

            self.assertEqual(partial.primary_key, full.primary_key)
            self.assertEqual(partial.last_merge_report['status'], 'contained')
            self.assertEqual(partial.last_merge_report['added_samples'], 0)
            self.assertEqual(partial.last_merge_report['added_datasets'], [])
