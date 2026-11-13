'''
Tests the ULog class
'''

import os
import inspect
import unittest
import tempfile
import struct
from io import BytesIO

from ddt import ddt, data
import numpy as np

import pyulog

TEST_PATH = os.path.dirname(os.path.abspath(
    inspect.getfile(inspect.currentframe())))

@ddt
class TestULog(unittest.TestCase):
    '''
    Tests the ULog class
    '''

    @data('sample')
    def test_comparison(self, base_name):
        '''
        Test that the custom comparison method works as expected.
        '''
        ulog_file_name = os.path.join(TEST_PATH, base_name + '.ulg')
        ulog1 = pyulog.ULog(ulog_file_name)
        ulog2 = pyulog.ULog(ulog_file_name)
        assert ulog1 == ulog2
        assert ulog1 is not ulog2

        # make them different in arbitrary field
        ulog1.data_list[0].data['timestamp'][0] += 1
        assert ulog1 != ulog2


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

            original = pyulog.ULog(ulog_file_name)
            original.write_ulog(written_ulog_file_name)
            copied = pyulog.ULog(written_ulog_file_name)

        # Some fields are not copied but dropped, so we cheat by modifying the original
        original._sync_seq_cnt = 0  # pylint: disable=protected-access
        original._appended_offsets = []  # pylint: disable=protected-access
        original._incompat_flags[0] &= 0xFE  # pylint: disable=protected-access

        assert copied == original

    @data('sample')
    def test_write_ulog_memory(self, base_name):
        '''
        Test that the write_ulog method can write bytes to memory.
        '''
        ulog_file_name = os.path.join(TEST_PATH, base_name + '.ulg')
        original = pyulog.ULog(ulog_file_name)
        with BytesIO() as bytes_handle:
            original.write_ulog(bytes_handle)
            bytes_handle.seek(0)
            copied = pyulog.ULog(bytes_handle)

        for original_key, original_value in original.__dict__.items():
            copied_value = getattr(copied, original_key)
            if original_key == '_sync_seq_cnt':
                # Sync messages are counted on parse, but otherwise dropped, so
                # we don't rewrite them
                assert copied_value == 0
            elif original_key == '_appended_offsets':
                # Abruptly ended messages just before offsets are dropped, so
                # we don't rewrite appended offsets
                assert copied_value == []
            elif original_key == '_incompat_flags':
                # Same reasoning on incompat_flags[0] as for '_appended_offsets'
                assert copied_value[0] == original_value[0] & 0xFE # pylint: disable=unsubscriptable-object
                assert copied_value[1:] == original_value[1:] # pylint: disable=unsubscriptable-object
            else:
                assert copied_value == original_value

    @staticmethod
    def _build_ulog(*messages):
        '''
        Build a minimal ULog file in memory from (msg_type, payload) tuples.
        '''
        content = pyulog.ULog.HEADER_BYTES + b'\x01' + struct.pack('<Q', 0)
        for msg_type, payload in messages:
            content += struct.pack('<HB', len(payload), ord(msg_type)) + payload
        return BytesIO(content)

    def test_invalid_header(self):
        '''
        Test that a file with an invalid header raises a ULogException.
        '''
        for content in [b'ULog', b'\x00' * 16]:
            with self.assertRaises(pyulog.ULogException):
                pyulog.ULog(BytesIO(content))

    @data(b'\x02' + bytes(7), # unknown flag in the first byte
          b'\x00\x01' + bytes(6)) # any flag in the other bytes
    def test_unknown_incompat_flag(self, incompat_flags):
        '''
        Test that an unknown incompatible flag raises a ULogIncompatibleFlagException.
        '''
        flag_bits = bytes(8) + incompat_flags + bytes(3 * 8)
        with self.assertRaises(pyulog.ULogIncompatibleFlagException):
            pyulog.ULog(self._build_ulog(('B', flag_bits)))

    def test_corrupt_definitions(self):
        '''
        Test that malformed definitions are flagged as file corruption instead of raising.
        '''
        invalid_array_size = b'foo:uint8_t[x] a;'
        bogus_appended_offset = bytes(8) + b'\x01' + bytes(7) + struct.pack('<QQQ', 1 << 63, 0, 0)
        for msg_type, payload in [('F', invalid_array_size), ('B', bogus_appended_offset)]:
            ulog = pyulog.ULog(self._build_ulog((msg_type, payload)))
            assert ulog.file_corruption

    def test_strict_string_parsing(self):
        '''
        Test that string parsing errors still raise with disable_str_exceptions=False,
        both for an info message (definitions) and a logged string (data section).
        '''
        info = bytes([10]) + b'char[2] ab' + b'\xff\xfe'
        logged_string = b'6' + bytes(8) + b'\xff\xfe'
        for msg_type, payload in [('I', info), ('L', logged_string)]:
            with self.assertRaises(UnicodeDecodeError):
                pyulog.ULog(self._build_ulog((msg_type, payload)), disable_str_exceptions=False)
            assert not pyulog.ULog(self._build_ulog((msg_type, payload))).file_corruption

class TestDataStringFields(unittest.TestCase):
    '''
    Tests the ULog.Data helpers that combine char array fields into strings.
    '''

    def _make_data(self, field_data, data_dict):
        ''' Build a ULog.Data object without going through a log file. '''
        data_obj = object.__new__(pyulog.ULog.Data)
        data_obj.name = 'test_topic'
        data_obj.field_data = [pyulog.ULog._FieldData(name, type_str)  # pylint: disable=protected-access
                               for name, type_str in field_data]
        data_obj.data = data_dict
        return data_obj

    def test_string_field_from_log_file(self):
        '''
        The transponder_report topic of the px4 events sample log has a
        char[9] 'callsign' field, stored as 9 individual columns.
        '''
        ulog_file_name = os.path.join(TEST_PATH, 'sample_px4_events.ulg')
        ulog = pyulog.ULog(ulog_file_name)
        topic = ulog.get_dataset('transponder_report')
        assert topic.list_string_fields() == ['callsign']
        strings = topic.get_string_field('callsign')
        assert len(strings) == len(topic.data['timestamp'])
        # compare against a manual combination of the char columns
        columns = [topic.data['callsign[{}]'.format(i)] for i in range(9)]
        for sample, string in enumerate(strings):
            raw = bytes(int(column[sample]) & 0xff for column in columns)
            assert string == raw.split(b'\x00', 1)[0].decode('utf-8', 'replace')

    def test_string_field_conversion(self):
        ''' Char columns are joined per sample and cut at the first NUL. '''
        # 2 samples of a char[5] field: 'ab' (NUL-padded), then a sample
        # with an invalid utf-8 byte: 'c\xffd'
        sample_bytes = [b'ab\x00zz', bytes([0x63, 0xff, 0x64, 0x00, 0x00])]
        columns = {}
        for i in range(5):
            column = np.array([sample[i] for sample in sample_bytes],
                              dtype=np.uint8)
            columns['text[{}]'.format(i)] = column.view(np.int8)
        field_data = [('text[{}]'.format(i), 'char') for i in range(5)]
        data_obj = self._make_data(field_data, columns)
        assert data_obj.list_string_fields() == ['text']
        strings = data_obj.get_string_field('text')
        assert list(strings) == ['ab', 'c\ufffdd']

    def test_plain_char_and_non_char_fields(self):
        ''' A plain char field is a 1-char string field; other types are not
        string fields and requesting them raises a KeyError. '''
        field_data = [('timestamp', 'uint64_t'), ('flag', 'char')]
        data_dict = {'timestamp': np.array([1, 2], dtype=np.uint64),
                     'flag': np.array([ord('y'), ord('n')], dtype=np.int8)}
        data_obj = self._make_data(field_data, data_dict)
        assert data_obj.list_string_fields() == ['flag']
        assert list(data_obj.get_string_field('flag')) == ['y', 'n']
        with self.assertRaises(KeyError):
            data_obj.get_string_field('timestamp')
        with self.assertRaises(KeyError):
            data_obj.get_string_field('does_not_exist')


class TestFilteredWriteULog(unittest.TestCase):
    '''
    Tests the privacy-preserving, whitelist/time-window export of ULog.
    Exports are always re-read with a plain ULog to prove interoperability.
    '''

    def _roundtrip(self, ulog, **kwargs):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, True))
        out_name = os.path.join(tmpdir, 'filtered.ulg')
        report = ulog.write_ulog(out_name, **kwargs)
        return report, pyulog.ULog(out_name), out_name

    @staticmethod
    def _window_mask(data, start, end):
        timestamps = data.data['timestamp']
        return (timestamps >= start) & (timestamps < end)

    def test_window_values_types_and_order(self):
        ''' Public samples keep their values, types and in-window order. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        start, end = 120000000, 130000000
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={'vehicle_attitude': ['timestamp', 'rollspeed', 'q']},
            time_window=(start, end))

        assert report.topics == {'vehicle_attitude': [0]}
        original = ulog.get_dataset('vehicle_attitude')
        copied = exported.get_dataset('vehicle_attitude')
        mask = self._window_mask(original, start, end)
        for field_name in ['timestamp', 'rollspeed', 'q[0]', 'q[1]', 'q[2]', 'q[3]']:
            assert np.array_equal(original.data[field_name][mask],
                                  copied.data[field_name])
        assert [field.type_str for field in copied.field_data] == \
            [field.type_str for field in original.field_data if
             field.field_name in ('timestamp', 'rollspeed', 'q[0]', 'q[1]',
                                  'q[2]', 'q[3]')]
        assert report.start_timestamp == original.data['timestamp'][mask][0]
        assert exported.start_timestamp == ulog.start_timestamp

    def test_private_fields_are_never_written(self):
        ''' Serial numbers and callsigns stay out even when requested. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample_px4_events.ulg'))
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={
                'battery_status': ['timestamp', 'voltage_v', 'serial_number'],
                'transponder_report': ['timestamp', 'lat', 'lon', 'callsign',
                                       'uas_id'],
            },
            time_window=(1710773350000000, 1710773400000000))

        assert report.omitted_sensitive_fields['battery_status'] == ['serial_number']
        assert report.omitted_sensitive_fields['transponder_report'] == \
            ['callsign', 'uas_id']
        for data_set in exported.data_list:
            names = [field.field_name for field in data_set.field_data]
            assert 'serial_number' not in names
            assert 'callsign[0]' not in names
            assert 'uas_id[0]' not in names
        # A caller asking only for private fields retires the topic.
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={'battery_status': ['serial_number']})
        assert 'battery_status' not in report.topics
        assert report.skipped_topics['battery_status']

    def test_parameter_state_at_cut_point(self):
        ''' Definition parameters describe the state at the window start. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        start, end = 158196367, 162054776
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={'vehicle_attitude': ['timestamp', 'q']},
            time_window=(start, end))

        # The original changes COM_AUTOS_PAR to 0 exactly at the cut point.
        assert exported.initial_parameters['COM_AUTOS_PAR'] == 0
        assert exported.initial_parameters['MPC_Z_VEL_MAX_DN'] == 1.0
        assert exported.changed_parameters == []
        assert report.parameters['COM_AUTOS_PAR'] == 0
        # Parameters that occur later are replayed inside the window.
        later_start, later_end = 160000000, 180000000
        _, exported, _ = self._roundtrip(
            ulog, field_whitelist={'vehicle_attitude': ['timestamp', 'q']},
            time_window=(later_start, later_end))
        assert exported.initial_parameters['COM_AUTOS_PAR'] == 0
        changes = [(name, value) for timestamp, name, value in
                   exported.changed_parameters if name == 'COM_AUTOS_PAR']
        assert changes == [('COM_AUTOS_PAR', 1), ('COM_AUTOS_PAR', 0),
                           ('COM_AUTOS_PAR', 1)]

    def test_sensitive_parameters_and_info(self):
        ''' Device identity parameters and info stay out of the definition. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample_px4_events.ulg'))
        report, exported, _ = self._roundtrip(
            ulog, topics=['vehicle_attitude'], include_info=True)
        for name in exported.initial_parameters:
            assert not pyulog.ULog._is_sensitive_name(name)
        assert 'CAL_GYRO0_ID' not in exported.initial_parameters
        assert 'COM_FLIGHT_UUID' not in exported.initial_parameters
        assert 'sys_os_name' in exported.msg_info_dict
        assert exported.msg_info_dict.get('sys_name') == 'PX4'

    def test_nested_definition_is_reduced_readably(self):
        ''' A nested type referencing private leaves gets its own layout. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample_px4_events.ulg'))
        start, end = 1710773350000000, 1710773400000000
        report, exported, out_name = self._roundtrip(
            ulog, field_whitelist={'esc_status': ['timestamp', 'esc[1].esc_rpm',
                                                  'esc[2].esc_voltage']},
            time_window=(start, end))

        esc_format = exported.message_formats['esc_status']
        nested_types = [field_type for field_type, _, _ in esc_format.fields
                        if field_type not in pyulog.ULog._UNPACK_TYPES]
        assert len(nested_types) == 1
        assert nested_types[0] != 'esc_report'
        assert nested_types[0] in exported.message_formats
        assert exported.get_dataset('esc_status').field_data[1].field_name == \
            'esc[0].esc_rpm'
        original = ulog.get_dataset('esc_status')
        copied = exported.get_dataset('esc_status')
        mask = self._window_mask(original, start, end)
        assert np.array_equal(original.data['esc[1].esc_rpm'][mask],
                              copied.data['esc[1].esc_rpm'])
        assert np.array_equal(original.data['esc[2].esc_voltage'][mask],
                              copied.data['esc[2].esc_voltage'])
        # Elements that were not selected stay rectangular but are zeroed.
        assert np.all(copied.data['esc[0].esc_rpm'] == 0)
        assert np.all(copied.data['esc[2].esc_rpm'] == 0)
        assert report.zero_filled_fields['esc_status']
        # Cross-check with the text parser: the definition is plain ULog.
        with open(out_name, 'rb') as file_handle:
            content = file_handle.read()
        assert b'esc_report' in content
        assert b'esc_status:' in content

    def test_unretainable_topics_are_reported(self):
        ''' Unknown topics, empty windows, and private-only topics are listed. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={'vehicle_attitude': ['timestamp', 'q'],
                                   'does_not_exist': ['x']},
            time_window=(1, 2))
        assert report.topics == {}
        assert report.skipped_topics['does_not_exist'] == 'topic not found'
        assert report.empty_topics['vehicle_attitude[0]'] == \
            'no samples in time window'
        assert exported.data_list == []
        assert 'vehicle_attitude' in exported.message_formats

        # A named topic with a reduced definition but no samples in the window
        # keeps its definition so the receiver can still decode it.
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={'position_setpoint_triplet':
                                   ['timestamp', 'current.lat', 'current.lon']},
            time_window=(120000000, 130000000))
        assert 'position_setpoint_triplet' in exported.message_formats
        assert report.empty_topics['position_setpoint_triplet'] == \
            'no samples in time window'
        assert exported.data_list == []

    def test_unmatched_fields_reported(self):
        ''' Whitelist entries absent from the format are visible to the caller. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        report, _, _ = self._roundtrip(
            ulog, field_whitelist={'vehicle_attitude': ['timestamp', 'q',
                                                        'no_such_field']})
        assert report.unmatched_fields['vehicle_attitude'] == ['no_such_field']

    def test_time_aliases_and_invalid_windows(self):
        ''' Seconds and microsecond aliases select the same samples. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        report_sec, exported_sec, _ = self._roundtrip(
            ulog, topics=['vehicle_attitude'], time_s=120, time_e=130)
        report_us, exported_us, _ = self._roundtrip(
            ulog, topics=['vehicle_attitude'], start_timestamp=120000000,
            end_timestamp=130000000)
        assert np.array_equal(exported_sec.get_dataset('vehicle_attitude').data['timestamp'],
                              exported_us.get_dataset('vehicle_attitude').data['timestamp'])
        assert report_sec.topics == report_us.topics
        with self.assertRaises(ValueError):
            ulog.write_ulog(tempfile.mktemp(suffix='.ulg'), topics=['vehicle_attitude'],
                            time_start=130, time_end=120)
        with self.assertRaises(ValueError):
            ulog.write_ulog(tempfile.mktemp(suffix='.ulg'), topics=['vehicle_attitude'],
                            time_start=1, time_s=2)

    def test_implicit_timestamp_and_array_prefix(self):
        ''' timestamp is added implicitly, array prefixes select all elements. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        report, exported, _ = self._roundtrip(
            ulog, field_whitelist={'sensor_combined': ['gyro_rad']})
        assert report.written_fields['sensor_combined'] == \
            ['timestamp', 'gyro_rad[0]', 'gyro_rad[1]', 'gyro_rad[2]']
        original = ulog.get_dataset('sensor_combined')
        copied = exported.get_dataset('sensor_combined')
        for index in range(3):
            field = 'gyro_rad[{}]'.format(index)
            assert np.array_equal(original.data[field], copied.data[field])

    def test_unfiltered_write_is_unchanged(self):
        ''' Omitting all filters keeps the historical write_ulog behaviour. '''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, True))
        out_name = os.path.join(tmpdir, 'copy.ulg')
        assert ulog.write_ulog(out_name) is None
        assert pyulog.ULog(out_name) == ulog


# vim: set et fenc=utf-8 ft=python ff=unix sts=4 sw=4 ts=4
