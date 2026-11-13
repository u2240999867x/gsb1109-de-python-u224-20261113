""" Main Module to load and parse an ULog file """

import struct
import copy
import fnmatch
import re
import sys
import contextlib

from collections.abc import Mapping

import numpy as np
from .exceptions import ULogException, ULogIncompatibleFlagException
#pylint: disable=too-many-instance-attributes, unused-argument, missing-docstring
#pylint: disable=protected-access, too-many-branches, too-many-lines

# Parameter/info names that can carry device serial numbers, device identities
# or operator information. They are kept out of filtered exports; the only way
# to influence this is to select the remaining parameters explicitly.
_SENSITIVE_NAME_RE = re.compile(
    r'(SERIAL|UUID|OPERATOR|PILOT|CALLSIGN|ARM_AUTH_ID|'
    r'^CAL_(?:GYRO|ACC|MAG|BARO)\d+_ID$|'
    r'^MAV_(?:SYS|COMP|RADIO|SIK_RADIO)_ID$)',
    re.IGNORECASE)

# Field names that must never leave the machine in a filtered export. Matching
# is done on the last component of a (possibly nested) flattened field name, so
# e.g. 'esc[0].serial_number' and 'battery_status.serial_number' are caught.
_SENSITIVE_FIELD_RE = re.compile(
    r'^(?:serial|serial_number|device_serial|hw_serial|callsign|pilot|'
    r'operator|operator_name|uas_id|flight_uuid)$',
    re.IGNORECASE)


class _UnrepresentableLayout(Exception):
    """ Raised when a whitelist cannot be expressed in a ULog format. """


def _layout_error_message(error):
    """ Return a report-friendly reason for a failed layout build. """
    if isinstance(error, _UnrepresentableLayout):
        return str(error)
    if isinstance(error, KeyError):
        return 'message format not found'
    return 'recursive message format definition'


class ULogExportReport(object):
    """ Result of a filtered :meth:`ULog.write_ulog` export.

    ``topics`` maps a topic name to the list of multi ids that were written.
    ``skipped_topics`` maps a topic (or a ``(topic, multi_id)`` tuple) to the
    reason it could not be retained. ``empty_topics`` lists ``topic[id]`` keys
    whose subscription and reduced definition were written although the window
    had no samples. ``unmatched_fields`` lists whitelist entries that did not
    occur in the corresponding topic definition,
    ``omitted_sensitive_fields`` lists requested fields that were suppressed by
    the privacy rules, and ``zero_filled_fields`` lists array slots kept for
    layout compatibility but written as zero.
    """

    def __init__(self):
        self.topics = {}
        self.skipped_topics = {}
        self.empty_topics = {}
        self.unmatched_fields = {}
        self.omitted_sensitive_fields = {}
        self.written_fields = {}
        self.zero_filled_fields = {}
        self.parameters = {}
        self.parameter_changes = []
        self.parameter_count = 0
        self.format_count = 0
        self.start_timestamp = None
        self.end_timestamp = None

    @property
    def kept_topics(self):
        """ Alias for ``topics``. """
        return self.topics

    @property
    def unretained_topics(self):
        """ Alias for ``skipped_topics``. """
        return self.skipped_topics

    def summary(self):
        """ Return a short human readable summary. """
        kept = ', '.join(
            '{}[{}]'.format(name, ','.join(str(i) for i in multi_ids))
            for name, multi_ids in sorted(self.topics.items())) or 'none'
        skipped = ', '.join(
            '{} ({})'.format(name, reason)
            for name, reason in sorted(self.skipped_topics.items(),
                                       key=lambda item: str(item[0]))) or 'none'
        empty = ', '.join(
            '{} ({})'.format(name, reason)
            for name, reason in sorted(self.empty_topics.items())) or 'none'
        return 'kept: {}; empty: {}; skipped: {}'.format(kept, empty, skipped)


class _ExportMessageFormat(object):
    """ Minimal MessageFormat look-alike used for reduced export definitions. """

    def __init__(self, name, fields):
        self.name = name
        self.fields = fields


class _ExportLayout(object):
    """ A reduced top-level format plus the flat leaves it writes. """

    def __init__(self, fields, leaves):
        self.fields = fields
        # list of (flat field name or None for zero fill, type string)
        self.leaves = leaves


def _layout_field_names(layout):
    """ Return the field names a reduced layout actually carries values for. """
    return [leaf for leaf, _ in layout.leaves if leaf is not None]


def _layout_zero_filled(layout):
    """ Return array slots that are kept only to preserve the layout shape. """
    return [index for index, (leaf, _) in enumerate(layout.leaves) if leaf is None]


class _ExportLayoutBuilder(object):
    """ Build reduced (nested) message formats for a field whitelist.

    All selections are applied to relative leaf names of a message format, so
    the same nested type can be reduced consistently for every occurrence.
    """

    def __init__(self, ulog_object, top_level_names=None):
        self._ulog = ulog_object
        self._cache = {}
        self._top_level_names = set(top_level_names or ())
        self._used_names = set(ulog_object.message_formats)
        self._formats = []
        self._expanding = set()
        self._counter = 0
        self.unmatched = []
        self.blocked = []
        self._all_leaves = []

    def build(self, topic_name, selection):
        """ Return an :class:`_ExportLayout` for a topic, or None if empty. """
        self.unmatched = []
        self.blocked = []
        self._all_leaves = self._flattened_leaves(topic_name)
        patterns = selection
        if patterns is not None:
            patterns = list(patterns)
            requested = list(patterns)
            if 'timestamp' not in patterns:
                # A regular reader needs a timestamp field to order samples.
                patterns.append('timestamp')
            for pattern in patterns:
                matched = [leaf for leaf in self._all_leaves
                           if self._pattern_matches(pattern, leaf)]
                if not matched:
                    self.unmatched.append(pattern)
                elif all(ULog._is_sensitive_field(leaf) for leaf in matched):
                    self.blocked.append(pattern)

            if requested and all(pattern in self.blocked for pattern in requested):
                raise _UnrepresentableLayout('only privacy-sensitive fields requested')

        top_format = self._ulog.message_formats[topic_name]
        formats_before = len(self._formats)
        cache_before = dict(self._cache)
        fields, leaves = self._reduce_fields(top_format.fields, patterns)
        written = [leaf for leaf, _ in leaves if leaf is not None]
        if 'timestamp' not in written:
            # Every readable data topic needs a top-level timestamp; without it
            # the data section cannot be placed in time.
            del self._formats[formats_before:]
            self._cache = cache_before
            raise _UnrepresentableLayout('no top-level timestamp field')
        if not fields or not any(leaf != 'timestamp' and
                                 not leaf.endswith('.timestamp') for leaf in written):
            # Roll back nested formats and cache entries of a topic that
            # cannot be retained, so no dangling synthetic names are emitted.
            del self._formats[formats_before:]
            self._cache = cache_before
            return None
        self._formats.append(_ExportMessageFormat(topic_name, fields))
        return _ExportLayout(fields, leaves)

    def formats(self):
        """ Return all synthetic nested formats that were created. """
        result = []
        names = set()
        for message_format in self._formats:
            if message_format.name in names:
                continue
            names.add(message_format.name)
            result.append(message_format)
        return result

    def _flattened_leaves(self, type_name, prefix='', _stack=None):
        """ Flatten a format definition to its leaf field names. """
        stack = set() if _stack is None else _stack
        if type_name in stack:
            raise RecursionError('recursive message format definition')
        stack.add(type_name)
        leaves = []
        for field_type, array_size, field_name in self._ulog.message_formats[type_name].fields:
            if field_name.startswith('_padding'):
                continue
            if field_type in ULog._UNPACK_TYPES:
                if array_size > 0:
                    leaves.extend(prefix + field_name + '[{}]'.format(i)
                                  for i in range(array_size))
                else:
                    leaves.append(prefix + field_name)
            elif array_size > 0:
                for i in range(array_size):
                    leaves.extend(self._flattened_leaves(
                        field_type, prefix + field_name + '[{}].'.format(i), stack))
            else:
                leaves.extend(self._flattened_leaves(
                    field_type, prefix + field_name + '.', stack))
        stack.discard(type_name)
        return leaves

    @staticmethod
    def _pattern_matches(pattern, leaf):
        """ Return True if a whitelist pattern selects a flattened leaf. """
        if pattern in ('*', '**'):
            return True
        if leaf == pattern or leaf.startswith(pattern + '.') or leaf.startswith(pattern + '['):
            return True
        return fnmatch.fnmatchcase(leaf, pattern)

    def _child_patterns(self, patterns, field_name):
        """ Map parent patterns to relative patterns of a nested type.

        Returns ``(matched, relative_patterns, elements)``. ``relative_patterns``
        is None when the whole nested type is selected. ``elements`` is None
        when every array element is selected, otherwise it is the set of
        requested element indices. A ULog format has a single definition per
        nested type, so members below each element are reduced consistently.
        """
        if patterns is None:
            return True, None, None
        result = []
        elements = set()
        matched = False
        for pattern in patterns:
            relative, element = self._relative_pattern(pattern, field_name)
            if relative is not None:
                matched = True
                if element is not None:
                    elements.add(element)
                if relative not in result:
                    result.append(relative)
        if elements:
            return True, result, elements
        return matched, result, None

    @staticmethod
    def _relative_pattern(pattern, field_name):
        """ Return ``(relative pattern, element index)`` below ``field_name``. """
        if pattern in ('*', '**'):
            return '*', None
        if pattern == field_name:
            return '*', None
        escaped = re.escape(field_name)
        element_match = re.fullmatch(escaped + r'\[(\d+)\]', pattern)
        if element_match:
            return '*', int(element_match.group(1))
        nested = re.match(r'^' + escaped + r'\[(\d+)\]\.(.+)$', pattern)
        if nested:
            return nested.group(2), int(nested.group(1))
        nested = re.match(r'^' + escaped + r'(?:\[\*\])?\.(.+)$', pattern)
        if nested:
            return nested.group(1), None
        if fnmatch.fnmatchcase(field_name, pattern):
            return '*', None
        if any(fnmatch.fnmatchcase(field_name + suffix, pattern)
               for suffix in ('[0]', '.x')):
            return '*', None
        return None, None

    def _reduce_fields(self, format_fields, patterns):
        """ Reduce a list of format fields; returns (fields, leaves). """
        fields = []
        leaves = []
        for field_type, array_size, field_name in format_fields:
            if field_name.startswith('_padding'):
                continue
            if field_type in ULog._UNPACK_TYPES:
                if array_size > 0:
                    allowed = [i for i in range(array_size)
                               if self._leaf_allowed(patterns,
                                                     '{}[{}]'.format(field_name, i))]
                    if not allowed:
                        continue
                    fields.append((field_type, array_size, field_name))
                    # A ULog format cannot express a hole in an array: keep
                    # the array rectangular and zero-fill unselected slots.
                    for i in range(array_size):
                        leaf = '{}[{}]'.format(field_name, i)
                        leaves.append((leaf if i in allowed else None, field_type))
                else:
                    if not self._leaf_allowed(patterns, field_name):
                        continue
                    fields.append((field_type, 0, field_name))
                    leaves.append((field_name, field_type))
                continue

            matched, child_patterns, elements = self._child_patterns(patterns, field_name)
            if not matched:
                continue
            child_name, child_leaves = self._reduce_type_name(field_type, child_patterns)
            if not child_leaves:
                continue
            if array_size > 0:
                # Each element of a nested array has the same definition. If
                # only some elements or members were selected by name, keep
                # the array rectangular and zero-fill unselected slots.
                fields.append((child_name, array_size, field_name))
                for i in range(array_size):
                    keep_element = elements is None or i in elements
                    for relative, child_type in child_leaves:
                        if relative is None or not keep_element:
                            leaf = None
                        else:
                            full_leaf = '{0}[{1}].{2}'.format(field_name, i, relative)
                            leaf = full_leaf if self._leaf_allowed(patterns, full_leaf) \
                                else None
                        leaves.append((leaf, child_type))
            else:
                fields.append((child_name, 0, field_name))
                for relative, child_type in child_leaves:
                    leaf = None if relative is None else \
                        '{}.{}'.format(field_name, relative)
                    leaves.append((leaf, child_type))
        return fields, leaves

    def _leaf_allowed(self, patterns, leaf):
        """ Return True if a leaf may be written. """
        if ULog._is_sensitive_field(leaf):
            return False
        if patterns is None:
            return True
        return any(self._pattern_matches(pattern, leaf) for pattern in patterns)

    def _reduce_type_name(self, type_name, patterns):
        """ Reduce a nested type and return (synthetic type name, leaves). """
        signature = (type_name, None if patterns is None else tuple(sorted(set(patterns))))
        if signature in self._cache:
            return self._cache[signature]
        if signature in self._expanding:
            raise RecursionError('recursive message format definition')

        original_fields = self._ulog.message_formats[type_name].fields
        # Guard against mutually recursive FORMAT definitions in a corrupt log.
        # The cache entry is only set after the expansion is complete.
        self._expanding.add(signature)
        try:
            fields, leaves = self._reduce_fields(original_fields, patterns)
        finally:
            self._expanding.discard(signature)
        if not fields:
            result = (None, [])
            self._cache[signature] = result
            return result

        clean_original = [field for field in original_fields
                          if not field[2].startswith('_padding')]
        if fields == clean_original and type_name not in self._top_level_names:
            reduced_name = type_name
        else:
            # Either the layout changed or the name is reserved for a reduced
            # top-level topic; both need a distinct synthetic name.
            reduced_name = self._new_name(type_name)
        self._formats.append(_ExportMessageFormat(reduced_name, fields))
        result = (reduced_name, leaves)
        self._cache[signature] = result
        return result

    def _new_name(self, type_name):
        """ Allocate a collision-free synthetic format name. """
        while True:
            self._counter += 1
            candidate = '{}__red{}'.format(type_name, self._counter)
            if candidate not in self._used_names:
                self._used_names.add(candidate)
                return candidate

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

    def write_ulog(self, log_file, field_whitelist=None, time_start=None, time_end=None,
                   time_window=None, time_s=None, time_e=None, start_timestamp=None,
                   end_timestamp=None, topics=None, topic_fields=None, fields=None,
                   parameter_whitelist=None, include_parameters=True,
                   include_info=False, include_logged_messages=False,
                   include_dropouts=False):
        """ Write current data back into a ulog file.

        Without filtering arguments this reproduces the loaded log as before.
        With a field whitelist and/or a time window it writes a reduced log that
        can be read by any regular ULog reader (including this class):

        :param field_whitelist: either a list of topic names to keep in full,
               or a dict mapping a topic name to a list of field names. Field
               names may be leaf names (``q[1]``), array/base names (``q``),
               nested prefixes (``current``) or shell-style patterns
               (``current.*``). ``None`` keeps every topic and every field that
               is not privacy-sensitive. A leaf is only ever a permission, so
               sensitive leaves such as ``serial_number`` or ``callsign`` are
               never written, even if they are listed.
        :param time_start: first sample timestamp to write, in microseconds
               (inclusive). ``None`` starts at the first sample.
        :param time_end: last sample timestamp (exclusive), in microseconds.
               ``None`` writes up to the end of the log.
        :param time_window: optional ``(start, end)`` tuple alternative to
               time_start/time_end.
        :param time_s: alias for time_start, in seconds.
        :param time_e: alias for time_end, in seconds.
        :param start_timestamp: alias for time_start (microseconds).
        :param end_timestamp: alias for time_end (microseconds).
        :param topics: alias to restrict the export to the given topic names.
        :param topic_fields: alias for field_whitelist.
        :param fields: alias for field_whitelist.
        :param parameter_whitelist: optional list of parameter names. By default
               all non-sensitive parameters are written.
        :param include_parameters: write the effective parameter state at the
               cut point (True by default).
        :param include_info: also write non-sensitive information messages.
        :param include_logged_messages: also write log strings (off by default,
               as they can contain operator information).
        :param include_dropouts: write dropout messages inside the window.
        :return: a :class:`ULogExportReport` describing what was retained.
        """
        options = self._normalize_export_options(
            field_whitelist, time_start, time_end, time_window, time_s, time_e,
            start_timestamp, end_timestamp, topics, topic_fields, fields,
            parameter_whitelist, include_parameters, include_info,
            include_logged_messages, include_dropouts)

        if not options['filtered']:
            self._write_full_ulog(log_file)
            return None

        report = ULogExportReport()
        export = self._build_export(options, report)

        if isinstance(log_file, str):
            handle = open(log_file, "wb")
        else:
            handle = contextlib.nullcontext(log_file)

        with handle as ulog_file:
            self._write_file_header(ulog_file, export['header_timestamp'])
            self._write_export_flags(ulog_file)
            for message_format in export['formats']:
                self._write_one_format_message(ulog_file, message_format)
            if options['include_parameters']:
                for name, value in export['parameter_state'].items():
                    data = self._make_parameter_data(name, value)
                    ulog_file.write(struct.pack('<HB', len(data), self.MSG_TYPE_PARAMETER))
                    ulog_file.write(data)
            if options['include_info']:
                self._write_export_info_messages(ulog_file)
            for subscription in export['subscriptions']:
                data = bytearray()
                data.extend(struct.pack('<BH', subscription['multi_id'],
                                        subscription['msg_id']))
                data.extend(bytes(subscription['name'], 'utf-8'))
                ulog_file.write(struct.pack('<HB', len(data), self.MSG_TYPE_ADD_LOGGED_MSG))
                ulog_file.write(data)
            for _, _, buffer in export['data_items']:
                ulog_file.write(buffer)

        report.format_count = len(export['formats'])
        report.parameter_count = (len(export['parameter_state']) +
                                  len(export['parameter_changes']))
        report.parameters = dict(export['parameter_state'])
        report.parameter_changes = list(export['parameter_changes'])
        report.start_timestamp = export['start_timestamp']
        report.end_timestamp = export['end_timestamp']
        return report

    def write_ulog_filtered(self, log_file, **kwargs):
        """ Alias for :meth:`write_ulog` with filtering arguments. """
        return self.write_ulog(log_file, **kwargs)

    @staticmethod
    def _normalize_export_options(field_whitelist, time_start, time_end, time_window,
                                  time_s, time_e, start_timestamp, end_timestamp,
                                  topics, topic_fields, fields, parameter_whitelist,
                                  include_parameters, include_info,
                                  include_logged_messages, include_dropouts):
        """ Validate and normalize the filtering arguments of write_ulog. """
        aliases = [value for value in (field_whitelist, topic_fields, fields,
                                       topics) if value is not None]
        if len(aliases) > 1:
            raise ValueError('pass only one of field_whitelist/topic_fields/fields/topics')

        if time_window is not None:
            try:
                window_start, window_end = time_window
            except (TypeError, ValueError) as error:
                raise ValueError('time_window must be a (start, end) pair') from error
        else:
            window_start = window_end = None

        whitelist = aliases[0] if aliases else None
        normalized = None
        if whitelist is not None:
            if isinstance(whitelist, Mapping):
                normalized = {}
                for name, selection in whitelist.items():
                    if not isinstance(name, str):
                        raise ValueError('topic names in field_whitelist must be strings')
                    if selection is None:
                        normalized[name] = None
                    elif isinstance(selection, str):
                        normalized[name] = [item.strip() for item in selection.split(',')
                                            if item.strip()]
                    else:
                        normalized[name] = [str(field) for field in selection]
            elif isinstance(whitelist, str):
                normalized = {item.strip(): None for item in whitelist.split(',')
                              if item.strip()}
            else:
                normalized = {}
                for name in whitelist:
                    if not isinstance(name, str):
                        raise ValueError('topic names in field_whitelist must be strings')
                    normalized[name] = None

        starts = []
        if time_start is not None:
            starts.append(int(time_start))
        if start_timestamp is not None:
            starts.append(int(start_timestamp))
        if time_s is not None:
            starts.append(int(round(time_s * 1e6)))
        if time_window is not None:
            starts.append(int(window_start))
        ends = []
        if time_end is not None:
            ends.append(int(time_end))
        if end_timestamp is not None:
            ends.append(int(end_timestamp))
        if time_e is not None:
            ends.append(int(round(time_e * 1e6)))
        if time_window is not None:
            ends.append(int(window_end))
        if len(set(starts)) > 1 or len(set(ends)) > 1:
            raise ValueError('conflicting start/end time arguments')
        start = starts[0] if starts else None
        end = ends[0] if ends else None
        if start is not None and end is not None and end < start:
            raise ValueError('time_end must not be smaller than time_start')

        if parameter_whitelist is not None:
            if isinstance(parameter_whitelist, str):
                parameter_whitelist = [parameter_whitelist]
            parameter_whitelist = set(parameter_whitelist)

        filtered = (normalized is not None or start is not None or end is not None or
                    parameter_whitelist is not None or include_info or
                    include_logged_messages or include_dropouts or
                    not include_parameters)
        return {
            'whitelist': normalized,
            'start': start,
            'end': end,
            'parameter_whitelist': parameter_whitelist,
            'include_parameters': include_parameters,
            'include_info': include_info,
            'include_logged_messages': include_logged_messages,
            'include_dropouts': include_dropouts,
            'filtered': filtered,
        }

    def _write_full_ulog(self, log_file):
        """ Write all loaded data, preserving the historical write_ulog behaviour. """
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

    @staticmethod
    def _is_sensitive_name(name):
        """ Return True if a parameter/info name carries private information. """
        return bool(_SENSITIVE_NAME_RE.search(name))

    @staticmethod
    def _is_sensitive_field(flat_name):
        """ Return True if a flattened data field is privacy-sensitive. """
        leaf = flat_name.rsplit('.', 1)[-1]
        leaf = re.sub(r'\[\d+\]$', '', leaf)
        return bool(_SENSITIVE_FIELD_RE.match(leaf))

    def _write_export_flags(self, file):
        """ Write flags for a standalone (non-appended) filtered log. """
        compat_flags = list(self._compat_flags)
        compat_flags[0] &= ~0x01 # no default parameter messages are written
        data = bytearray()
        data.extend(struct.pack('<' + 'B' * 8, *compat_flags))
        data.extend(struct.pack('<' + 'B' * 8, *([0] * 8)))
        data.extend(struct.pack('<' + 'Q' * 3, *([0] * 3)))
        file.write(struct.pack('<HB', len(data), self.MSG_TYPE_FLAG_BITS))
        file.write(data)

    def _write_one_format_message(self, file, message_format):
        """ Write a single MessageFormat-like object to the definition section. """
        data = bytearray()
        data.extend(bytes(message_format.name + ':', 'utf-8'))
        for field_type, array_size, field_name in message_format.fields:
            if array_size > 0:
                encoded_field = '%s[%d] %s;' % (field_type, array_size, field_name)
            else:
                encoded_field = '%s %s;' % (field_type, field_name)
            data.extend(bytes(encoded_field, 'utf-8'))
        file.write(struct.pack('<HB', len(data), self.MSG_TYPE_FORMAT))
        file.write(data)

    def _write_export_info_messages(self, file):
        """ Write non-sensitive info and info-multiple messages. """
        for key, value in self._msg_info_dict.items():
            if self._is_sensitive_name(key):
                continue
            value_type = self._msg_info_dict_types[key]
            data = self._make_info_message_data(value_type + ' ' + key, value, value_type)
            file.write(struct.pack('<HB', len(data), self.MSG_TYPE_INFO))
            file.write(data)
        for key, value_sets in self._msg_info_multiple_dict.items():
            if self._is_sensitive_name(key):
                continue
            value_type = self._msg_info_multiple_dict_types[key]
            for value_set in value_sets:
                continued = False
                for value in value_set:
                    data = self._make_info_message_data(value_type + ' ' + key, value,
                                                        value_type, continued)
                    file.write(struct.pack('<HB', len(data), self.MSG_TYPE_INFO_MULTIPLE))
                    file.write(data)
                    continued = True

    def _build_export(self, options, report):
        """ Build reduced formats, subscriptions, parameter state and data items. """
        whitelist = options['whitelist']
        start = options['start']
        end = options['end']

        datasets = {}
        for data_set in self._data_list:
            datasets.setdefault(data_set.name, {})[data_set.multi_id] = data_set

        if whitelist is None:
            # No topic filter: an untouched, non-sensitive view of all data.
            topic_selection = {name: None for name in datasets}
        else:
            # Only topics named by the caller are considered. A named topic
            # that never logged data still gets its reduced definition, so a
            # regular reader can decode the advertised topic types.
            topic_selection = dict(whitelist)

        layout_builder = _ExportLayoutBuilder(self, topic_selection)
        selected_datasets = []
        for topic_name, selection in topic_selection.items():
            if topic_name not in datasets:
                if topic_name not in self._message_formats:
                    report.skipped_topics[topic_name] = 'topic not found'
                    continue
                try:
                    layout = layout_builder.build(topic_name, selection)
                except (_UnrepresentableLayout, KeyError, RecursionError) as error:
                    report.skipped_topics[topic_name] = _layout_error_message(error)
                    continue
                if layout is None:
                    report.skipped_topics[topic_name] = 'no exportable fields'
                    continue
                if selection is not None:
                    report.unmatched_fields[topic_name] = list(layout_builder.unmatched)
                    report.omitted_sensitive_fields[topic_name] = \
                        list(layout_builder.blocked)
                # Keep the reduced definition so a plain reader can decode the
                # topic even though this window has no samples for it. The
                # subscription is only written if the topic logged data
                # somewhere, which is the case for every name in datasets.
                report.written_fields[topic_name] = _layout_field_names(layout)
                if _layout_zero_filled(layout):
                    report.zero_filled_fields[topic_name] = _layout_zero_filled(layout)
                report.empty_topics[topic_name] = 'no samples in time window'
                continue
            if topic_name not in self._message_formats:
                report.skipped_topics[topic_name] = 'message format not found'
                continue
            try:
                layout = layout_builder.build(topic_name, selection)
            except (_UnrepresentableLayout, KeyError, RecursionError) as error:
                report.skipped_topics[topic_name] = _layout_error_message(error)
                continue
            if layout is None:
                report.skipped_topics[topic_name] = 'no exportable fields'
                continue
            if selection is not None:
                report.unmatched_fields[topic_name] = list(layout_builder.unmatched)
                report.omitted_sensitive_fields[topic_name] = \
                    list(layout_builder.blocked)
            report.written_fields[topic_name] = _layout_field_names(layout)
            if _layout_zero_filled(layout):
                report.zero_filled_fields[topic_name] = _layout_zero_filled(layout)

            for multi_id, data_set in sorted(datasets[topic_name].items()):
                indices = self._window_indices(data_set, start, end)
                if len(indices) == 0:
                    # Keep the subscription so a receiver can decode an
                    # appended stream, but there is nothing to write here.
                    report.empty_topics['{}[{}]'.format(topic_name, multi_id)] = \
                        'no samples in time window'
                    selected_datasets.append((data_set, layout, indices))
                    continue
                selected_datasets.append((data_set, layout, indices))
                report.topics.setdefault(topic_name, []).append(multi_id)

        formats = layout_builder.formats()
        subscriptions = []
        data_items = []
        sample_timestamps = []
        sequence = 0
        for data_set, layout, indices in selected_datasets:
            subscriptions.append({
                'name': data_set.name,
                'multi_id': data_set.multi_id,
                'msg_id': data_set.msg_id,
            })
            for sample_index in indices:
                timestamp = int(data_set.data['timestamp'][sample_index])
                sample_timestamps.append(timestamp)
                data = bytearray(struct.pack('<H', data_set.msg_id))
                for leaf_name, type_str in layout.leaves:
                    encoding = self._UNPACK_TYPES[type_str][0]
                    if leaf_name is None:
                        # Unselected element of an array whose other elements
                        # are kept: the reduced format has to stay rectangular,
                        # so this slot is zero-filled.
                        value = 0 if encoding != 'c' else b'\x00'
                    else:
                        value = data_set.data[leaf_name][sample_index]
                        if encoding == 'c':
                            value = bytes([int(value) & 0xff])
                        else:
                            value = value.item() if hasattr(value, 'item') else value
                    data.extend(struct.pack('<' + encoding, value))
                header = struct.pack('<HB', len(data), self.MSG_TYPE_DATA)
                data_items.append((timestamp, sequence, header + bytes(data)))
                sequence += 1

        data_items.sort(key=lambda item: (item[0], item[1]))
        parameter_state, parameter_changes = self._build_parameter_state(options, start, end)
        for timestamp, name, value in parameter_changes:
            data = self._make_parameter_data(name, value)
            header = struct.pack('<HB', len(data), self.MSG_TYPE_PARAMETER)
            data_items.append((timestamp, sequence, header + data))
            sequence += 1
        if options['include_dropouts']:
            for dropout in self._dropouts:
                if start is not None and dropout.timestamp < start:
                    continue
                if end is not None and dropout.timestamp >= end:
                    continue
                data = struct.pack('<H', dropout.duration)
                header = struct.pack('<HB', len(data), self.MSG_TYPE_DROPOUT)
                data_items.append((dropout.timestamp, sequence, header + data))
                sequence += 1
        if options['include_logged_messages']:
            for message in self._logged_messages:
                self._append_export_logged_message(data_items, message, False, start, end,
                                                   sequence)
                sequence += 1
            for message_list in self._logged_messages_tagged.values():
                for message in message_list:
                    self._append_export_logged_message(data_items, message, True, start,
                                                       end, sequence)
                    sequence += 1
        data_items.sort(key=lambda item: (item[0], item[1]))

        if sample_timestamps:
            start_timestamp = min(sample_timestamps)
        elif start is not None:
            start_timestamp = start
        else:
            start_timestamp = self._start_timestamp
        end_timestamp = max(sample_timestamps) if sample_timestamps else start_timestamp
        return {
            'formats': formats,
            'subscriptions': sorted(subscriptions,
                                    key=lambda item: (item['msg_id'], item['multi_id'])),
            'data_items': data_items,
            'parameter_state': parameter_state,
            'parameter_changes': parameter_changes,
            'start': start,
            'end': end,
            # Keep the original logging start: it is a reference for duration
            # and absolute time, and is not implied by the first sample.
            'header_timestamp': self._start_timestamp,
            'start_timestamp': start_timestamp,
            'end_timestamp': end_timestamp,
        }

    def _append_export_logged_message(self, data_items, message, tagged, start, end,
                                      sequence):
        """ Append a log string message if it lies inside the export window. """
        if start is not None and message.timestamp < start:
            return
        if end is not None and message.timestamp >= end:
            return
        data = bytearray()
        if tagged:
            data.extend(struct.pack('<BHQ', message.log_level, message.tag,
                                    message.timestamp))
            msg_type = self.MSG_TYPE_LOGGING_TAGGED
        else:
            data.extend(struct.pack('<BQ', message.log_level, message.timestamp))
            msg_type = self.MSG_TYPE_LOGGING
        data.extend(bytes(message.message, 'utf-8'))
        header = struct.pack('<HB', len(data), msg_type)
        data_items.append((message.timestamp, sequence, header + data))

    @staticmethod
    def _window_indices(data_set, start, end):
        """ Return sample indices within [start, end). """
        timestamps = data_set.data['timestamp']
        if len(timestamps) == 0:
            return []
        mask = np.ones(len(timestamps), dtype=bool)
        if start is not None:
            mask &= timestamps >= start
        if end is not None:
            mask &= timestamps < end
        return np.nonzero(mask)[0]

    def _build_parameter_state(self, options, start, end):
        """ Return effective parameters at the cut point and in-window changes. """
        state = {}
        if options['include_parameters']:
            for name, value in self._initial_parameters.items():
                if self._parameter_allowed(name, options):
                    state[name] = value
            if start is not None:
                # Definition parameters describe the state at the first written
                # sample, so changes up to and including the cut point are
                # folded into it and are not repeated in the data section.
                for timestamp, name, value in self._changed_parameters:
                    if timestamp <= start and self._parameter_allowed(name, options):
                        state[name] = value

        changes = []
        if options['include_parameters']:
            for timestamp, name, value in self._changed_parameters:
                if not self._parameter_allowed(name, options):
                    continue
                if start is not None and timestamp <= start:
                    continue
                if end is not None and timestamp >= end:
                    continue
                changes.append((timestamp, name, value))

        return dict(sorted(state.items())), changes

    def _parameter_allowed(self, name, options):
        if self._is_sensitive_name(name):
            return False
        whitelist = options['parameter_whitelist']
        return whitelist is None or name in whitelist

    def _write_file_header(self, file, start_timestamp=None):
        header_data = bytearray()
        header_data.extend(self.HEADER_BYTES)
        header_data.extend(struct.pack('B', self._file_version))
        if start_timestamp is None:
            start_timestamp = self._start_timestamp
        header_data.extend(struct.pack('<Q', int(start_timestamp)))
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
            data_set_length = len(data_set.data[data_set.field_data[0].field_name])
            for i_sample in range(data_set_length):
                timestamp = data_set.data['timestamp'][i_sample]
                msg_id = struct.pack('<H', data_set.msg_id)
                data = bytearray()
                data.extend(msg_id)
                for field in data_set.field_data:
                    field_name = field.field_name
                    field_type = field.type_str
                    field_encoding = self._UNPACK_TYPES[field_type][0]
                    field_data = data_set.data[field_name][i_sample]

                    # For char type, convert np.int8 into single bytes() object
                    # so that struct.pack can handle it
                    if field_encoding == 'c':
                        field_data = bytes(chr(field_data), 'utf-8')

                    data.extend(struct.pack('<' + field_encoding, field_data))

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
            self.field_data = message_add_logged_obj.field_data
            self.timestamp_idx = message_add_logged_obj.timestamp_idx

            # get data as numpy.ndarray
            np_array = np.frombuffer(message_add_logged_obj.buffer,
                                     dtype=message_add_logged_obj.dtype)
            # convert into dict of np.array (which is easier to handle)
            self.data = {}
            for name in np_array.dtype.names:
                self.data[name] = np_array[name]

        def __eq__(self, other):
            if not isinstance(other, ULog.Data):
                return NotImplemented

            # Compare data arrays
            arrays_equal = self.data.keys() == other.data.keys()
            if arrays_equal:
                for l_array, r_array in zip(self.data.values(), other.data.values()):
                    arrays_equal = arrays_equal and np.array_equal(l_array, r_array, equal_nan=True)

            return (self.multi_id == other.multi_id and
                    self.msg_id == other.msg_id and
                    self.name == other.name and
                    self.field_data == other.field_data and
                    self.timestamp_idx == other.timestamp_idx and
                    arrays_equal)

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

            return self.name == other.name and self.fields == other.fields

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

    class _FieldData(object):
        """ Type and name of a single ULog data field """
        def __init__(self, field_name, type_str):
            self.field_name = field_name
            self.type_str = type_str

        def __eq__(self, other):
            if not isinstance(other, ULog._FieldData):
                return NotImplemented

            return self.field_name == other.field_name and self.type_str == other.type_str

    class _MessageAddLogged(object):
        """ ULog add logging data message representation """
        def __init__(self, data, header, message_formats):
            self.multi_id, = struct.unpack('<B', data[0:1])
            self.msg_id, = struct.unpack('<H', data[1:3])
            self.message_name = ULog.parse_string(data[3:])
            self.field_data = [] # list of _FieldData
            self.timestamp_idx = -1
            self.max_data_size = 0 # Max size of each data point (including padding fields at end)

            self._parse_format(message_formats)

            self.timestamp_offset = 0
            for field in self.field_data:
                if field.field_name == 'timestamp':
                    break
                self.timestamp_offset += ULog._UNPACK_TYPES[field.type_str][1]

            self.buffer = bytearray() # accumulate all message data here

            # construct types for numpy
            dtype_list = []
            for field in self.field_data:
                numpy_type = ULog._UNPACK_TYPES[field.type_str][2]
                self.max_data_size += ULog._UNPACK_TYPES[field.type_str][1]
                dtype_list.append((field.field_name, numpy_type))
            self.dtype = np.dtype(dtype_list).newbyteorder('<')


        def _parse_format(self, message_formats):
            self._parse_nested_type('', self.message_name, message_formats)

            # remove padding fields at the end
            while (len(self.field_data) > 0 and
                   self.field_data[-1].field_name.startswith('_padding')):
                self.field_data.pop()

        def _parse_nested_type(self, prefix_str, type_name, message_formats):
            # we flatten nested types
            message_format = message_formats[type_name]
            for (type_name_fmt, array_size, field_name) in message_format.fields:
                if type_name_fmt in ULog._UNPACK_TYPES:
                    if array_size > 0:
                        for i in range(array_size):
                            self.field_data.append(ULog._FieldData(
                                prefix_str+field_name+'['+str(i)+']', type_name_fmt))
                    else:
                        self.field_data.append(ULog._FieldData(
                            prefix_str+field_name, type_name_fmt))
                    if prefix_str+field_name == 'timestamp':
                        self.timestamp_idx = len(self.field_data) - 1
                else: # nested type
                    if array_size > 0:
                        for i in range(array_size):
                            self._parse_nested_type(prefix_str+field_name+'['+str(i)+'].',
                                                    type_name_fmt, message_formats)
                    else:
                        self._parse_nested_type(prefix_str+field_name+'.',
                                                type_name_fmt, message_formats)

    class _MessageData(object):
        def __init__(self):
            self.timestamp = 0

        def initialize(self, data, header, subscriptions, ulog_object) -> bool:
            has_corruption = False
            msg_id, = ULog._unpack_ushort(data[:2])
            if msg_id in subscriptions:
                subscription = subscriptions[msg_id]
                min_data_size = subscription.dtype.itemsize
                data_size = len(data) - 2
                if data_size < min_data_size or data_size > subscription.max_data_size:
                    # Corrupt data: skip
                    self.timestamp = 0
                    has_corruption = True
                else:
                    if data_size > min_data_size:
                        # Strip extra data (_padding bytes)
                        data = data[:2+min_data_size]
                    # accumulate data to a buffer, will be parsed later
                    subscription.buffer += data[2:]
                    t_off = subscription.timestamp_offset
                    # TODO: the timestamp can have another size than uint64
                    self.timestamp, = ULog._unpack_uint64(data[t_off+2:t_off+10])
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
