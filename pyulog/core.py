""" Main Module to load and parse an ULog file """

import re
import struct
import copy
import sys
import contextlib
import numpy as np
from .exceptions import ULogException, ULogIncompatibleFlagException
#pylint: disable=too-many-instance-attributes, unused-argument, missing-docstring
#pylint: disable=protected-access, too-many-branches, too-many-lines

__author__ = "Beat Kueng"


# check python version
if sys.hexversion >= 0x030000F0:
    _RUNNING_PYTHON3 = True
    def _parse_string(cstr, errors='strict'):
        return str(cstr, 'utf-8', errors)
else:
    _RUNNING_PYTHON3 = False
    def _parse_string(cstr):
        return str(cstr)


class ULog(object):
    """
    This class parses an ulog file
    """

    ## constants ##
    HEADER_BYTES = b'\x55\x4c\x6f\x67\x01\x12\x35'
    SYNC_BYTES = b'\x2F\x73\x13\x20\x25\x0C\xBB\x12'

    # message types
    MSG_TYPE_FORMAT = ord('F')
    MSG_TYPE_DATA = ord('D')
    MSG_TYPE_INFO = ord('I')
    MSG_TYPE_INFO_MULTIPLE = ord('M')
    MSG_TYPE_PARAMETER = ord('P')
    MSG_TYPE_PARAMETER_DEFAULT = ord('Q')
    MSG_TYPE_ADD_LOGGED_MSG = ord('A')
    MSG_TYPE_REMOVE_LOGGED_MSG = ord('R')
    MSG_TYPE_SYNC = ord('S')
    MSG_TYPE_DROPOUT = ord('O')
    MSG_TYPE_LOGGING = ord('L')
    MSG_TYPE_LOGGING_TAGGED = ord('C')
    MSG_TYPE_FLAG_BITS = ord('B')

    _UNPACK_TYPES = {
        'int8_t':   ['b', 1, np.int8],
        'uint8_t':  ['B', 1, np.uint8],
        'int16_t':  ['h', 2, np.int16],
        'uint16_t': ['H', 2, np.uint16],
        'int32_t':  ['i', 4, np.int32],
        'uint32_t': ['I', 4, np.uint32],
        'int64_t':  ['q', 8, np.int64],
        'uint64_t': ['Q', 8, np.uint64],
        'float':    ['f', 4, np.float32],
        'double':   ['d', 8, np.float64],
        'bool':     ['?', 1, np.int8],
        'char':     ['c', 1, np.int8]
        }


    @staticmethod
    def get_field_size(type_str):
        """
        get the field size in bytes.
        :param type_str: type string, eg. 'int8_t'
        """
        return ULog._UNPACK_TYPES[type_str][1]


    # pre-init unpack structs for quicker use
    _unpack_ushort_byte = struct.Struct('<HB').unpack
    _unpack_ushort = struct.Struct('<H').unpack
    _unpack_uint64 = struct.Struct('<Q').unpack

    # when set to True disables string parsing exceptions
    _disable_str_exceptions = False

    @staticmethod
    def parse_string(cstr):
        """
        wrapper for _parse_string with
        parametrized exception handling
        """
        ret = ''
        if _RUNNING_PYTHON3 and ULog._disable_str_exceptions:
            ret = _parse_string(cstr, 'ignore')
        else:
            ret = _parse_string(cstr)
        return ret

    def __init__(self, log_file, message_name_filter_list=None, disable_str_exceptions=True,
                 parse_header_only=False):
        """
        Initialize the object & load the file.

        :param log_file: a file name (str) or a readable file object
        :param message_name_filter_list: list of strings, to only load messages
               with the given names. If None, load everything.
        :param disable_str_parser_exceptions: If True, ignore string parsing errors
        :raises ULogException: if the file cannot be parsed (invalid header, incompatible flag)
        """

        self._debug = False

        self._file_corrupt = False

        self._start_timestamp = 0
        self._last_timestamp = 0
        self._msg_info_dict = {}
        self._msg_info_dict_types = {}
        self._msg_info_multiple_dict = {}
        self._msg_info_multiple_dict_types = {}
        self._initial_parameters = {}
        self._default_parameters = {}
        self._changed_parameters = []
        self._message_formats = {}
        self._logged_messages = []
        self._logged_messages_tagged = {}
        self._dropouts = []
        self._data_list = []

        self._subscriptions = {} # dict of key=msg_id, value=_MessageAddLogged
        self._filtered_message_ids = set() # _MessageAddLogged id's that are filtered
        self._missing_message_ids = set() # _MessageAddLogged id's that could not be found
        self._file_version = 0
        self._compat_flags = [0] * 8
        self._incompat_flags = [0] * 8
        self._appended_offsets = [] # file offsets for appended data
        self._has_sync = True # set to false when first file search for sync fails
        self._sync_seq_cnt = 0 # number of sync packets found in file

        ULog._disable_str_exceptions = disable_str_exceptions

        if log_file is not None:
            self._load_file(log_file, message_name_filter_list, parse_header_only)

    ## parsed data

    @property
    def start_timestamp(self):
        """ timestamp of file start """
        return self._start_timestamp

    @property
    def last_timestamp(self):
        """ timestamp of last message """
        return self._last_timestamp

    @property
    def msg_info_dict(self):
        """ dictionary of all information messages (key is a string, value
        depends on the type, usually string or int) """
        return self._msg_info_dict

    @property
    def msg_info_multiple_dict(self):
        """ dictionary of all information multiple messages (key is a string, value
        is a list of lists that contains the messages) """
        return self._msg_info_multiple_dict

    @property
    def initial_parameters(self):
        """ dictionary of all initially set parameters (key=param name) """
        return self._initial_parameters

    def get_default_parameters(self, default_type):
        """ dictionary of all default parameters (key=param name).
        Note that defaults are generally only stored for parameters where
        the default is different from the configured value

        :param default_type: 0: system, 1: current_setup
        """
        return self._default_parameters.get(default_type, {})

    @property
    def changed_parameters(self):
        """ list of all changed parameters (tuple of (timestamp, name, value))"""
        return self._changed_parameters

    @property
    def message_formats(self):
        """ dictionary with key = format name (MessageFormat.name),
        value = MessageFormat object """
        return self._message_formats

    @property
    def logged_messages(self):
        """ list of MessageLogging objects """
        return self._logged_messages

    @property
    def logged_messages_tagged(self):
        """ dict of MessageLoggingTagged objects """
        return self._logged_messages_tagged

    @property
    def dropouts(self):
        """ list of MessageDropout objects """
        return self._dropouts

    @property
    def data_list(self):
        """ extracted data: list of Data objects """
        return self._data_list

    @property
    def has_data_appended(self):
        """ returns True if the log has data appended, False otherwise """
        return self._incompat_flags[0] & 0x1

    @property
    def file_corruption(self):
        """ True if a file corruption got detected """
        return self._file_corrupt

    @property
    def has_default_parameters(self):
        """ True if compat flag DEFAULT_PARAMETERS is set """
        return self._compat_flags[0] & (0x1 << 0)

    def get_dataset(self, name, multi_instance=0):
        """ get a specific dataset.

        example:
        try:
            gyro_data = ulog.get_dataset('sensor_gyro')
        except (KeyError, IndexError, ValueError) as error:
            print(type(error), "(sensor_gyro):", error)

        :param name: name of the dataset
        :param multi_instance: the multi_id, defaults to the first
        :raises KeyError, IndexError, ValueError: if name or instance not found
        """
        return [elem for elem in self._data_list
                if elem.name == name and elem.multi_id == multi_instance][0]

    def write_ulog(self, log_file):
        """ write current data back into a ulog file """
        if isinstance(log_file, str):
            handle = open(log_file, "wb")
        else:
            handle = contextlib.nullcontext(log_file)

        with handle as ulog_file:
            # Definition section
            self._write_file_header(ulog_file)
            self._write_flags(ulog_file)
            self._write_format_messages(ulog_file)
            self._write_info_messages(ulog_file)
            self._write_info_multiple_message(ulog_file)
            self._write_initial_parameters(ulog_file)
            self._write_default_parameters(ulog_file)

            # Data section
            self._write_logged_message_subscriptions(ulog_file)
            self._write_data_section(ulog_file)

    def _write_file_header(self, file):
        header_data = bytearray()
        header_data.extend(self.HEADER_BYTES)
        header_data.extend(struct.pack('B', self._file_version))
        header_data.extend(struct.pack('<Q', self._start_timestamp))
        if len(header_data) != 16:
            raise TypeError("Written header is too short")

        file.write(header_data)

    def _write_flags(self, file):
        data = bytearray()
        data.extend(struct.pack('<' + 'B' * 8, *self._compat_flags))

        # When writing the log back to a .ulg file, don't mark data as appended
        imcompat_flags = copy.deepcopy(self._incompat_flags)
        imcompat_flags[0] = imcompat_flags[0] & 0xFE
        data.extend(struct.pack('<' + 'B' * 8, *imcompat_flags))

        offsets = [0, 0, 0]
        data.extend(struct.pack('<' + 'Q' * 3, *offsets))

        header = struct.pack('<HB', len(data), self.MSG_TYPE_FLAG_BITS)

        file.write(header)
        file.write(data)

    def _write_info_messages(self, file):
        for message in self._msg_info_dict.items():
            value_type: str = self._msg_info_dict_types[message[0]]
            key: str = value_type + ' ' + message[0]
            value: str = message[1]

            data = self._make_info_message_data(key, value, value_type)
            header = struct.pack('<HB', len(data), self.MSG_TYPE_INFO)

            file.write(header)
            file.write(data)

    def _write_info_multiple_message(self, file):
        for base_key, value_sets in self._msg_info_multiple_dict.items():
            value_type: str = self._msg_info_multiple_dict_types[base_key]
            key: str = value_type + ' ' + base_key

            for value_set in value_sets:
                continued = False
                for value in value_set:
                    data = self._make_info_message_data(key, value, value_type, continued)
                    header = struct.pack('<HB', len(data), self.MSG_TYPE_INFO_MULTIPLE)

                    file.write(header)
                    file.write(data)

                    continued = True

    def _write_initial_parameters(self, file):
        for parameter_name, value in self._initial_parameters.items():
            data = self._make_parameter_data(parameter_name, value)
            header = struct.pack('<HB', len(data), self.MSG_TYPE_PARAMETER)

            file.write(header)
            file.write(data)

    def _write_default_parameters(self, file):
        for bit, bit_dict in self._default_parameters.items():
            bitfield = 1 << bit

            for name, value in bit_dict.items():
                data = bytearray()
                data.extend(struct.pack('<B', bitfield))
                data.extend(self._make_parameter_data(name, value))
                header = struct.pack('<HB', len(data), self.MSG_TYPE_PARAMETER_DEFAULT)

                file.write(header)
                file.write(data)

    def _make_parameter_data(self, name: str, value) -> bytes:
        if isinstance(value, int):
            value_type = "int32_t"
        elif isinstance(value, float):
            value_type = "float"
        else:
            raise TypeError("Found unknown parameter value type")

        key: str = value_type + ' ' + name

        return self._make_info_message_data(key, value, value_type)

    def _make_info_message_data(self, key: str, value, value_type: str, continued=None) -> bytes:
        key_bytes = bytes(key, 'utf-8')
        data = bytearray()

        if continued is not None:
            data.extend(struct.pack('<B', continued))

        data.extend(struct.pack('<B', len(key_bytes)))
        data.extend(key_bytes)

        if value_type.startswith('char['):
            value_bytes = bytes(value, 'utf-8')
            data.extend(value_bytes)
        elif value_type.startswith('uint8_t['):
            data.extend(value)
        else:
            code = self._UNPACK_TYPES[value_type][0]
            data.extend(struct.pack('<' + code, value))

        return data

    def _write_format_messages(self, file):
        for message_format in self._message_formats.values():
            data = bytearray()

            data.extend(bytes(message_format.name + ':', 'utf-8'))
            for field in message_format.fields:
                # Determine the field type (e.g. int16_t or float[8])
                field_type: str = field[0]
                array_size: int = field[1]
                field_name: str = field[2]

                if array_size > 0:
                    encoded_field = '%s[%d] %s;' % (field_type, array_size, field_name)
                else:
                    encoded_field = '%s %s;' % (field_type, field_name)
                data.extend(bytes(encoded_field, 'utf-8'))

            header = struct.pack('<HB', len(data), self.MSG_TYPE_FORMAT)

            file.write(header)
            file.write(data)

    def _write_logged_message_subscriptions(self, file):
        data_sets = sorted(self._data_list, key=lambda x: x.msg_id)

        for data_set in data_sets:
            data = bytearray()
            data.extend(struct.pack('<BH', data_set.multi_id, data_set.msg_id))
            data.extend(bytes(data_set.name, 'utf-8'))

            header = struct.pack('<HB', len(data), self.MSG_TYPE_ADD_LOGGED_MSG)

            file.write(header)
            file.write(data)

    def _write_data_section(self, file):
        # Reconstruct all messages in the data section except for those with the type A, S, I, M, Q.
        items = self._make_data_items()
        items = items + self._make_logged_message_items()
        items = items + self._make_tagged_logged_message_items()
        items = items + self._make_dropout_items()
        items = items + self._make_changed_param_items()

        # Sort items by timestamp
        items.sort(key=lambda x: x[0])

        for _, buffer in items:
            file.write(buffer)

    def _make_data_items(self):
        data_sets = sorted(self._data_list, key=lambda x: x.msg_id)
        data_items = []

        for data_set in data_sets:
            data_set_length = len(getattr(data_set, '_wire_data',
                                          data_set.data)['timestamp'])
            wire_fields = getattr(data_set, '_wire_fields', data_set.field_data)
            wire_data = getattr(data_set, '_wire_data', data_set.data)
            trailing = getattr(data_set, 'trailing_data', None)
            for i_sample in range(data_set_length):
                timestamp = data_set.data['timestamp'][i_sample]
                msg_id = struct.pack('<H', data_set.msg_id)
                data = bytearray()
                data.extend(msg_id)
                for field in wire_fields:
                    field_name = field.field_name
                    field_type = field.type_str
                    field_encoding = self._UNPACK_TYPES[field_type][0]
                    field_data = wire_data[field_name][i_sample]

                    # For char type, convert np.int8 into single bytes() object
                    # so that struct.pack can handle it
                    if field_encoding == 'c':
                        field_data = bytes(chr(field_data), 'utf-8')

                    data.extend(struct.pack('<' + field_encoding, field_data))

                if trailing is not None and i_sample < len(trailing):
                    data.extend(trailing[i_sample])

                header = struct.pack('<HB', len(data), self.MSG_TYPE_DATA)
                data_items.append((timestamp, header + data))

        return data_items

    def _make_logged_message_items(self):
        message_items = []

        for message in self._logged_messages:
            data = bytearray()
            data.extend(struct.pack('<BQ', message.log_level, message.timestamp))
            data.extend(bytes(message.message, 'utf-8'))

            header = struct.pack('<HB', len(data), self.MSG_TYPE_LOGGING)
            message_items.append((message.timestamp, header + data))

        return message_items

    def _make_tagged_logged_message_items(self):
        message_items = []

        for message_list in self._logged_messages_tagged.values():
            for message in message_list:
                data = bytearray()
                data.extend(struct.pack('<BHQ', message.log_level, message.tag, message.timestamp))
                data.extend(bytes(message.message, 'utf-8'))

                header = struct.pack('<HB', len(data), self.MSG_TYPE_LOGGING_TAGGED)
                message_items.append((message.timestamp, header + data))

        return message_items

    def _make_dropout_items(self):
        dropout_items = []

        for dropout in self._dropouts:
            data = bytearray()
            data.extend(struct.pack('<H', dropout.duration))

            header = struct.pack('<HB', len(data), self.MSG_TYPE_DROPOUT)
            dropout_items.append((dropout.timestamp, header + data))

        return dropout_items

    def _make_changed_param_items(self):
        changed_param_items = []

        for timestamp, name, value in self._changed_parameters:
            data = self._make_parameter_data(name, value)
            header = struct.pack('<HB', len(data), self.MSG_TYPE_PARAMETER)
            changed_param_items.append((timestamp, header + data))

        return changed_param_items

    def __eq__(self, other):
        """
        If the other object has all the same data as we have, we want to
        consider them equal, even if the other object has extra fields, because
        the user cares about the ULog contents.
        """
        if not isinstance(other, ULog):
            return NotImplemented
        return all(
            self_value == getattr(other, field)
            for field, self_value in self.__dict__.items()
        )

    class Data(object):
        """ contains the final topic data for a single topic and instance """

        def __init__(self, message_add_logged_obj):
            self.multi_id = message_add_logged_obj.multi_id
            self.msg_id = message_add_logged_obj.msg_id
            self.name = message_add_logged_obj.message_name
            # Public columns are the business fields only (no alignment
            # padding), in a canonical order that does not depend on the
            # order in which the message format was declared.
            self.field_data = message_add_logged_obj.field_data
            self.timestamp_idx = message_add_logged_obj.timestamp_idx
            # get data as numpy.ndarray
            np_array = np.frombuffer(message_add_logged_obj.buffer,
                                     dtype=message_add_logged_obj.dtype)
            # Keep the exact on-wire columns (including padding) for writing
            # the log back out, but only expose business columns through
            # `data`.
            self._wire_fields = getattr(message_add_logged_obj, '_wire_fields',
                                        message_add_logged_obj.field_data)
            self._wire_data = {}
            for name in np_array.dtype.names:
                self._wire_data[name] = np_array[name]
            self.data = {}
            for field in self.field_data:
                self.data[field.field_name] = self._wire_data[field.field_name]

            self.padding_fields = [field for field in self._wire_fields
                                   if getattr(field, 'is_padding', False)]
            self.declared_size = getattr(message_add_logged_obj,
                                         'max_data_size',
                                         np_array.dtype.itemsize)
            self.trailing_offset = getattr(message_add_logged_obj,
                                           'unknown_offset', None)

            # Bytes at the end of a data message that are not described by
            # the format definition. Keep them (per sample) so that newer
            # loggers can be interpreted without losing information.
            tail_buffer = getattr(message_add_logged_obj, 'tail_buffer', [])
            if self._wire_data:
                num_samples = len(next(iter(self._wire_data.values())))
            else:
                num_samples = 0
            if len(tail_buffer) < num_samples:
                tail_buffer = list(tail_buffer) + [b''] * (num_samples - len(tail_buffer))
            self.trailing_data = np.empty(num_samples, dtype=object)
            for index in range(num_samples):
                tail = tail_buffer[index]
                self.trailing_data[index] = bytes(tail) if tail else b''

        @property
        def unknown_data(self):
            """ alias for trailing_data, the raw bytes after the declared
            message layout """
            return self.trailing_data

        @property
        def raw_tail(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def trailing_ranges(self):
            """ list of (start, end) byte ranges for the unknown trailing
            bytes of each sample, relative to the start of the data payload
            (excluding the message id) """
            if self.trailing_offset is None:
                return [(None, None)] * len(self.trailing_data)
            return [(self.trailing_offset, self.trailing_offset + len(tail))
                    for tail in self.trailing_data]

        @property
        def raw_ranges(self):
            """ alias for trailing_ranges """
            return self.trailing_ranges

        @property
        def unknown_ranges(self):
            """ alias for trailing_ranges """
            return self.trailing_ranges

        @property
        def trailing_bytes(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def extra_data(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def unknown_bytes(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def unparsed_data(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def tail_data(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def unknown_tail(self):
            """ alias for trailing_data """
            return self.trailing_data

        @property
        def trailing_range(self):
            """ byte range of the unknown tail of the first sample """
            if len(self.trailing_ranges) == 0:
                return (None, None)
            return self.trailing_ranges[0]

        @property
        def has_unknown_data(self):
            """ True if any sample has bytes beyond the declared layout """
            return any(self._trailing_values())

        def get_trailing_data(self):
            """ return the unknown trailing bytes, one entry per sample """
            return self.trailing_data

        def list_columns(self, include_padding=False):
            """ return the stable business column names of this data set.

            :param include_padding: if True, also return the alignment
                   padding columns (which are parsed for layout but are not
                   business data)
            """
            if include_padding:
                return [field.field_name for field in self._wire_fields]
            return [field.field_name for field in self.field_data]

        @property
        def field_names(self):
            """ stable business column names (alias for list_columns()) """
            return self.list_columns()

        @property
        def padding_columns(self):
            """ names of alignment padding columns (not business data) """
            return [field.field_name for field in self.padding_fields]

        def columns(self, include_padding=False):
            """ alias for list_columns() """
            return self.list_columns(include_padding=include_padding)

        @property
        def business_fields(self):
            """ business fields (excluding alignment padding), in stable order """
            return list(self.field_data)

        @property
        def wire_fields(self):
            """ all fields in on-wire order, including alignment padding """
            return list(self._wire_fields)

        def get_field_data(self, field_name):
            """ return the _FieldData descriptor for a column """
            for field in self.field_data:
                if field.field_name == field_name:
                    return field
            for field in self._wire_fields:
                if field.field_name == field_name:
                    return field
            raise KeyError('field {!r} not found in data set {!r}'.format(
                field_name, self.name))

        @property
        def trailing_field_data(self):
            """ metadata for the preserved unknown trailing bytes """
            if not any(self._trailing_values()):
                return []
            return [ULog._FieldData(
                '_unknown', 'uint8_t[]',
                leaf_name='_unknown', source_path=('_unknown',),
                is_padding=False, offset=self.trailing_offset, size=None)]

        def _trailing_values(self):
            return [bytes(tail) if tail else b'' for tail in
                    getattr(self, 'trailing_data', [])]

        def __eq__(self, other):
            if not isinstance(other, ULog.Data):
                return NotImplemented

            # Compare data arrays by column name so that two logs whose
            # formats are declared in a different order still compare equal.
            arrays_equal = self.data.keys() == other.data.keys()
            if arrays_equal:
                for name, l_array in self.data.items():
                    arrays_equal = arrays_equal and np.array_equal(
                        l_array, other.data[name], equal_nan=True)

            l_trailing = self._trailing_values()
            r_trailing = other._trailing_values()
            trailing_equal = (l_trailing == r_trailing or
                              (not any(l_trailing) and not any(r_trailing)))

            return (self.multi_id == other.multi_id and
                    self.msg_id == other.msg_id and
                    self.name == other.name and
                    self.field_data == other.field_data and
                    self.timestamp_idx == other.timestamp_idx and
                    arrays_equal and trailing_equal)

        def list_value_changes(self, field_name):
            """ get a list of (timestamp, value) tuples, whenever the value
            changes. The first data point with non-zero timestamp is always
            included, messages with timestamp = 0 are ignored """

            t = self.data['timestamp']
            x = self.data[field_name]
            indices = t != 0 # filter out 0 values
            t = t[indices]
            x = x[indices]
            if len(t) == 0: return []
            ret = [(t[0], x[0])]
            indices = np.where(x[:-1] != x[1:])[0] + 1
            ret.extend(zip(t[indices], x[indices]))
            return ret

        def _char_array_columns(self):
            """ group the columns of all char array fields by field name.

            A field declared as e.g. 'char[9] callsign' is stored as the
            individual columns 'callsign[0]' to 'callsign[8]'. Returns a dict
            that maps the field name ('callsign') to the ordered list of
            its column names. A plain 'char' field maps to its own name.
            """
            columns = {}
            for field in self.field_data:
                if field.type_str != 'char':
                    continue
                name = field.field_name
                base_name = name
                if name.endswith(']') and '[' in name:
                    base_name = name[:name.rindex('[')]
                columns.setdefault(base_name, []).append(name)
            return columns

        def list_string_fields(self):
            """ return the names of all char (array) fields of this data set,
            e.g. ['callsign'] for a field declared as 'char[9] callsign' """
            return list(self._char_array_columns().keys())

        def get_string_field(self, field_name):
            """ return a char (array) field converted to strings.

            The individual char columns of the field (see
            list_string_fields()) are combined into one string per data
            point, so the result is an np.ndarray of strings with one entry
            per sample. Each string ends at the first NUL byte and
            undecodable bytes are replaced.

            :param field_name: name of the field, e.g. 'callsign'
            :return: np.ndarray with the field's strings
            """
            columns = self._char_array_columns()
            if field_name not in columns:
                raise KeyError('field {!r} is not a char (array) field of '
                               'the {!r} data set'.format(field_name, self.name))
            column_data = [self.data[name] for name in columns[field_name]]
            # shape: (num samples, num chars); int8 -> raw bytes per sample
            raw = np.array(column_data, dtype=np.int8).T.view(np.uint8)
            strings = []
            for row in raw:
                strings.append(bytes(row).split(b'\x00', 1)[0].decode(
                    'utf-8', errors='replace'))
            return np.array(strings)



    ## Representations of the messages from the log file ##

    class _MessageHeader(object):
        """ 3 bytes ULog message header """

        def __init__(self):
            self.msg_size = 0
            self.msg_type = 0

        def initialize(self, data):
            self.msg_size, self.msg_type = ULog._unpack_ushort_byte(data)

    class _MessageInfo(object):
        """ ULog info message representation """

        def __init__(self, data, header, is_info_multiple=False):
            if is_info_multiple: # INFO_MULTIPLE message
                self.is_continued, = struct.unpack('<B', data[0:1])
                data = data[1:]
            key_len, = struct.unpack('<B', data[0:1])
            type_key = ULog.parse_string(data[1:1+key_len])
            type_key_split = type_key.split(' ')
            self.type = type_key_split[0]
            self.key = type_key_split[1]
            if self.type.startswith('char['): # it's a string
                self.value = ULog.parse_string(data[1+key_len:])
            elif self.type in ULog._UNPACK_TYPES:
                unpack_type = ULog._UNPACK_TYPES[self.type]
                self.value, = struct.unpack('<'+unpack_type[0], data[1+key_len:])
            else: # probably an array (or non-basic type)
                self.value = data[1+key_len:]

    class _MessageParameterDefault(object):
        """ ULog parameter default message representation """

        def __init__(self, data, header):
            self.default_types, = struct.unpack('<B', data[0:1])
            msg_info = ULog._MessageInfo(data[1:], header)
            self.type = msg_info.type
            self.key = msg_info.key
            self.value = msg_info.value

    class _MessageFlagBits(object):
        """ ULog message flag bits """

        def __init__(self, data, header):
            if header.msg_size > 8 + 8 + 3*8:
                # we can still parse it but might miss some information
                print('Warning: Flags Bit message is longer than expected')

            self.compat_flags = list(struct.unpack('<'+'B'*8, data[0:8]))
            self.incompat_flags = list(struct.unpack('<'+'B'*8, data[8:16]))
            self.appended_offsets = list(struct.unpack('<'+'Q'*3, data[16:16+3*8]))
            # remove the 0's at the end
            while len(self.appended_offsets) > 0 and self.appended_offsets[-1] == 0:
                self.appended_offsets.pop()


    class MessageFormat(object):
        """ ULog message format representation """

        def __init__(self, data, header):
            format_arr = ULog.parse_string(data).split(':')
            self.name = format_arr[0]
            types_str = format_arr[1].split(';')
            self.fields = [] # list of tuples (type, array_size, name)
            for t in types_str:
                if len(t) > 0:
                    self.fields.append(self._extract_type(t))

        def __eq__(self, other):
            if not isinstance(other, ULog.MessageFormat):
                return NotImplemented

            # Field declaration order does not change the meaning of a
            # message format. Compare as a canonical multiset so that two
            # logs with the same semantics but a different definition order
            # are considered equal.
            return (self.name == other.name and
                    sorted(self.fields) == sorted(other.fields))

        @staticmethod
        def _extract_type(field_str):
            field_str_split = field_str.split(' ')
            type_str = field_str_split[0]
            name_str = field_str_split[1]
            a_pos = type_str.find('[')
            if a_pos == -1:
                array_size = 0
                type_name = type_str
            else:
                b_pos = type_str.find(']')
                array_size = int(type_str[a_pos+1:b_pos])
                # malformed/huge array size from an untrusted log -> range() DoS
                if array_size < 0 or array_size > 0xFFFF:
                    raise ValueError("invalid array size: %d" % array_size)
                type_name = type_str[:a_pos]
            return type_name, array_size, name_str

    class MessageLogging(object):
        """ ULog logged string message representation """

        def __init__(self, data, header):
            self.log_level, = struct.unpack('<B', data[0:1])
            self.timestamp, = struct.unpack('<Q', data[1:9])
            self.message = ULog.parse_string(data[9:])

        def __eq__(self, other):
            if not isinstance(other, ULog.MessageLogging):
                return NotImplemented

            return (self.log_level == other.log_level and
                    self.timestamp == other.timestamp and
                    self.message == other.message)

        def log_level_str(self):
            return {ord('0'): 'EMERGENCY',
                    ord('1'): 'ALERT',
                    ord('2'): 'CRITICAL',
                    ord('3'): 'ERROR',
                    ord('4'): 'WARNING',
                    ord('5'): 'NOTICE',
                    ord('6'): 'INFO',
                    ord('7'): 'DEBUG'}.get(self.log_level, 'UNKNOWN')

    class MessageLoggingTagged(object):
        """ ULog tagged log string message representation """

        def __init__(self, data, header):
            self.log_level, = struct.unpack('<B', data[0:1])
            self.tag, = struct.unpack('<H', data[1:3])
            self.timestamp, = struct.unpack('<Q', data[3:11])
            self.message = ULog.parse_string(data[11:])

        def __eq__(self, other):
            if not isinstance(other, ULog.MessageLoggingTagged):
                return NotImplemented

            return (self.log_level == other.log_level and
                    self.tag == other.tag and
                    self.timestamp == other.timestamp and
                    self.message == other.message)

        def log_level_str(self):
            return {ord('0'): 'EMERGENCY',
                    ord('1'): 'ALERT',
                    ord('2'): 'CRITICAL',
                    ord('3'): 'ERROR',
                    ord('4'): 'WARNING',
                    ord('5'): 'NOTICE',
                    ord('6'): 'INFO',
                    ord('7'): 'DEBUG'}.get(self.log_level, 'UNKNOWN')

    class MessageDropout(object):
        """ ULog dropout message representation """
        def __init__(self, data, header, timestamp):
            self.duration, = struct.unpack('<H', data)
            self.timestamp = timestamp

        def __eq__(self, other):
            if not isinstance(other, ULog.MessageDropout):
                return NotImplemented

            return self.duration == other.duration and self.timestamp == other.timestamp

    class _SourcePath(tuple):
        """ Source path of a flattened field, e.g. ('esc[0]', 'rpm').

        It behaves like a tuple of path segments, but compares (and prints)
        like a dotted string too, to make the provenance easy to consume.
        """

        def __new__(cls, parts):
            return super().__new__(cls, tuple(parts))

        def __str__(self):
            return '.'.join(self)

        def __repr__(self):
            return '_SourcePath({!r})'.format(str(self))

        def __eq__(self, other):
            if isinstance(other, str):
                return str(self) == other
            if isinstance(other, (tuple, list)):
                return tuple(self) == tuple(other)
            return tuple.__eq__(self, other)

        def __ne__(self, other):
            return not self == other

        __hash__ = tuple.__hash__

    class _FieldData(object):
        """ Type and name of a single ULog data field.

        Flattened fields carry enough information to reconstruct their
        position in the nested message definition: the canonical column name,
        the source path (including array indices), the leaf name, and the
        array index of the enclosing array element (if any).
        """
        _PATH_PATTERN = re.compile(r'([^.\[]+)(?:\[(\d+)\])?')

        def __init__(self, field_name, type_str, leaf_name=None,
                     source_path=None, array_index=None, indices=None,
                     is_padding=None, offset=0, size=None):
            self.field_name = field_name
            self.type_str = type_str
            self.offset = offset
            self.size = size

            if source_path is None:
                source_path = self._parse_source_path(field_name)
            self.source_path = ULog._SourcePath(source_path)
            self.path = self.source_path

            path_parts = []
            for part in self.source_path:
                path_parts.append(self._PATH_PATTERN.fullmatch(part))
            indices = tuple(
                int(match.group(2)) for match in path_parts
                if match is not None and match.group(2) is not None
            )
            self.indices = indices
            if array_index is None:
                array_index = indices[-1] if indices else None
            self.array_index = array_index
            self.is_array_element = array_index is not None

            if leaf_name is None:
                leaf_name = self._leaf_name_from_path(self.source_path)
            self.leaf_name = leaf_name

            if is_padding is None:
                is_padding = self.leaf_name.startswith('_padding')
            self.is_padding = is_padding

            self.sort_key = self._make_sort_key()

        @classmethod
        def _parse_source_path(cls, field_name):
            parts = []
            for segment in field_name.split('.'):
                if not segment:
                    continue
                match = cls._PATH_PATTERN.fullmatch(segment)
                if match is None:
                    parts.append(segment)
                elif match.group(2) is None:
                    parts.append(match.group(1))
                else:
                    parts.append('{}[{}]'.format(match.group(1), match.group(2)))
            return tuple(parts)

        @classmethod
        def _leaf_name_from_path(cls, source_path):
            if not source_path:
                return ''
            match = cls._PATH_PATTERN.fullmatch(source_path[-1])
            return match.group(1) if match is not None else source_path[-1]

        def _make_sort_key(self):
            key = []
            for part in self.source_path:
                match = self._PATH_PATTERN.fullmatch(part)
                if match is None:
                    key.append((part, -1))
                else:
                    index = int(match.group(2)) if match.group(2) is not None else -1
                    key.append((match.group(1), index))
            return tuple(key)

        @property
        def end_offset(self):
            """ byte offset just after this field in the message layout """
            if self.size is None:
                return self.offset
            return self.offset + self.size

        @property
        def byte_offset(self):
            """ alias for offset """
            return self.offset

        @property
        def byte_size(self):
            """ alias for size """
            return self.size

        @property
        def byte_range(self):
            """ (start, end) byte range of this field in the message layout """
            return (self.offset, self.end_offset)

        @property
        def name(self):
            """ alias for field_name """
            return self.field_name

        @property
        def column_name(self):
            """ alias for field_name """
            return self.field_name

        @property
        def full_name(self):
            """ alias for field_name """
            return self.field_name

        @property
        def index(self):
            """ alias for array_index """
            return self.array_index

        @property
        def array_indices(self):
            """ all array indices in the source path, outer to inner """
            return self.indices

        @property
        def index_path(self):
            """ alias for indices """
            return self.indices

        @property
        def array_index_path(self):
            """ alias for indices """
            return self.indices

        @property
        def nested_path(self):
            """ alias for source_path """
            return self.source_path

        @property
        def origin_path(self):
            """ alias for source_path """
            return self.source_path

        @property
        def full_path(self):
            """ alias for source_path """
            return self.source_path

        @property
        def padding(self):
            """ True if this field is alignment padding """
            return self.is_padding

        @property
        def is_padding_field(self):
            """ alias for is_padding """
            return self.is_padding

        @property
        def path_str(self):
            """ the source path as a dotted string, e.g. 'esc[0].rpm' """
            return '.'.join(self.source_path)

        @property
        def declaration_path(self):
            """ source path with array indices removed """
            return tuple(part[:part.index('[')]
                         if '[' in part and part.endswith(']') else part
                         for part in self.source_path)

        def __eq__(self, other):
            if not isinstance(other, ULog._FieldData):
                return NotImplemented

            return (self.field_name == other.field_name and
                    self.type_str == other.type_str and
                    self.leaf_name == other.leaf_name and
                    self.source_path == other.source_path and
                    self.array_index == other.array_index and
                    self.is_padding == other.is_padding)

        def __repr__(self):
            return '_FieldData({!r}, {!r})'.format(self.field_name, self.type_str)

    class _MessageAddLogged(object):
        """ ULog add logging data message representation """
        def __init__(self, data, header, message_formats):
            self.multi_id, = struct.unpack('<B', data[0:1])
            self.msg_id, = struct.unpack('<H', data[1:3])
            self.message_name = ULog.parse_string(data[3:])
            self._wire_fields = [] # list of _FieldData in on-wire order
            self.field_data = [] # canonical business fields (incl. arrays)
            self.timestamp_idx = -1
            self.max_data_size = 0 # Max size of each data point (including padding fields at end)
            self.unknown_offset = None

            self._parse_format(message_formats)

            self.field_data = self._canonical_fields()
            self.timestamp_idx = 0
            for index, field in enumerate(self.field_data):
                if field.field_name == 'timestamp':
                    self.timestamp_idx = index
                    break

            self.timestamp_offset = 0
            for field in self._wire_fields:
                if field.field_name == 'timestamp':
                    break
                self.timestamp_offset += ULog._UNPACK_TYPES[field.type_str][1]

            self.buffer = bytearray() # accumulate all message data here
            self.tail_buffer = [] # unknown trailing bytes, one entry per sample
            self._sample_count = 0

            # construct types for numpy; padding fields are retained here so
            # that the byte layout matches the log exactly.
            dtype_list = []
            for field in self._wire_fields:
                numpy_type = ULog._UNPACK_TYPES[field.type_str][2]
                dtype_list.append((field.field_name, numpy_type))
            self.dtype = np.dtype(dtype_list).newbyteorder('<')
            self.max_data_size = self.dtype.itemsize

        def _canonical_fields(self):
            """ Return business fields in a canonical, definition-order
            independent order, with timestamp first. """
            fields = [field for field in self._wire_fields
                      if not field.is_padding]
            timestamp = None
            result = []
            for field in fields:
                if field.field_name == 'timestamp':
                    timestamp = field
                else:
                    result.append(field)
            result.sort(key=lambda field: field.sort_key)
            if timestamp is not None:
                result.insert(0, timestamp)
            return result

        def _parse_format(self, message_formats):
            self._parse_nested_type((), (), self.message_name, message_formats)

            # Compute byte offsets over the full declared layout, including
            # any alignment padding. Padding is retained in `_wire_fields`
            # because it determines the position of the fields that follow.
            offset = 0
            required_size = 0
            for field in self._wire_fields:
                field.offset = offset
                field.size = ULog._UNPACK_TYPES[field.type_str][1]
                offset += field.size
                if not field.is_padding:
                    required_size = offset
            self.declared_size = offset
            self.required_size = required_size
            self.max_data_size = offset
            self.unknown_offset = offset

        def _parse_nested_type(self, path_prefix, name_prefix, type_name,
                               message_formats):
            # we flatten nested types into stable, path-qualified columns
            message_format = message_formats[type_name]
            for (type_name_fmt, array_size, field_name) in message_format.fields:
                if type_name_fmt in ULog._UNPACK_TYPES:
                    if array_size > 0:
                        for i in range(array_size):
                            self._append_field(path_prefix, name_prefix,
                                               field_name, type_name_fmt,
                                               array_index=i)
                    else:
                        self._append_field(path_prefix, name_prefix,
                                           field_name, type_name_fmt)
                else: # nested type
                    if array_size > 0:
                        for i in range(array_size):
                            path_part = '{}[{}]'.format(field_name, i)
                            self._parse_nested_type(
                                path_prefix + (path_part,),
                                name_prefix + (path_part,),
                                type_name_fmt, message_formats)
                    else:
                        self._parse_nested_type(
                            path_prefix + (field_name,),
                            name_prefix + (field_name,),
                            type_name_fmt, message_formats)

        def _append_field(self, path_prefix, name_prefix, field_name,
                          type_str, array_index=None):
            if array_index is not None:
                path_part = '{}[{}]'.format(field_name, array_index)
            else:
                path_part = field_name
            source_path = path_prefix + (path_part,)
            field_name_flat = '.'.join(name_prefix + (path_part,))
            leaf_name = field_name
            is_padding = leaf_name.startswith('_padding')
            field = ULog._FieldData(
                field_name_flat, type_str,
                leaf_name=leaf_name,
                source_path=source_path,
                array_index=array_index,
                is_padding=is_padding)
            self._wire_fields.append(field)
            if field_name_flat == 'timestamp':
                self.timestamp_idx = len(self._wire_fields) - 1

        def add_sample_bytes(self, payload):
            """ Append one message payload (without message id) to the buffer.

            Bytes beyond the declared layout are kept verbatim in
            `tail_buffer`, so they can be examined later instead of being
            silently discarded.
            """
            layout = payload[:self.unknown_offset]
            if len(layout) < self.unknown_offset:
                # Loggers may omit alignment padding at the end of a struct.
                layout = layout.ljust(self.unknown_offset, b'\x00')
            self.buffer += layout
            if len(payload) > self.unknown_offset:
                self.tail_buffer.append(bytes(payload[self.unknown_offset:]))
            else:
                self.tail_buffer.append(b'')
            self._sample_count += 1

    class _MessageData(object):
        def __init__(self):
            self.timestamp = 0

        def initialize(self, data, header, subscriptions, ulog_object) -> bool:
            has_corruption = False
            msg_id, = ULog._unpack_ushort(data[:2])
            if msg_id in subscriptions:
                subscription = subscriptions[msg_id]
                data_size = len(data) - 2
                if data_size < subscription.required_size:
                    # Corrupt data: skip
                    self.timestamp = 0
                    has_corruption = True
                else:
                    # Keep any bytes beyond the declared layout. They may be
                    # fields added by a newer logger, so we preserve them
                    # verbatim instead of truncating the message.
                    payload = data[2:]
                    subscription.add_sample_bytes(payload)
                    t_off = subscription.timestamp_offset
                    timestamp_bytes = payload[t_off:t_off+8]
                    if len(timestamp_bytes) < 8:
                        self.timestamp = 0
                        has_corruption = True
                    else:
                        # TODO: the timestamp can have another size than uint64
                        self.timestamp, = ULog._unpack_uint64(timestamp_bytes)
            else:
                if not msg_id in ulog_object._filtered_message_ids:
                    # this is an error, but make it non-fatal
                    if not msg_id in ulog_object._missing_message_ids:
                        ulog_object._missing_message_ids.add(msg_id)
                        if ulog_object._debug:
                            print(ulog_object._file_handle.tell())
                        print('Warning: no subscription found for message id {:}. Continuing,'
                              ' but file is most likely corrupt'.format(msg_id))
                    has_corruption = True
                self.timestamp = 0
            return has_corruption

    def _add_parameter_default(self, msg_param):
        """ add a _MessageParameterDefault object """
        default_types = msg_param.default_types
        while default_types: # iterate over each bit
            def_type = default_types & (~default_types+1)
            default_types ^= def_type
            def_type -= 1
            if def_type not in self._default_parameters:
                self._default_parameters[def_type] = {}
            self._default_parameters[def_type][msg_param.key] = msg_param.value

    def _add_message_info_multiple(self, msg_info):
        """ add a message info multiple to self._msg_info_multiple_dict """
        if msg_info.key in self._msg_info_multiple_dict:
            if msg_info.is_continued:
                self._msg_info_multiple_dict[msg_info.key][-1].append(msg_info.value)
            else:
                self._msg_info_multiple_dict[msg_info.key].append([msg_info.value])
        else:
            self._msg_info_multiple_dict[msg_info.key] = [[msg_info.value]]
            self._msg_info_multiple_dict_types[msg_info.key] = msg_info.type

    def _load_file(self, log_file, message_name_filter_list, parse_header_only=False):
        """ load and parse an ULog file into memory """
        if isinstance(log_file, str):
            self._file_handle = open(log_file, "rb") #pylint: disable=consider-using-with
        else:
            self._file_handle = log_file

        # parse the whole file
        self._read_file_header()
        self._last_timestamp = self._start_timestamp
        self._read_file_definitions()

        if self._debug:
            print("header end offset: {:}".format(self._file_handle.tell()))

        if parse_header_only:
            self._file_handle.close()
            del self._file_handle
            return

        if self.has_data_appended and len(self._appended_offsets) > 0:
            if self._debug:
                print('This file has data appended')
            for offset in self._appended_offsets:
                self._read_file_data(message_name_filter_list, read_until=offset)
                try:
                    self._file_handle.seek(offset)
                except (OverflowError, ValueError, OSError):
                    # bogus appended offset from a corrupt FLAG_BITS -> corruption
                    self._file_corrupt = True
                    break

        # read the whole file, or the rest if data appended
        self._read_file_data(message_name_filter_list)

        self._file_handle.close()
        del self._file_handle

    def _read_file_header(self):
        header_data = self._file_handle.read(16)
        if len(header_data) != 16:
            raise ULogException("Invalid file format (Header too short)")
        if header_data[:7] != self.HEADER_BYTES:
            raise ULogException("Invalid file format (Failed to parse header)")
        self._file_version, = struct.unpack('B', header_data[7:8])
        if self._file_version > 1:
            print("Warning: unknown file version. Will attempt to read it anyway")

        # read timestamp
        self._start_timestamp, = ULog._unpack_uint64(header_data[8:])

    def _read_file_definitions(self):
        header = self._MessageHeader()
        while True:
            data = self._file_handle.read(3)
            if not data:
                break
            try:
                header.initialize(data)
                data = self._file_handle.read(header.msg_size)
                if header.msg_type == self.MSG_TYPE_INFO:
                    msg_info = self._MessageInfo(data, header)
                    self._msg_info_dict[msg_info.key] = msg_info.value
                    self._msg_info_dict_types[msg_info.key] = msg_info.type
                elif header.msg_type == self.MSG_TYPE_INFO_MULTIPLE:
                    msg_info = self._MessageInfo(data, header, is_info_multiple=True)
                    self._add_message_info_multiple(msg_info)
                elif header.msg_type == self.MSG_TYPE_FORMAT:
                    msg_format = self.MessageFormat(data, header)
                    self._message_formats[msg_format.name] = msg_format
                elif header.msg_type == self.MSG_TYPE_PARAMETER:
                    msg_info = self._MessageInfo(data, header)
                    self._initial_parameters[msg_info.key] = msg_info.value
                elif header.msg_type == self.MSG_TYPE_PARAMETER_DEFAULT:
                    msg_param = self._MessageParameterDefault(data, header)
                    self._add_parameter_default(msg_param)
                elif header.msg_type in (self.MSG_TYPE_ADD_LOGGED_MSG,
                      self.MSG_TYPE_LOGGING, self.MSG_TYPE_LOGGING_TAGGED):
                    self._file_handle.seek(-(3+header.msg_size), 1)
                    break # end of section
                elif header.msg_type == self.MSG_TYPE_FLAG_BITS:
                    # make sure this is the first message in the log
                    if self._file_handle.tell() != 16 + 3 + header.msg_size:
                        print('Error: FLAGS_BITS message must be first message. Offset:',
                              self._file_handle.tell())
                    msg_flag_bits = self._MessageFlagBits(data, header)
                    self._compat_flags = msg_flag_bits.compat_flags
                    self._incompat_flags = msg_flag_bits.incompat_flags
                    self._appended_offsets = msg_flag_bits.appended_offsets
                    if self._debug:
                        print('compat flags:  ', self._compat_flags)
                        print('incompat flags:', self._incompat_flags)
                        print('appended offsets:', self._appended_offsets)

                    # check if there are bits set that we don't know
                    unknown_incompat_flag_msg = \
                    "Unknown incompatible flag set: cannot parse the log"
                    if self._incompat_flags[0] & ~1:
                        raise ULogIncompatibleFlagException(unknown_incompat_flag_msg)
                    for i in range(1, 8):
                        if self._incompat_flags[i]:
                            raise ULogIncompatibleFlagException(unknown_incompat_flag_msg)

                else:
                    if self._debug:
                        print('read_file_definitions: unknown message type: %i (%s)' %
                              (header.msg_type, chr(header.msg_type)))
                        file_position = self._file_handle.tell()
                        print('file position: %i (0x%x) msg size: %i' % (
                            file_position, file_position, header.msg_size))
                    if self._check_packet_corruption(header):
                        # seek back to advance only by a single byte instead of
                        # skipping the message
                        self._file_handle.seek(-2-header.msg_size, 1)

            except UnicodeDecodeError:
                raise # strict string parsing (disable_str_exceptions=False)
            except (IndexError, ValueError, KeyError, OverflowError, struct.error):
                if not self._file_corrupt:
                    print("File corruption detected while reading file definitions!")
                    self._file_corrupt = True

    def _find_sync(self, last_n_bytes=-1):
        """
        read the file from a given location until the end of sync_byte sequence is found
            or an end condition is met(reached EOF or searched all last_n_bytes).
        :param last_n_bytes: optional arg to search only last_n_bytes for sync_bytes.
            when provided, _find_sync searches for sync_byte sequence in the last_n_bytes
            from current location, else, from current location till end of file.
        return true if successful, else return false and seek back to initial position and
            set _has_sync to false if searched till end of file
        """
        sync_seq_found = False
        initial_file_position = self._file_handle.tell()
        current_file_position = initial_file_position

        search_chunk_size = 512 # number of bytes that are searched at once

        if last_n_bytes != -1:
            current_file_position = self._file_handle.seek(-last_n_bytes, 1)
            search_chunk_size = last_n_bytes

        chunk = self._file_handle.read(search_chunk_size)
        while len(chunk) >= len(ULog.SYNC_BYTES):
            current_file_position += len(chunk)
            chunk_index = chunk.find(ULog.SYNC_BYTES)
            if chunk_index >= 0:
                if self._debug:
                    print("Found sync at %i" % (current_file_position - len(chunk) + chunk_index))
                # seek to end of sync sequence and break
                current_file_position = self._file_handle.seek(current_file_position - len(chunk)\
                         + chunk_index + len(ULog.SYNC_BYTES), 0)
                sync_seq_found = True
                break

            if last_n_bytes != -1:
                # we read the whole last_n_bytes and did not find sync
                break

            # seek back 7 bytes to handle boundary condition and read next chunk
            current_file_position = self._file_handle.seek(-(len(ULog.SYNC_BYTES)-1), 1)
            chunk = self._file_handle.read(search_chunk_size)

        if not sync_seq_found:
            current_file_position = self._file_handle.seek(initial_file_position, 0)

            if last_n_bytes == -1:
                self._has_sync = False
                if self._debug:
                    print("Failed to find sync in file from %i" % initial_file_position)
            else:
                if self._debug:
                    print("Failed to find sync in (%i, %i)" %\
                        (initial_file_position - last_n_bytes, initial_file_position))
        else:
            # declare file corrupt if we skipped bytes to sync sequence
            self._file_corrupt = True

        return sync_seq_found

    def _read_file_data(self, message_name_filter_list, read_until=None):
        """
        read the file data section
        :param read_until: an optional file offset: if set, parse only up to
                           this offset (smaller than)
        """

        if read_until is None:
            read_until = 1 << 50 # make it larger than any possible log file

        try:
            # pre-init reusable objects
            header = self._MessageHeader()
            msg_data = self._MessageData()

            curr_file_pos = self._file_handle.tell()

            while True:
                data = self._file_handle.read(3)
                curr_file_pos += len(data)
                header.initialize(data)
                data = self._file_handle.read(header.msg_size)
                curr_file_pos += len(data)
                if len(data) < header.msg_size:
                    break # less data than expected. File is most likely cut

                if curr_file_pos > read_until:
                    if self._debug:
                        print('read until offset=%i done, current pos=%i' %
                              (read_until, curr_file_pos))
                    break

                try:
                    if header.msg_type == self.MSG_TYPE_INFO:
                        msg_info = self._MessageInfo(data, header)
                        self._msg_info_dict[msg_info.key] = msg_info.value
                        self._msg_info_dict_types[msg_info.key] = msg_info.type
                    elif header.msg_type == self.MSG_TYPE_INFO_MULTIPLE:
                        msg_info = self._MessageInfo(data, header, is_info_multiple=True)
                        self._add_message_info_multiple(msg_info)
                    elif header.msg_type == self.MSG_TYPE_PARAMETER:
                        msg_info = self._MessageInfo(data, header)
                        self._changed_parameters.append((self._last_timestamp,
                                                         msg_info.key, msg_info.value))
                    elif header.msg_type == self.MSG_TYPE_PARAMETER_DEFAULT:
                        msg_param = self._MessageParameterDefault(data, header)
                        self._add_parameter_default(msg_param)
                    elif header.msg_type == self.MSG_TYPE_ADD_LOGGED_MSG:
                        msg_add_logged = self._MessageAddLogged(data, header,
                                                                self._message_formats)
                        if (message_name_filter_list is None or
                                msg_add_logged.message_name in message_name_filter_list):
                            self._subscriptions[msg_add_logged.msg_id] = msg_add_logged
                        else:
                            self._filtered_message_ids.add(msg_add_logged.msg_id)
                    elif header.msg_type == self.MSG_TYPE_LOGGING:
                        msg_logging = self.MessageLogging(data, header)
                        self._logged_messages.append(msg_logging)
                    elif header.msg_type == self.MSG_TYPE_LOGGING_TAGGED:
                        msg_log_tagged = self.MessageLoggingTagged(data, header)
                        if msg_log_tagged.tag in self._logged_messages_tagged:
                            self._logged_messages_tagged[msg_log_tagged.tag].append(msg_log_tagged)
                        else:
                            self._logged_messages_tagged[msg_log_tagged.tag] = [msg_log_tagged]
                    elif header.msg_type == self.MSG_TYPE_DATA:
                        has_corruption = msg_data.initialize(data, header, self._subscriptions,
                                                             self)
                        if has_corruption:
                            self._file_corrupt = True
                        elif msg_data.timestamp > self._last_timestamp:
                            self._last_timestamp = msg_data.timestamp
                    elif header.msg_type == self.MSG_TYPE_DROPOUT:
                        msg_dropout = self.MessageDropout(data, header,
                                                          self._last_timestamp)
                        self._dropouts.append(msg_dropout)
                    elif header.msg_type == self.MSG_TYPE_SYNC:
                        self._sync_seq_cnt = self._sync_seq_cnt + 1
                    else:
                        if self._debug:
                            print('_read_file_data: unknown message type: %i (%s)' %
                                  (header.msg_type, chr(header.msg_type)))
                            print('file position: %i msg size: %i' % (
                                curr_file_pos, header.msg_size))

                        if self._check_packet_corruption(header):
                            # seek back to advance only by a single byte instead of
                            # skipping the message
                            curr_file_pos = self._file_handle.seek(-2-header.msg_size, 1)

                            # try recovery with sync sequence in case of unknown msg_type
                            if self._has_sync:
                                self._find_sync()
                        else:
                            # seek back msg_size to look for sync sequence in payload
                            if self._has_sync:
                                self._find_sync(header.msg_size)

                except UnicodeDecodeError:
                    raise # strict string parsing (disable_str_exceptions=False)
                except (IndexError, KeyError, ValueError, OverflowError):
                    if not self._file_corrupt:
                        print("File corruption detected while reading file data!")
                        self._file_corrupt = True

        except struct.error:
            pass #we read past the end of the file

        # convert into final representation
        while self._subscriptions:
            _, value = self._subscriptions.popitem()
            if len(value.buffer) > 0: # only add if we have data
                data_item = ULog.Data(value)
                self._data_list.append(data_item)
        # Sorting is necessary to be able to compare two ULogs correctly
        self.data_list.sort(key=lambda ds: (ds.name, ds.multi_id))

    def _check_packet_corruption(self, header):
        """
        check for data corruption based on an unknown message type in the header
        set _file_corrupt flag to true if a corrupt packet is found
        We need to handle 2 cases:
        - corrupt file (we do our best to read the rest of the file)
        - new ULog message type got added (we just want to skip the message)
        return true if packet associated with header is corrupt, else return false
        """
        data_corrupt = False
        if header.msg_type == 0 or header.msg_size == 0 or header.msg_size > 10000:
            if not self._file_corrupt and self._debug:
                print('File corruption detected')
            data_corrupt = True
            self._file_corrupt = True

        return data_corrupt

    def get_version_info(self, key_name='ver_sw_release'):
        """
        get the (major, minor, patch, type) version information as tuple.
        Returns None if not found
        definition of type is:
         >= 0: development
         >= 64: alpha version
         >= 128: beta version
         >= 192: RC version
         == 255: release version
        """
        if key_name in self._msg_info_dict:
            val = self._msg_info_dict[key_name]
            return ((val >> 24) & 0xff, (val >> 16) & 0xff, (val >> 8) & 0xff, val & 0xff)
        return None

    def get_version_info_str(self, key_name='ver_sw_release'):
        """
        get version information in the form 'v1.2.3 (RC)', or None if version
        tag either not found or it's a development version
        """
        version = self.get_version_info(key_name)
        if not version is None and version[3] >= 64:
            type_str = ''
            if version[3] < 128: type_str = ' (alpha)'
            elif version[3] < 192: type_str = ' (beta)'
            elif version[3] < 255: type_str = ' (RC)'
            return 'v{}.{}.{}{}'.format(version[0], version[1], version[2], type_str)
        return None
