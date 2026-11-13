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


# vim: set et fenc=utf-8 ft=python ff=unix sts=4 sw=4 ts=4


class TestNestedColumns(unittest.TestCase):
    '''
    Tests flattening of nested message formats into stable business columns.
    '''

    SUB_FORMAT = 'uint8_t x;uint8_t[2] arr;uint8_t[2] _padding0;'

    @staticmethod
    def _build_log(formats, subscriptions, data_messages):
        content = pyulog.ULog.HEADER_BYTES + b'\x01' + struct.pack('<Q', 0)
        for msg_type, payload in formats:
            content += struct.pack('<HB', len(payload), ord(msg_type)) + payload
        for msg_type, payload in subscriptions:
            content += struct.pack('<HB', len(payload), ord(msg_type)) + payload
        for msg_type, payload in data_messages:
            content += struct.pack('<HB', len(payload), ord(msg_type)) + payload
        return BytesIO(content)

    @classmethod
    def _format(cls, name, fields):
        return ('F', bytes(name + ':' + fields, 'utf-8'))

    @classmethod
    def _subscription(cls, msg_id, name):
        return ('A', struct.pack('<BH', 0, msg_id) + bytes(name, 'utf-8'))

    @classmethod
    def _data(cls, msg_id, payload):
        return ('D', struct.pack('<H', msg_id) + payload)

    @classmethod
    def _sub_payload(cls, x, first, second):
        return struct.pack('<B2B2B', x, first, second, 0, 0)

    def _parse(self, outer_fields, payload):
        formats = [self._format('sub', self.SUB_FORMAT),
                   self._format('outer', outer_fields)]
        subscriptions = [self._subscription(7, 'outer')]
        data_messages = [self._data(7, payload)]
        return pyulog.ULog(self._build_log(formats, subscriptions, data_messages))

    def test_nested_paths_and_array_indices(self):
        ''' Array elements keep their index and full source path. '''
        payload = (struct.pack('<Q', 123) +
                   self._sub_payload(1, 9, 10) +
                   self._sub_payload(2, 11, 12) +
                   b'\x00\x00' + b'\xaa\xbb\xcc')
        ulog = self._parse('uint64_t timestamp;sub a;sub b;'
                           'uint8_t[2] _padding0;', payload)
        dataset = ulog.get_dataset('outer')

        assert dataset.list_columns() == [
            'timestamp', 'a.arr[0]', 'a.arr[1]', 'a.x',
            'b.arr[0]', 'b.arr[1]', 'b.x']

        arr_field = dataset.get_field_data('a.arr[1]')
        assert arr_field.array_index == 1
        assert arr_field.source_path == ('a', 'arr[1]')
        assert arr_field.leaf_name == 'arr'
        assert arr_field.is_padding is False

        # Byte offsets are taken from the declared wire layout.
        assert arr_field.offset == 10
        assert dataset.declared_size == 20

    def test_same_named_leaves_do_not_overwrite(self):
        ''' Leaves with the same name in two submessages are separate columns. '''
        payload = (struct.pack('<Q', 123) +
                   self._sub_payload(1, 9, 10) +
                   self._sub_payload(2, 11, 12) +
                   b'\x00\x00')
        ulog = self._parse('uint64_t timestamp;sub a;sub b;'
                           'uint8_t[2] _padding0;', payload)
        dataset = ulog.get_dataset('outer')

        assert dataset.data['a.x'][0] == 1
        assert dataset.data['b.x'][0] == 2
        assert dataset.data['a.arr[0]'][0] == 9
        assert dataset.data['b.arr[0]'][0] == 11

    def test_padding_is_not_a_business_column(self):
        ''' Alignment padding is parsed for layout but never exposed as data. '''
        payload = (struct.pack('<Q', 123) +
                   self._sub_payload(1, 9, 10) +
                   b'\x00\x00')
        ulog = self._parse('uint64_t timestamp;sub a;'
                           'uint8_t[3] _padding0;', payload)
        dataset = ulog.get_dataset('outer')

        assert not any('_padding' in name for name in dataset.data)
        assert not any('_padding' in name for name in dataset.list_columns())
        assert dataset.padding_fields
        assert all(field.is_padding for field in dataset.padding_fields)

    def test_unknown_trailing_bytes_are_preserved(self):
        ''' Bytes after the declared layout are kept with their byte range. '''
        payload = (struct.pack('<Q', 123) +
                   self._sub_payload(1, 9, 10) +
                   b'\x00\x00' + b'\xaa\xbb\xcc')
        ulog = self._parse('uint64_t timestamp;sub a;'
                           'uint8_t[2] _padding0;', payload)
        dataset = ulog.get_dataset('outer')

        assert list(dataset.trailing_data) == [b'\xaa\xbb\xcc']
        assert list(dataset.unknown_data) == [b'\xaa\xbb\xcc']
        assert dataset.trailing_ranges[0] == (15, 18)
        assert dataset.trailing_field_data[0].offset == 15

        with tempfile.TemporaryDirectory() as tmpdirname:
            copy_path = os.path.join(tmpdirname, 'tail_copy.ulg')
            ulog.write_ulog(copy_path)
            copied = pyulog.ULog(copy_path)
        copied_dataset = copied.get_dataset('outer')
        assert list(copied_dataset.trailing_data) == [b'\xaa\xbb\xcc']

    def test_definition_order_does_not_change_columns(self):
        ''' Semantically equal logs with reordered definitions align. '''
        payload_ab = (struct.pack('<Q', 5) +
                      self._sub_payload(1, 9, 10) +
                      self._sub_payload(2, 11, 12) + b'\x00\x00')
        payload_ba = (struct.pack('<Q', 5) +
                      self._sub_payload(2, 11, 12) +
                      self._sub_payload(1, 9, 10) + b'\x00\x00')

        ulog_ab = self._parse('uint64_t timestamp;sub a;sub b;'
                              'uint8_t[2] _padding0;', payload_ab)
        ulog_ba = self._parse('uint64_t timestamp;sub b;sub a;'
                              'uint8_t[2] _padding0;', payload_ba)

        dataset_ab = ulog_ab.get_dataset('outer')
        dataset_ba = ulog_ba.get_dataset('outer')
        assert dataset_ab.list_columns() == dataset_ba.list_columns()
        assert list(dataset_ab.data) == list(dataset_ba.data)
        for column in dataset_ab.data:
            assert np.array_equal(dataset_ab.data[column],
                                  dataset_ba.data[column])
        assert dataset_ab == dataset_ba
