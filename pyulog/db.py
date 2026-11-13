'''
Module containing the DatabaseULog class.
'''

import sqlite3
import hashlib
import contextlib
import datetime
import numpy as np
from pyulog import ULog
from pyulog.exceptions import ULogMergeConflict, ULogMergeEvidenceError

# pylint: disable=too-many-instance-attributes
class DatabaseULog(ULog):
    '''
    This class can be used in place of a ULog, except that it reads from and
    writes to a sqlite3 database instead of a file.

    The first time you see a ulog file, instantiate a DatabaseULog directly
    from a ulog file, and then call save() to write it to the database. Later
    it can be accessed by providing the primary_key, upon which this class
    loads all needed fields from the database. If you don't have the
    primary_key value available, but you have the .ulg file and know it is in
    the database, you can retrieve the hash with DatabaseULog.calc_sha256sum
    and DatabaseULog.primary_key_from_sha256sum.

    This class is currently designed to be write-once only, so you cannot
    update existing database entries. To do so, first call delete() and then
    save() again.

    The one supported way of extending an existing record is save() on a log
    that is provably part of the same acquisition (same device identity and
    overlapping time span, e.g. an export that was written before the logger
    had flushed all topics). See save() for the exact evidence rules. Anything
    that conflicts with already stored samples is rejected without modifying
    the existing record.

    A weakness of the implementation is that there are some silently failing
    states if you don't call save() immediately after instantiating from a ULog
    object. The requirement of explicit save() is to prevent unexpected, sudden
    side effects, which is considered worse than the current solution.

    Example usage:
    > from pyulog.db import DatabaseULog
    >
    > db_handle = DatabaseULog.get_db_handle('dbulog.sqlite3')
    > dbulog = DatabaseULog(db_handle, log_file='example.ulg') # Slow
    > dbulog.save()
    > pk = dbulog.primary_key()
    > # [...]
    > dbulog = DatabaseULog(db_handle, primary_key=pk) # Fast

    SCHEMA_VERSION specifies which version of the database schema (as read from
    "PRAGMA user_version") this file is supposed to match. This is done to
    ensure consistency between the database operations and the state of the
    database. If SCHEMA_VERSION is higher than what is found in the database,
    then it means that the datbaase is has not been updated, and the
    contsructor will throw an exception. See the documentation of
    "ulog_migratedb" for more information.
    '''
    SCHEMA_VERSION = 6

    @staticmethod
    def get_db_handle(db_path):
        '''
        Generate a database handle that can be used in subsequent database access.
        '''
        def db_handle():
            con = sqlite3.connect(
                db_path,
                detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES
            )
            # The next line is necessary for sqlite3 to actually respect
            # FOREIGN KEY constraints and ON DELETE CASCADE.
            con.execute('PRAGMA foreign_keys=on')
            return con
        return db_handle

    @staticmethod
    def exists_in_db(db_handle, primary_key):
        '''
        Check whether an ULog row with Id=primary_key exists in the database
        accessed with db_handle.
        '''
        with db_handle() as con:
            cur = con.cursor()

            cur.execute('''
                SELECT COUNT(*)
                FROM ULog
                WHERE Id = ?
                ''', (primary_key,))
            count, = cur.fetchone()
            cur.close()

            return count == 1

    @staticmethod
    def primary_key_from_sha256sum(db_handle, sha256sum):
        '''
        Search the database for a row with the given SHA256 digest and returns
        the corresponding primary key. Returns None if not found.
        '''
        primary_key = None
        with db_handle() as con:
            cur = con.cursor()
            cur.execute('''
                SELECT Id
                FROM ULog
                WHERE SHA256Sum = ?
                ''', (sha256sum,))
            row = cur.fetchone()
            if row:
                primary_key = row[0]
            cur.close()
        return primary_key

    @staticmethod
    def primary_key_from_supplement_sha256sum(db_handle, sha256sum):
        '''
        Return the primary key of the record that already contains the given
        supplement file, or None if the digest has not been merged yet.

        This is what makes re-submitting the same complete file idempotent:
        the digest is already recorded as part of the parent record, so no
        rows are inserted a second time.
        '''
        primary_key = None
        with db_handle() as con:
            cur = con.cursor()
            cur.execute('''
                SELECT ULogId
                FROM ULogSupplement
                WHERE SHA256Sum = ? AND Status IN ('merged', 'contained')
                ORDER BY Id
                LIMIT 1
                ''', (sha256sum,))
            row = cur.fetchone()
            if row:
                primary_key = row[0]
            cur.close()
        return primary_key

    @staticmethod
    def calc_sha256sum(log_file):
        '''
        Compute the SHA256 digest of a file, specified as a file or a valid file path.
        '''
        if log_file is None:
            return None
        if isinstance(log_file, str):
            file_context = open(log_file, 'rb') # pylint: disable=consider-using-with
        elif log_file.closed:
            file_context = open(log_file.name, 'rb')
        else:
            file_context = contextlib.nullcontext(log_file)

        file_hash = hashlib.sha256()
        with file_context as open_file:
            while True:
                block = open_file.read(4096)
                file_hash.update(block)
                if block == b'':
                    break
        return file_hash.hexdigest()


    def __init__(self, db_handle, primary_key=None, log_file=None, lazy=True, **kwargs):
        '''
        You always need the database handle (which can be generated with
        DatabaseULog.get_db_handle), but there are two options for the
        parameters "primary_key" and "log_file":
        - For storing a new log in the database, supply the corresponding
          log_file parameter, but leave the primary_key field at None.
        - For reading an existing log from the database, supply the desired
          primary_key parameter, but leave the log_file parameter at None. If
          you don't know the primary_key, you can try finding it with
          DatabaseULog.primary_key_from_sha256sum.
        You cannot supply both of these parameters.

        Furthermore, the "lazy" parameter specifies whether all data fields
        should be read on-demand when get_dataset is called, or if they should
        all be read and populated into data_list when the object is
        instantiated.

        The constructor also checks that SCHEMA_VERSION matches the "PRAGMA
        user_version" found in the database, see the documentation of "PRAGMA
        user_version" for more information.
        '''

        with db_handle() as con:
            cur = con.cursor()
            cur.execute('PRAGMA user_version')
            (db_version,) = cur.fetchone()
            if db_version < DatabaseULog.SCHEMA_VERSION:
                raise ValueError('Database version %d < schema version %d, migration needed.' % (
                    db_version,
                    DatabaseULog.SCHEMA_VERSION
            ))

        if log_file is not None and primary_key is not None:
            raise ValueError('You cannot provide both primary_key and log_file.')
        if log_file is None and primary_key is None:
            raise ValueError('You must provide either a primary_key or log_file.')


        self._pk = primary_key
        self._db = db_handle
        self._lazy_loaded = lazy
        if log_file is not None:
            self._sha256sum = DatabaseULog.calc_sha256sum(log_file)

        super().__init__(log_file, **kwargs)
        if primary_key is not None:
            self.load(lazy=lazy)

    def __eq__(self, other):
        """
        If the other object is a normal ULog, then we just want to compare ULog
        data, not DatabaseULog specific fields, because we want to compare
        theULog file contents.
        """
        if type(other) is ULog:  # pylint: disable=unidiomatic-typecheck
            return other.__eq__(self)
        return super().__eq__(other)

    def write_ulog(self, log_file):
        if self._lazy_loaded:
            raise ValueError('Cannot write after lazy load because it has no datasets.')
        super().write_ulog(log_file)

    @property
    def primary_key(self):
        '''The primary key of the ulog, pointing to the correct "ULog" row in the database.'''
        return self._pk

    @property
    def sha256sum(self):
        '''The computed SHA256 digest of the file, stored for later use.'''
        return self._sha256sum

    # pylint: disable=too-many-locals,too-many-branches
    def load(self, lazy=True):
        '''
        Load all necessary data from the database, possibly except for the data series
        themselves, which can cost unnecessary resources.

        If lazy=True, then the data series will be left unread, deferred until
        get_dataset is called. If however lazy=False, then all data series will
        be read at once.

        Even if the log was originally saved with append_json=True, this
        function will always use the faster BLOB column for retrieval.
        '''
        if not DatabaseULog.exists_in_db(self._db, self._pk):
            raise KeyError(f'No ULog in database with Id={self._pk}')

        with self._db() as con:
            cur = con.cursor()

            # ULog metadata
            cur.execute('''
                SELECT FileVersion,
                       StartTimestamp,
                       LastTimestamp,
                       CompatFlags,
                       IncompatFlags,
                       SyncCount,
                       HasSync,
                       SHA256Sum
                FROM ULog
                WHERE Id = ?
                ''', (self._pk,))
            ulog_result = cur.fetchone()
            self._file_version = ulog_result[0]
            self._start_timestamp = ulog_result[1]
            self._last_timestamp = ulog_result[2]
            self._compat_flags = [ord(c) for c in ulog_result[3]]
            self._incompat_flags = [ord(c) for c in ulog_result[4]]
            self._sync_seq_cnt = ulog_result[5]
            self._has_sync = ulog_result[6]
            self._sha256sum = ulog_result[7]

            # appended_offsets
            cur.execute('''
                SELECT Offset
                FROM ULogAppendedOffsets
                WHERE ULogId = ?
                ORDER BY SeriesIndex
                ''', (self._pk,))
            offsets_result = cur.fetchall()
            for offset, in offsets_result:
                self._appended_offsets.append(offset)

            # data_list
            self._data_list = []
            cur.execute('''
                SELECT DatasetName, MultiId
                FROM ULogDataset
                WHERE ULogId = ?
                ORDER BY DatasetName, MultiId
                ''', (self._pk,))
            dataset_results = cur.fetchall()
            for dataset_name, multi_id in dataset_results:
                dataset = self.get_dataset(dataset_name,
                                           multi_instance=multi_id,
                                           lazy=lazy,
                                           db_cursor=cur,
                                           caching=False)
                self._data_list.append(dataset)

            # dropouts
            cur.execute('''
                SELECT Timestamp, Duration
                FROM ULogMessageDropout
                WHERE ULogId = ?
                ''', (self._pk,))
            for timestamp, duration in cur.fetchall():
                self._dropouts.append(
                    DatabaseULog.DatabaseMessageDropout(
                        timestamp=timestamp,
                        duration=duration,
                    )
                )

            # logged_messages
            cur.execute('''
                SELECT LogLevel, Timestamp, Message
                FROM ULogMessageLogging
                WHERE ULogId = ?
                ''', (self._pk,))
            for log_level, timestamp, message in cur.fetchall():
                self._logged_messages.append(
                    DatabaseULog.DatabaseMessageLogging(
                        log_level=log_level,
                        timestamp=timestamp,
                        message=message,
                    )
                )

            # logged_messages_tagged
            cur.execute('''
                SELECT LogLevel, Tag, Timestamp, Message
                FROM ULogMessageLoggingTagged
                WHERE ULogId = ?
                ''', (self._pk,))
            for log_level, tag, timestamp, message in cur.fetchall():
                if tag not in self._logged_messages_tagged:
                    self._logged_messages_tagged[tag] = []
                self._logged_messages_tagged[tag].append(
                    DatabaseULog.DatabaseMessageLoggingTagged(
                        log_level=log_level,
                        tag=tag,
                        timestamp=timestamp,
                        message=message,
                    )
                )

            # message_formats
            cur.execute('''
                SELECT
                    msg.Name,
                    field.FieldType,
                    field.ArraySize,
                    field.Name
                FROM ULogMessageFormat msg JOIN ULogMessageFormatField field
                    ON field.MessageId = msg.Id
                WHERE ULogId = ?
                ''', (self._pk,))
            for row in cur.fetchall():
                msg_name = row[0]
                field_data = row[1:]
                if msg_name in self._message_formats:
                    self._message_formats[msg_name].fields.append(field_data)
                else:
                    self._message_formats[msg_name] = DatabaseULog.DatabaseMessageFormat(
                        name=msg_name,
                        fields=[field_data]
                    )

            # msg_info_dict
            cur.execute('''
                SELECT Key, Typename, Value
                FROM ULogMessageInfo
                WHERE ULogId = ?
                ''', (self._pk,))
            for key, typename, value in cur.fetchall():
                self._msg_info_dict[key] = value
                self._msg_info_dict_types[key] = typename

            # msg_info_multiple_dict
            cur.execute('''
                SELECT Id, Key, Typename
                FROM ULogMessageInfoMultiple
                WHERE ULogId = ?
                ''', (self._pk,))
            for message_id, key, typename in cur.fetchall():
                self._msg_info_multiple_dict[key] = []
                self._msg_info_multiple_dict_types[key] = typename
                cur.execute('''
                    SELECT Id
                    FROM ULogMessageInfoMultipleList
                    WHERE MessageId = ?
                    ORDER BY SeriesIndex
                    ''', (message_id,))
                for (list_id,) in cur.fetchall():
                    cur.execute('''
                        SELECT Value
                        FROM ULogMessageInfoMultipleListElement
                        WHERE ListId = ?
                        ORDER BY SeriesIndex
                        ''', (list_id,))
                    self._msg_info_multiple_dict[key].append([value for (value,) in cur.fetchall()])

            # initial_parameters
            cur.execute('''
                SELECT Key, Value
                FROM ULogInitialParameter
                WHERE ULogId = ?
                ''', (self._pk,))
            for key, value in cur.fetchall():
                self._initial_parameters[key] = value

            # _default_parameters
            cur.execute('''
                SELECT DefaultType, Key, Value
                FROM ULogDefaultParameter
                WHERE ULogId = ?
                ''', (self._pk,))
            for default_type, key, value in cur.fetchall():
                if default_type not in self._default_parameters:
                    self._default_parameters[default_type] = {}
                self._default_parameters[default_type][key] = value

            # changed_parameters
            cur.execute('''
                SELECT Timestamp, Key, Value
                FROM ULogChangedParameter
                WHERE ULogId = ?
                ''', (self._pk,))
            for timestamp, key, value in cur.fetchall():
                self._changed_parameters.append((timestamp, key, value))

            cur.close()
        self._lazy_loaded = lazy

    def get_dataset(self, name, multi_instance=0, lazy=False, db_cursor=None, caching=True):
        '''
        Access a specific dataset and its data series from the database.

        The "lazy" argument specifies whether only dataset metadata should be
        retrieved from the database, or if the data series arrays should be
        retrieved too.

        The optional "db_cursor" argument can be used to avoid re-opening the
        database connection each time get_dataset is called.

        Since we don't expect the data to change often, we will normally use
        self._data_list as a cache, and check there before reading from the
        database. However, if caching=False, then we will always read anew
        from the database.
        '''

        if db_cursor is None:
            db_context = self._db()
            cur = db_context.cursor()
        else:
            db_context = contextlib.nullcontext()
            cur = db_cursor

        existing_dataset = None
        for dataset in self._data_list:
            if dataset.name == name and dataset.multi_id == multi_instance:
                existing_dataset = dataset
                break

        if (caching
                and existing_dataset is not None
                and (lazy or existing_dataset.data)):
            return existing_dataset

        with db_context:
            cur.execute('''
                SELECT Id, TimestampIndex, MessageId
                FROM ULogDataset
                WHERE DatasetName = ? AND MultiId = ? AND ULogId = ?
                ''', (name, multi_instance, self._pk))
            dataset_result = cur.fetchone()
            if dataset_result is None:
                raise KeyError(f'Dataset with name {name} and multi id {multi_instance} not found.')
            dataset_id, timestamp_idx, msg_id = dataset_result
            fields = []
            data = {}
            if not lazy:
                cur.execute('''
                    SELECT TopicName, DataType, ValueArray
                    FROM ULogField
                    WHERE DatasetId = ?
                    ''', (dataset_id,))
                field_results = cur.fetchall()
                for field_name, data_type, value_bytes in field_results:
                    fields.append(DatabaseULog._FieldData(field_name=field_name,type_str=data_type))
                    dtype = DatabaseULog._UNPACK_TYPES[data_type][2]
                    data[field_name] = np.frombuffer(value_bytes, dtype=dtype)

        # If caching=True but there is no existing dataset we could append a
        # new one to self._data_list, but that could be considered a
        # non-obvious side effect.
        if caching and existing_dataset is not None:
            existing_dataset.msg_id = msg_id
            existing_dataset.timestamp_idx = timestamp_idx
            existing_dataset.field_data = fields
            existing_dataset.data = data
            dataset = existing_dataset
        else:
            dataset = DatabaseULog.DatabaseData(
                name=name,
                multi_id=multi_instance,
                msg_id=msg_id,
                timestamp_idx=timestamp_idx,
                field_data=fields,
                data=data,
            )
        return dataset

    # Keys that identify the physical acquisition device. A supplement is
    # only considered to belong to an existing record when at least one of
    # these can be compared positively. Equal start/end timestamps alone are
    # never sufficient.
    _IDENTITY_KEYS = ('sys_uuid', 'ver_hw', 'ver_sw', 'sys_name', 'sys_mcu', 'sys_os_name')

    @staticmethod
    def _as_text(value):
        '''Normalise an info value loaded from sqlite to a comparable string.'''
        if isinstance(value, bytes):
            try:
                return value.decode('utf-8')
            except UnicodeDecodeError:
                return value
        if isinstance(value, str):
            return value
        return str(value)

    @classmethod
    def _identity_matches(cls, existing_info, new_info):
        '''
        Return True if the two info dictionaries carry positive evidence that
        they were recorded by the same device.

        sys_uuid is the strongest key. Logs without one (older firmware) fall
        back to a matching hardware/software pair.
        '''
        existing = {key: cls._as_text(existing_info[key])
                    for key in cls._IDENTITY_KEYS if key in existing_info}
        new = {key: cls._as_text(new_info[key])
               for key in cls._IDENTITY_KEYS if key in new_info}

        if 'sys_uuid' in existing or 'sys_uuid' in new:
            return existing.get('sys_uuid') is not None and \
                   existing.get('sys_uuid') == new.get('sys_uuid')
        if 'ver_hw' in existing or 'ver_sw' in existing:
            return existing.get('ver_hw') is not None and \
                   existing.get('ver_sw') is not None and \
                   existing.get('ver_hw') == new.get('ver_hw') and \
                   existing.get('ver_sw') == new.get('ver_sw')
        # Without any device identity there is no evidence of a common
        # acquisition, even if the timestamps happen to be identical.
        return False

    def _dataset_keys(self):
        '''Return the set of (topic name, multi id) in this log.'''
        return {(dataset.name, dataset.multi_id) for dataset in self.data_list}

    def _has_common_sample(self, cur, candidate_id, own_datasets):
        '''
        Check whether this log and the candidate share enough identical
        samples in topics that both contain.

        This is the positive evidence that the two files are parts of one
        acquisition. A matching device identity, an overlapping time range or
        even identical start/end timestamps are not enough on their own,
        because a second run on the same hardware can reproduce those.

        A single coincidentally equal sample (for example an unchanged
        parameter or a constant field) is not enough either: at least two
        samples at different topic/timestamp pairs have to be identical.
        '''
        matches = 0
        for dataset in self.data_list:
            key = (dataset.name, dataset.multi_id)
            if key not in own_datasets:
                continue
            cur.execute('''
                SELECT Id
                FROM ULogDataset
                WHERE ULogId = ? AND DatasetName = ? AND MultiId = ?
                ''', (candidate_id, dataset.name, dataset.multi_id))
            row = cur.fetchone()
            if row is None:
                continue
            fields, _, old_data, _ = self._load_dataset_rows(cur, row[0])
            if [field.field_name for field in fields] != \
                    [field.field_name for field in dataset.field_data]:
                continue
            if any(left.type_str != right.type_str for left, right in
                   zip(fields, dataset.field_data)):
                continue

            old_by_time = {}
            for index, timestamp in enumerate(old_data['timestamp'].tolist()):
                old_by_time.setdefault(int(timestamp), []).append(index)
            if not old_by_time:
                continue

            new_timestamps = dataset.data['timestamp']
            for new_index, timestamp in enumerate(new_timestamps.tolist()):
                if int(timestamp) == 0:
                    # Zero timestamps are shared by unrelated logs (initial
                    # values), so they cannot prove a common acquisition.
                    continue
                old_rows = old_by_time.get(int(timestamp))
                if not old_rows:
                    continue
                row = {name: values[new_index]
                       for name, values in dataset.data.items()}
                for old_index in old_rows:
                    old_row = {name: values[old_index]
                               for name, values in old_data.items()}
                    if self._rows_equal(row, old_row):
                        matches += 1
                        break
                if matches >= 2:
                    return True
        return False

    def _find_merge_target(self, require_common_sample=True):
        '''
        Find an existing record that this log is a supplement of.

        The time ranges have to overlap, the device identity has to match, at
        least one topic instance has to be shared and, if
        require_common_sample is set, at least one sample in that shared topic
        has to be identical. Otherwise None is returned and the log is stored
        as a separate acquisition.
        '''
        own_datasets = self._dataset_keys()
        if not own_datasets:
            return None

        with self._db() as con:
            cur = con.cursor()
            cur.execute('''
                SELECT Id
                FROM ULog
                WHERE StartTimestamp <= ? AND LastTimestamp >= ?
                ORDER BY Id
                ''', (self._last_timestamp, self._start_timestamp))
            candidate_ids = [row[0] for row in cur.fetchall()]
            if not candidate_ids:
                cur.close()
                return None

            own_info = self.msg_info_dict
            best = None
            best_shared = 0
            for candidate_id in candidate_ids:
                cur.execute('''
                    SELECT Key, Value
                    FROM ULogMessageInfo
                    WHERE ULogId = ?
                    ''', (candidate_id,))
                candidate_info = dict(cur.fetchall())
                if not self._identity_matches(candidate_info, own_info):
                    continue

                cur.execute('''
                    SELECT DatasetName, MultiId
                    FROM ULogDataset
                    WHERE ULogId = ?
                    ''', (candidate_id,))
                candidate_datasets = {(name, multi_id)
                                      for name, multi_id in cur.fetchall()}
                shared = len(own_datasets & candidate_datasets)
                if shared == 0 or shared <= best_shared:
                    continue
                if require_common_sample and \
                        not self._has_common_sample(cur, candidate_id, own_datasets):
                    continue
                best = candidate_id
                best_shared = shared
            cur.close()
        return best

    @staticmethod
    def _scalar_equal(left, right):
        '''Exact scalar comparison that also treats NaN as equal to NaN.'''
        if left == right:
            return True
        try:
            return bool(np.isnan(left) and np.isnan(right))
        except TypeError:
            return False

    @classmethod
    def _rows_equal(cls, left, right):
        '''Compare two sample rows (dicts of field name -> scalar).'''
        if left.keys() != right.keys():
            return False
        return all(cls._scalar_equal(left[name], right[name]) for name in left)

    @staticmethod
    def _row_matches_any(row, candidates, used):
        '''Find an unused candidate row equal to row.'''
        for index, candidate in enumerate(candidates):
            if used[index]:
                continue
            if DatabaseULog._rows_equal(row, candidate):
                used[index] = True
                return index
        return None

    @classmethod
    def _match_all(cls, rows, candidates):
        '''
        Greedily match every row in ``rows`` to an unused equal candidate.

        Returns (all_matched, matched_flags) where matched_flags refers to
        ``candidates`` and marks the entries that were consumed.
        '''
        used = [False] * len(candidates)
        all_matched = True
        for row in rows:
            if cls._row_matches_any(row, candidates, used) is None:
                all_matched = False
                break
        return all_matched, used

    @classmethod
    def _new_sample_indices(cls, old_data, new_data):
        '''
        Validate a supplement topic against the archived samples and return
        (indices of genuinely new samples, first conflict or None).

        At a timestamp that is not archived yet, every supplied row is new.
        At a timestamp that already exists the rows are compared as multisets:

        - if the archive holds a single distinct row shape there, every
          supplied row must equal it. Extra identical rows are new samples,
          a different row is a contradiction;
        - if the archive already holds several distinct rows there (event
          topics), the supplied rows may extend that set; if neither set
          contains the other, the files disagree;
        - rows at timestamp 0 are not individually time-stamped (initial
          values) and are treated as an unordered growing set.

        Topics whose format does not contain a unique timestamp (for example
        event topics where several rows share one timestamp) are supported:
        genuinely new rows at an existing timestamp are added to the archive.
        '''
        old_timestamps = old_data['timestamp']
        new_timestamps = new_data['timestamp']
        new_indices = []
        conflict = None

        old_by_time = {}
        for index, timestamp in enumerate(old_timestamps.tolist()):
            old_by_time.setdefault(int(timestamp), []).append(index)

        new_by_time = {}
        for index, timestamp in enumerate(new_timestamps.tolist()):
            new_by_time.setdefault(int(timestamp), []).append(index)

        for timestamp in sorted(new_by_time):
            new_rows = new_by_time[timestamp]
            old_rows = old_by_time.get(timestamp)
            if not old_rows:
                new_indices.extend(new_rows)
                continue

            old_rows_data = [
                {name: values[index] for name, values in old_data.items()}
                for index in old_rows
            ]
            new_rows_data = [
                {name: values[index] for name, values in new_data.items()}
                for index in new_rows
            ]

            if timestamp == 0:
                all_old_matched, used_in_new = cls._match_all(
                    old_rows_data, new_rows_data)
                all_new_matched, used_in_old = cls._match_all(
                    new_rows_data, old_rows_data)
                if all_old_matched:
                    new_indices.extend(
                        new_rows[index]
                        for index, matched in enumerate(used_in_new)
                        if not matched
                    )
                elif not all_new_matched:
                    new_indices.extend(
                        new_rows[index]
                        for index, matched in enumerate(used_in_old)
                        if not matched
                    )
                continue

            distinct_old = []
            for row in old_rows_data:
                if not any(cls._rows_equal(row, seen) for seen in distinct_old):
                    distinct_old.append(row)

            if len(distinct_old) == 1:
                archived_row = distinct_old[0]
                if not all(cls._rows_equal(row, archived_row) for row in new_rows_data):
                    if conflict is None or timestamp < conflict:
                        conflict = timestamp
                    continue
                _, used_in_new = cls._match_all(old_rows_data, new_rows_data)
                new_indices.extend(
                    new_rows[index]
                    for index, matched in enumerate(used_in_new)
                    if not matched
                )
                continue

            all_old_matched, used_in_new = cls._match_all(old_rows_data, new_rows_data)
            all_new_matched, _ = cls._match_all(new_rows_data, old_rows_data)

            if all_old_matched:
                new_indices.extend(
                    new_rows[index]
                    for index, matched in enumerate(used_in_new)
                    if not matched
                )
            elif not all_new_matched:
                if conflict is None or timestamp < conflict:
                    conflict = timestamp
        return new_indices, conflict

    @staticmethod
    def _merge_arrays(old_data, new_data, new_indices, field_names):
        '''Return timestamp-sorted arrays containing old plus the new samples.'''
        if not new_indices:
            return None
        old_timestamps = old_data['timestamp']
        new_timestamps = new_data['timestamp'][new_indices]
        timestamps = np.concatenate((old_timestamps, new_timestamps))
        order = np.argsort(timestamps, kind='stable')
        merged = {'timestamp': timestamps[order]}
        for name in field_names:
            if name == 'timestamp':
                continue
            values = np.concatenate((old_data[name], new_data[name][new_indices]))
            merged[name] = values[order]
        return merged

    @staticmethod
    def _values_json(data, field_name):
        '''Build the optional JSON representation used by save(append_json=True).'''
        timestamp_list = [str(s) for s in data['timestamp'].tolist()]
        values = data[field_name]
        return str(dict(zip(timestamp_list, values.tolist()))) \
            .replace('nan', 'null').replace('inf', 'null').replace("'", '"')

    def _load_dataset_rows(self, cur, dataset_id):
        '''Load a dataset's field metadata and BLOBs from the database.'''
        cur.execute('''
            SELECT Id, TopicName, DataType, ValueArray, ValueJson
            FROM ULogField
            WHERE DatasetId = ?
            ORDER BY Id
            ''', (dataset_id,))
        rows = cur.fetchall()
        fields = []
        field_ids = []
        data = {}
        has_json = False
        for field_id, field_name, data_type, value_bytes, value_json in rows:
            fields.append(DatabaseULog._FieldData(field_name=field_name, type_str=data_type))
            field_ids.append(field_id)
            dtype = DatabaseULog._UNPACK_TYPES[data_type][2]
            data[field_name] = np.frombuffer(value_bytes, dtype=dtype)
            has_json = has_json or value_json is not None
        return fields, field_ids, data, has_json

    def _validate_and_plan_merge(self, con, target_pk):
        '''
        Compare this log with the archived record without changing anything.

        Returns a plan describing new datasets and missing samples, or raises
        ULogMergeConflict with the first (in time) conflicting topic.

        Comparisons are made per topic instance: the stored format must be
        identical (same fields, types, order and timestamp column), and the
        overlapping samples must not contradict the archive. A topic that is
        not in the archive at all is simply new; its samples are not compared
        against other topics.
        '''
        cur = con.cursor()
        cur.execute('''
            SELECT Id, DatasetName, MultiId, MessageId, TimestampIndex
            FROM ULogDataset
            WHERE ULogId = ?
            ''', (target_pk,))
        existing_datasets = {}
        for dataset_id, name, multi_id, msg_id, timestamp_idx in cur.fetchall():
            fields, field_ids, data, has_json = self._load_dataset_rows(cur, dataset_id)
            existing_datasets[(name, multi_id)] = {
                'id': dataset_id,
                'msg_id': msg_id,
                'timestamp_idx': timestamp_idx,
                'fields': fields,
                'field_ids': field_ids,
                'data': data,
                'has_json': has_json,
            }

        conflicts = []
        new_datasets = []
        missing_samples = {}
        for dataset in self.data_list:
            key = (dataset.name, dataset.multi_id)
            existing = existing_datasets.get(key)
            if existing is None:
                new_datasets.append(dataset)
                continue

            existing_field_names = [field.field_name for field in existing['fields']]
            new_field_names = [field.field_name for field in dataset.field_data]
            format_ok = (
                existing['timestamp_idx'] == dataset.timestamp_idx and
                existing_field_names == new_field_names and
                all(left.type_str == right.type_str for left, right in
                    zip(existing['fields'], dataset.field_data))
            )
            if not format_ok:
                existing_timestamps = existing['data'].get('timestamp', [])
                new_timestamps = dataset.data.get('timestamp', [])
                if len(existing_timestamps) and len(new_timestamps):
                    # The format conflict is observable at the first moment
                    # at which both files claim samples for this topic.
                    timestamp = int(max(min(existing_timestamps), min(new_timestamps)))
                elif len(new_timestamps):
                    timestamp = int(min(new_timestamps))
                elif len(existing_timestamps):
                    timestamp = int(min(existing_timestamps))
                else:
                    timestamp = int(self._start_timestamp)
                conflicts.append((timestamp, dataset.name, dataset.multi_id,
                                  'stored and supplied topic formats differ'))
                continue

            new_indices, conflict = self._new_sample_indices(
                existing['data'], dataset.data)
            if conflict is not None:
                conflicts.append((conflict, dataset.name, dataset.multi_id,
                                  'stored and supplied samples differ'))
            missing_samples[key] = (existing, dataset, new_indices)

        if conflicts:
            timestamp, topic, multi_id, reason = min(
                conflicts, key=lambda item: (item[0], item[1], item[2]))
            raise ULogMergeConflict(topic, timestamp, multi_id=multi_id, reason=reason)
        cur.close()
        return existing_datasets, new_datasets, missing_samples

    def _merge_into(self, target_pk, append_json=False):
        '''
        Merge this log into an existing record as a supplement.

        All validation happens before the first write; the write itself is a
        single transaction, so a conflict leaves the archived record exactly
        as it was.
        '''
        with self._db() as con:
            cur = con.cursor()
            existing_datasets, new_datasets, missing_samples = \
                self._validate_and_plan_merge(con, target_pk)

            added_datasets = []
            updated_datasets = []
            added_samples = 0

            for dataset in new_datasets:
                cur.execute('''
                    INSERT INTO ULogDataset
                    (DatasetName, MultiId, MessageId, TimestampIndex, ULogId)
                    VALUES
                    (?, ?, ?, ?, ?)
                    ''', (
                        dataset.name,
                        dataset.multi_id,
                        dataset.msg_id,
                        dataset.timestamp_idx,
                        target_pk,
                    ))
                dataset_id = cur.lastrowid
                for field in dataset.field_data:
                    values = dataset.data[field.field_name]
                    values_bytes = values.tobytes()
                    values_json = None
                    json_placeholder = '?'
                    if append_json:
                        values_json = self._values_json(dataset.data, field.field_name)
                        json_placeholder = 'json(?)'
                    cur.execute(f'''
                        INSERT INTO ULogField
                        (TopicName, DataType, ValueArray, ValueJson, DatasetId)
                        VALUES
                        (?, ?, ?, {json_placeholder}, ?)
                        ''', (
                            field.field_name,
                            field.type_str,
                            values_bytes,
                            values_json,
                            dataset_id,
                        ))
                added_datasets.append((dataset.name, dataset.multi_id))
                added_samples += len(dataset.data.get('timestamp', []))

            for key, (existing, dataset, new_indices) in missing_samples.items():
                if not new_indices:
                    continue
                field_names = [field.field_name for field in existing['fields']]
                merged = self._merge_arrays(
                    existing['data'], dataset.data, new_indices, field_names)
                for field, field_id in zip(existing['fields'], existing['field_ids']):
                    values = merged[field.field_name]
                    values_bytes = values.tobytes()
                    if append_json or existing['has_json']:
                        values_json = self._values_json(merged, field.field_name)
                        json_placeholder = 'json(?)'
                    else:
                        values_json = None
                        json_placeholder = '?'
                    # Update by the stored field Id: the field name alone is
                    # not unique when nested types flatten to the same name.
                    cur.execute(f'''
                        UPDATE ULogField
                        SET ValueArray = ?, ValueJson = {json_placeholder}
                        WHERE DatasetId = ? AND Id = ?
                        ''', (
                            values_bytes,
                            values_json,
                            existing['id'],
                            field_id,
                        ))
                updated_datasets.append(key)
                added_samples += len(new_indices)

            # Timestamp-independent metadata is added only where the archived
            # record has no value yet. Existing values are never overwritten.
            cur.execute('SELECT Key FROM ULogMessageInfo WHERE ULogId = ?', (target_pk,))
            existing_info_keys = {row[0] for row in cur.fetchall()}
            cur.executemany('''
                INSERT INTO ULogMessageInfo
                (Key, Value, Typename, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(
                    key,
                    value,
                    self._msg_info_dict_types[key],
                    target_pk,
                ) for key, value in self.msg_info_dict.items()
                    if key not in existing_info_keys])

            cur.execute('SELECT Key FROM ULogInitialParameter WHERE ULogId = ?', (target_pk,))
            existing_param_keys = {row[0] for row in cur.fetchall()}
            cur.executemany('''
                INSERT INTO ULogInitialParameter
                (Key, Value, ULogId)
                VALUES
                (?, ?, ?)
                ''', [(
                    key,
                    value,
                    target_pk,
                ) for key, value in self.initial_parameters.items()
                    if key not in existing_param_keys])

            cur.execute('''
                SELECT DefaultType, Key
                FROM ULogDefaultParameter
                WHERE ULogId = ?
                ''', (target_pk,))
            existing_defaults = {(default_type, key) for default_type, key in cur.fetchall()}
            cur.executemany('''
                INSERT INTO ULogDefaultParameter
                (DefaultType, Key, Value, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(
                    default_type,
                    key,
                    value,
                    target_pk,
                ) for default_type, parameters in self._default_parameters.items()
                    for key, value in parameters.items()
                    if (default_type, key) not in existing_defaults])

            cur.execute('''
                SELECT Timestamp, Key, Value
                FROM ULogChangedParameter
                WHERE ULogId = ?
                ''', (target_pk,))
            existing_changes = {(timestamp, key, value) for timestamp, key, value
                                in cur.fetchall()}
            cur.executemany('''
                INSERT INTO ULogChangedParameter
                (Timestamp, Key, Value, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(
                    timestamp,
                    key,
                    value,
                    target_pk,
                ) for timestamp, key, value in self.changed_parameters
                    if (timestamp, key, value) not in existing_changes])

            cur.execute('''
                SELECT LogLevel, Timestamp, Message
                FROM ULogMessageLogging
                WHERE ULogId = ?
                ''', (target_pk,))
            existing_messages = {}
            for row in cur.fetchall():
                existing_messages[row] = existing_messages.get(row, 0) + 1
            new_messages = []
            for message in self.logged_messages:
                row = (message.log_level, message.timestamp, message.message)
                if existing_messages.get(row, 0) > 0:
                    existing_messages[row] -= 1
                else:
                    new_messages.append(row)
            cur.executemany('''
                INSERT INTO ULogMessageLogging
                (LogLevel, Timestamp, Message, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(level, timestamp, message, target_pk)
                      for level, timestamp, message in new_messages])

            cur.execute('''
                SELECT LogLevel, Timestamp, Tag, Message
                FROM ULogMessageLoggingTagged
                WHERE ULogId = ?
                ''', (target_pk,))
            existing_tagged = {}
            for row in cur.fetchall():
                existing_tagged[row] = existing_tagged.get(row, 0) + 1
            tagged_rows = []
            for tag, messages in self._logged_messages_tagged.items():
                for message in messages:
                    row = (message.log_level, message.timestamp, tag, message.message)
                    if existing_tagged.get(row, 0) > 0:
                        existing_tagged[row] -= 1
                    else:
                        tagged_rows.append(row)
            cur.executemany('''
                INSERT INTO ULogMessageLoggingTagged
                (LogLevel, Timestamp, Tag, Message, ULogId)
                VALUES
                (?, ?, ?, ?, ?)
                ''', [(level, timestamp, tag, message, target_pk)
                      for level, timestamp, tag, message in tagged_rows])

            cur.execute('''
                SELECT Timestamp, Duration
                FROM ULogMessageDropout
                WHERE ULogId = ?
                ''', (target_pk,))
            existing_dropouts = {}
            for row in cur.fetchall():
                existing_dropouts[row] = existing_dropouts.get(row, 0) + 1
            new_dropouts = []
            for dropout in self.dropouts:
                row = (dropout.timestamp, dropout.duration)
                if existing_dropouts.get(row, 0) > 0:
                    existing_dropouts[row] -= 1
                else:
                    new_dropouts.append(row)
            cur.executemany('''
                INSERT INTO ULogMessageDropout
                (Timestamp, Duration, ULogId)
                VALUES
                (?, ?, ?)
                ''', [(timestamp, duration, target_pk)
                      for timestamp, duration in new_dropouts])

            cur.execute('SELECT Name FROM ULogMessageFormat WHERE ULogId = ?', (target_pk,))
            existing_formats = {row[0] for row in cur.fetchall()}
            for name, message_format in self._message_formats.items():
                if name in existing_formats:
                    continue
                cur.execute('''
                    INSERT INTO ULogMessageFormat
                    (Name, ULogId)
                    VALUES
                    (?, ?)
                    ''', (name, target_pk))
                format_id = cur.lastrowid
                cur.executemany('''
                    INSERT INTO ULogMessageFormatField
                    (FieldType, ArraySize, Name, MessageId)
                    VALUES
                    (?, ?, ?, ?)
                    ''', [(*field, format_id) for field in message_format.fields])

            cur.execute('''
                SELECT Key
                FROM ULogMessageInfoMultiple
                WHERE ULogId = ?
                ''', (target_pk,))
            existing_multiple = {row[0] for row in cur.fetchall()}
            for key, lists in self.msg_info_multiple_dict.items():
                if key in existing_multiple:
                    continue
                cur.execute('''
                    INSERT INTO ULogMessageInfoMultiple
                    (Key, Typename, ULogId)
                    VALUES
                    (?, ?, ?)
                    ''', (key, self._msg_info_multiple_dict_types[key], target_pk))
                message_id = cur.lastrowid
                for list_index, message_list in enumerate(lists):
                    cur.execute('''
                        INSERT INTO ULogMessageInfoMultipleList
                        (SeriesIndex, MessageId)
                        VALUES
                        (?, ?)
                        ''', (list_index, message_id))
                    list_id = cur.lastrowid
                    cur.executemany('''
                        INSERT INTO ULogMessageInfoMultipleListElement
                        (SeriesIndex, Value, ListId)
                        VALUES
                        (?, ?, ?)
                        ''', [(
                            series_index,
                            value,
                            list_id,
                        ) for series_index, value in enumerate(message_list)])

            cur.execute('''
                SELECT StartTimestamp, LastTimestamp, SyncCount, HasSync
                FROM ULog
                WHERE Id = ?
                ''', (target_pk,))
            (stored_start, stored_last, stored_sync_count, stored_has_sync) = cur.fetchone()
            start_timestamp = min(stored_start, self._start_timestamp)
            last_timestamp = max(stored_last, self._last_timestamp)
            # Sync counters describe the file envelope, not the samples. Keep
            # them consistent with the stored bytes instead of silently
            # replacing the archived record's values.
            sync_count = max(stored_sync_count or 0, self._sync_seq_cnt)
            has_sync = bool(stored_has_sync) or bool(self._has_sync)
            cur.execute('''
                UPDATE ULog
                SET StartTimestamp = ?, LastTimestamp = ?, SyncCount = ?, HasSync = ?
                WHERE Id = ?
                ''', (start_timestamp, last_timestamp, sync_count, has_sync, target_pk))

            status = 'merged' if (added_datasets or added_samples) else 'contained'
            cur.execute('''
                INSERT OR REPLACE INTO ULogSupplement
                (ULogId, SHA256Sum, StartTimestamp, LastTimestamp, Status, Reason,
                 ConflictTopic, ConflictMultiId, ConflictTimestamp,
                 AddedDatasetCount, AddedSampleCount, MergedAt)
                VALUES
                (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
                ''', (
                    target_pk,
                    self._sha256sum,
                    self._start_timestamp,
                    self._last_timestamp,
                    status,
                    len(added_datasets),
                    added_samples,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ))
            cur.close()

        self._start_timestamp = start_timestamp
        self._last_timestamp = last_timestamp
        self._sync_seq_cnt = sync_count
        self._has_sync = has_sync
        self._last_merge_report = {
            'status': status,
            'primary_key': target_pk,
            'added_datasets': added_datasets,
            'updated_datasets': updated_datasets,
            'added_samples': added_samples,
            'sha256sum': self._sha256sum,
        }

    @property
    def last_merge_report(self):
        '''Details of the most recent save() that was treated as a supplement.'''
        return getattr(self, '_last_merge_report', None)

    def _merge_evidence(self, cur, target_pk):
        '''
        Check the evidence that links this log to an existing record.

        Returns (shared_topic_count, reason). A reason of None means the
        evidence is sufficient; otherwise it explains what is missing.
        '''
        cur.execute('''
            SELECT StartTimestamp, LastTimestamp
            FROM ULog
            WHERE Id = ?
            ''', (target_pk,))
        row = cur.fetchone()
        if row is None:
            return 0, f'no ULog with Id={target_pk}'
        stored_start, stored_last = row
        if stored_start > self._last_timestamp or stored_last < self._start_timestamp:
            return 0, 'the time ranges do not overlap'

        cur.execute('''
            SELECT Key, Value
            FROM ULogMessageInfo
            WHERE ULogId = ?
            ''', (target_pk,))
        if not self._identity_matches(dict(cur.fetchall()), self.msg_info_dict):
            return 0, 'the device identity does not match'

        own_datasets = self._dataset_keys()
        cur.execute('''
            SELECT DatasetName, MultiId
            FROM ULogDataset
            WHERE ULogId = ?
            ''', (target_pk,))
        stored_datasets = {(name, multi_id) for name, multi_id in cur.fetchall()}
        shared = len(own_datasets & stored_datasets)
        if shared == 0:
            return 0, 'the files have no topic instance in common'
        if not self._has_common_sample(cur, target_pk, own_datasets):
            return shared, 'the files share no identical sample, so they are not the same acquisition'
        return shared, None

    def merge_into(self, primary_key, append_json=False, require_evidence=True):
        '''
        Merge this log into an existing database record as a supplement.

        Use this when the target record is known explicitly. With
        require_evidence=True (the default) the same positive checks as save()
        apply: overlapping time range, matching device identity, a shared
        topic and at least two identical samples in different topics. Without
        such evidence ULogMergeEvidenceError is raised and nothing is written.

        A contradictory supplement raises ULogMergeConflict identifying the
        first conflicting topic and timestamp; the stored record is left
        exactly as it was.
        '''
        if self._pk is not None:
            raise KeyError('Cannot merge a log that is already in the database')
        already = DatabaseULog.primary_key_from_sha256sum(self._db, self._sha256sum)
        if already is not None:
            raise KeyError(f'Hash {self._sha256sum} already in database with Id={already}')
        already = DatabaseULog.primary_key_from_supplement_sha256sum(
            self._db, self._sha256sum)
        if already is not None:
            self._pk = already
            return
        with self._db() as con:
            cur = con.cursor()
            if require_evidence:
                shared, reason = self._merge_evidence(cur, primary_key)
                if reason is not None:
                    cur.close()
                    raise ULogMergeEvidenceError(
                        f'Cannot merge into Id={primary_key}: {reason}')
                del shared
            cur.close()
        self._merge_into(primary_key, append_json=append_json)
        self._pk = primary_key

    def save(self, append_json=False, merge=True):
        '''
        Save the DatabaseULog to the database. Throws a KeyError if the primary
        key is already in the database.

        The datasets are stored in a sqlite BLOB for fast retrieval, but if
        append_json=True, then datasets are additionally stored in a JSON
        field. This allows them to be directly queried using the sqlite
        function json_each, but increases the writing time and database size.

        If a file with a new digest can be shown to belong to the same
        acquisition as an existing record, it is treated as a supplement: only
        datasets and samples that are genuinely missing are inserted, and
        everything else already in the database is kept byte-for-byte. The
        evidence is an overlapping time range, a matching device identity, a
        shared topic instance and at least one identical sample. Identical
        start/end timestamps alone are explicitly not enough, so a different
        run with the same envelope becomes its own record. A supplement that
        contradicts an archived sample raises ULogMergeConflict and leaves the
        record untouched. Re-submitting a file that was already merged is a
        no-op and returns the original record. Pass merge=False to force the
        old behaviour of always inserting a new row.
        '''
        if self._pk is not None:
            raise KeyError('Cannot save logs that are already in the database')

        pk_from_hash = DatabaseULog.primary_key_from_sha256sum(self._db, self._sha256sum)
        if pk_from_hash is not None:
            self._pk = pk_from_hash
            raise KeyError(f'Hash {self._sha256sum} already in database with Id={pk_from_hash}')

        # A file that was already merged before is idempotent: re-submitting
        # the complete export must not create another row or another copy of
        # its samples.
        pk_from_supplement = DatabaseULog.primary_key_from_supplement_sha256sum(
            self._db, self._sha256sum)
        if pk_from_supplement is not None:
            self._pk = pk_from_supplement
            return

        if merge:
            merge_target = self._find_merge_target()
            if merge_target is not None:
                self._merge_into(merge_target, append_json=append_json)
                self._pk = merge_target
                return

        with self._db() as con:
            cur = con.cursor()

            # ULog metadata
            cur.execute('''
                INSERT INTO ULog
                (FileVersion, StartTimestamp, LastTimestamp, CompatFlags, IncompatFlags, SyncCount, HasSync, SHA256Sum)
                VALUES
                (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                    self._file_version,
                    self._start_timestamp,
                    self._last_timestamp,
                    ''.join([chr(n) for n in self._compat_flags]),
                    ''.join([chr(n) for n in self._incompat_flags]),
                    self._sync_seq_cnt,
                    self._has_sync,
                    self._sha256sum,
                )
            )
            self._pk = cur.lastrowid

            # appended_offsets
            cur.executemany('''
                INSERT INTO ULogAppendedOffsets
                (SeriesIndex, Offset, ULogId)
                VALUES
                (?, ?, ?)
                ''', [(
                    list_index,
                    offset,
                    self._pk,
                ) for list_index, offset in enumerate(self._appended_offsets)])

            # data_list
            for dataset in self.data_list:
                cur.execute('''
                    INSERT INTO ULogDataSet
                    (DatasetName, MultiId, MessageId, TimestampIndex, ULogId)
                    VALUES
                    (?, ?, ?, ?, ?)
                    ''', (
                        dataset.name,
                        dataset.multi_id,
                        dataset.msg_id,
                        dataset.timestamp_idx,
                        self._pk,
                    )
                )
                dataset_id = cur.lastrowid
                for field in dataset.field_data:
                    values = dataset.data[field.field_name]
                    values_bytes = values.tobytes()
                    if append_json:
                        # Precision is only good enough up to a few decimals,
                        # depending on the default float formatter. The
                        # function np.array2string was tested, but was slower.
                        # Also note that doing the slow json.dumps is also
                        # unnecessary since we know that the object to be
                        # formatted is an array.
                        timestamp_list = [str(s) for s in dataset.data['timestamp'].tolist()]
                        values_json = str(
                            dict(zip(timestamp_list, values.tolist()))
                        ).replace('nan', 'null').replace('inf', 'null').replace("'", '"')
                        json_placeholder = 'json(?)'  # Saves some space
                    else:
                        values_json = None
                        json_placeholder = '?'

                    cur.execute(f'''
                        INSERT INTO ULogField
                        (TopicName, DataType, ValueArray, ValueJson, DatasetId)
                        VALUES
                        (?, ?, ?, {json_placeholder}, ?)
                        ''', (
                            field.field_name,
                            field.type_str,
                            values_bytes,
                            values_json,
                            dataset_id,
                        )
                    )

            # dropouts
            cur.executemany('''
                INSERT INTO ULogMessageDropout
                (Timestamp, Duration, ULogId)
                VALUES
                (?, ?, ?)
                ''', [(
                    dropout.timestamp,
                    dropout.duration,
                    self._pk,
                ) for dropout in self._dropouts])

            # logged_messages
            cur.executemany('''
                INSERT INTO ULogMessageLogging
                (LogLevel, Timestamp, Message, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(
                    message.log_level,
                    message.timestamp,
                    message.message,
                    self._pk,
                ) for message in self._logged_messages])

            # logged_messages_tagged
            for tag, messages in self._logged_messages_tagged.items():
                cur.executemany('''
                    INSERT INTO ULogMessageLoggingTagged
                    (LogLevel, Timestamp, Tag, Message, ULogId)
                    VALUES
                    (?, ?, ?, ?, ?)
                    ''', [(
                        message.log_level,
                        message.timestamp,
                        tag,
                        message.message,
                        self._pk,
                    ) for message in messages])

            # message_formats
            for name, message_format in self._message_formats.items():
                cur.execute('''
                    INSERT INTO ULogMessageFormat
                    (Name, ULogId)
                    VALUES
                    (?, ?)
                    ''', (name, self._pk))
                format_id = cur.lastrowid
                cur.executemany('''
                    INSERT INTO ULogMessageFormatField
                    (FieldType, ArraySize, Name, MessageId)
                    VALUES
                    (?, ?, ?, ?)
                    ''', [(*field, format_id) for field in message_format.fields])

            # msg_info_dict
            cur.executemany('''
                INSERT INTO ULogMessageInfo
                (Key, Value, Typename, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(
                    key,
                    value,
                    self._msg_info_dict_types[key],
                    self._pk,
                ) for key, value in self.msg_info_dict.items()])

            # msg_info_multiple_dict
            for key, lists in self.msg_info_multiple_dict.items():
                cur.execute('''
                    INSERT INTO ULogMessageInfoMultiple
                    (Key, Typename, ULogId)
                    VALUES
                    (?, ?, ?)
                    ''', (key, self._msg_info_multiple_dict_types[key], self._pk))
                message_id = cur.lastrowid
                for list_index, message_list in enumerate(lists):
                    cur.execute('''
                        INSERT INTO ULogMessageInfoMultipleList
                        (SeriesIndex, MessageId)
                        VALUES
                        (?, ?)
                        ''', (list_index, message_id))
                    list_id = cur.lastrowid
                    cur.executemany('''
                        INSERT INTO ULogMessageInfoMultipleListElement
                        (SeriesIndex, Value, ListId)
                        VALUES
                        (?, ?, ?)
                        ''', [(
                            series_index,
                            value,
                            list_id,
                        ) for series_index, value in enumerate(message_list)])

            # initial_parameters
            cur.executemany('''
                INSERT INTO ULogInitialParameter
                (Key, Value, ULogId)
                VALUES
                (?, ?, ?)
                ''', [(
                    key,
                    value,
                    self._pk,
                ) for key, value in self.initial_parameters.items()])

            # _default_parameters
            for default_type, parameters in self._default_parameters.items():
                cur.executemany('''
                    INSERT INTO ULogDefaultParameter
                    (DefaultType, Key, Value, ULogId)
                    VALUES
                    (?, ?, ?, ?)
                    ''', [(
                        default_type,
                        key,
                        value,
                        self._pk,
                    ) for key, value in parameters.items()])

            # changed_parameters
            cur.executemany('''
                INSERT INTO ULogChangedParameter
                (Timestamp, Key, Value, ULogId)
                VALUES
                (?, ?, ?, ?)
                ''', [(
                    timestamp,
                    key,
                    value,
                    self._pk,
                ) for timestamp, key, value in self.changed_parameters])

            cur.close()

    def delete(self):
        '''
        Deletes the ULog row and cascading rows from the database.
        '''
        if self._pk is None:
            raise KeyError('Cannot delete logs that are not in the database')

        with self._db() as con:
            cur = con.cursor()
            cur.execute('''
                DELETE FROM ULog
                WHERE Id = ?
            ''', (self._pk,)
            )
            cur.close()
        self._pk = None

    class DatabaseData(ULog.Data):
        '''
        Overrides the ULog.Data class since its constructor only
        reads ULog.MessageLogAdded objects, and we want to specify
        the fields directly.
        '''
        # pylint: disable=super-init-not-called,too-many-arguments
        def __init__(self, name, multi_id, msg_id, timestamp_idx, field_data, data=None):
            self.name = name
            self.multi_id = multi_id
            self.msg_id = msg_id
            self.timestamp_idx = timestamp_idx
            self.field_data = field_data
            self.data = data

    class DatabaseMessageDropout(ULog.MessageDropout):
        '''
        Overrides the ULog.MessageDropout class since its
        constructor is not suitable for out purpose.
        '''
        # pylint: disable=super-init-not-called
        def __init__(self, timestamp, duration):
            self.timestamp = timestamp
            self.duration = duration

    class DatabaseMessageFormat(ULog.MessageFormat):
        '''
        Overrides the ULog.MessageFormat class since its
        constructor is not suitable for out purpose.
        '''
        # pylint: disable=super-init-not-called
        def __init__(self, name, fields):
            self.name = name
            self.fields = fields

    class DatabaseMessageLogging(ULog.MessageLogging):
        '''
        Overrides the ULog.MessageLogging class since its
        constructor is not suitable for out purpose.
        '''
        # pylint: disable=super-init-not-called
        def __init__(self, log_level, timestamp, message):
            self.log_level = log_level
            self.timestamp = timestamp
            self.message = message

    class DatabaseMessageLoggingTagged(ULog.MessageLoggingTagged):
        '''
        Overrides the ULog.MessageLoggingTagged class since its
        constructor is not suitable for our purpose.
        '''
        # pylint: disable=super-init-not-called
        def __init__(self, log_level, tag, timestamp, message):
            self.log_level = log_level
            self.tag = tag
            self.timestamp = timestamp
            self.message = message
