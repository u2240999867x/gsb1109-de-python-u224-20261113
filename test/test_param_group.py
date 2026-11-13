'''
Tests for parameter-based batch grouping (pyulog.param_group).

The ULog files used here are built in memory, so every scenario can be
replayed without external data: initial parameters, stored defaults, data
samples and data-section parameter changes are encoded explicitly.
'''

import json
import os
import inspect
import struct
import tempfile
import unittest
from io import BytesIO

import pyulog
from pyulog import param_group

TEST_PATH = os.path.dirname(os.path.abspath(
    inspect.getfile(inspect.currentframe())))


def _info_payload(type_name, name, value):
    '''Payload of a ULog INFO/PARAMETER message.'''
    key = '{} {}'.format(type_name, name).encode('utf-8')
    if type_name == 'float':
        packed = struct.pack('<f', value)
    else:
        packed = struct.pack('<i', int(value))
    return bytes([len(key)]) + key + packed


def _param_message(name, value):
    if isinstance(value, float):
        payload = _info_payload('float', name, value)
    else:
        payload = _info_payload('int32_t', name, value)
    return ord('P'), payload


def _default_param_message(name, value, default_type=0):
    # ULog stores the default type as a bitfield (bit 0: system, bit 1:
    # current_setup), not as the zero-based index used by get_default_parameters
    bitfield = 1 << default_type
    if isinstance(value, float):
        payload = bytes([bitfield]) + _info_payload('float', name, value)
    else:
        payload = bytes([bitfield]) + _info_payload('int32_t', name, value)
    return ord('Q'), payload


def _data_message(timestamp, value=0.0, msg_id=1):
    return ord('D'), struct.pack('<HQf', msg_id, timestamp, value)


def build_log(start_timestamp, messages):
    '''
    Build a minimal ULog in memory.

    messages is a list of (type_char, payload) tuples. A 'F' format and an
    'A' subscription for one data topic are always included.
    '''
    content = pyulog.ULog.HEADER_BYTES + b'\x01' + struct.pack('<Q', start_timestamp)
    format_payload = b'sensor_sample:uint64_t timestamp;float value;'
    content += struct.pack('<HB', len(format_payload), ord('F')) + format_payload
    for msg_type, payload in messages:
        content += struct.pack('<HB', len(payload), msg_type) + payload
    return BytesIO(content)


def subscription_message(msg_id=1):
    payload = struct.pack('<BH', 0, msg_id) + b'sensor_sample'
    return ord('A'), payload


class TestParamGroup(unittest.TestCase):
    '''Grouping verdicts and evidence handling.'''

    def _profile(self, messages, start_timestamp=0, path='mem.ulg'):
        ulog = pyulog.ULog(build_log(start_timestamp, messages))
        return param_group.build_profile(ulog, path)

    def test_explicit_equal(self):
        '''Identical initial parameters group as explicit_equal.'''
        messages = [
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
        ]
        left = self._profile(messages, path='a.ulg')
        right = self._profile(messages, path='b.ulg')
        verdicts = param_group.compare_profiles(left, right)
        foo = next(v for v in verdicts if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_EXPLICIT_EQUAL)
        decision = param_group.pair_decision([left, right], 0, 1)
        self.assertTrue(decision.compatible)

    def test_default_filled_equal(self):
        '''A missing explicit value equal to the stored default groups.'''
        left = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
        ], path='a.ulg')
        right = self._profile([
            _default_param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
        ], path='b.ulg')
        verdicts = param_group.compare_profiles(left, right)
        foo = next(v for v in verdicts if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_DEFAULT_EQUAL)
        self.assertEqual(right.params['FOO'].effective_source,
                         param_group.SOURCE_DEFAULT)
        self.assertIn('b.ulg', foo.note)

    def test_default_filled_both_sides(self):
        '''Two default-only batches with equal defaults still match.'''
        messages = [
            _default_param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
        ]
        left = self._profile(messages, path='a.ulg')
        right = self._profile(messages, path='b.ulg')
        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_DEFAULT_EQUAL)

    def test_missing_evidence_is_undecidable(self):
        '''No explicit value and no default cannot be judged.'''
        left = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
        ], path='a.ulg')
        right = self._profile([
            subscription_message(),
            _data_message(1000),
        ], path='b.ulg')
        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_UNDECIDABLE)
        self.assertEqual(foo.reason, param_group.REASON_MISSING_EVIDENCE)
        self.assertFalse(
            param_group.pair_decision([left, right], 0, 1).compatible)

    def test_explicit_mismatch_is_undecidable(self):
        left = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
        ], path='a.ulg')
        right = self._profile([
            _param_message('FOO', 2),
            subscription_message(),
            _data_message(1000),
        ], path='b.ulg')
        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_UNDECIDABLE)
        self.assertEqual(foo.reason, param_group.REASON_EXPLICIT_MISMATCH)

    def test_default_mismatch_is_undecidable(self):
        left = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
        ], path='a.ulg')
        right = self._profile([
            _default_param_message('FOO', 2),
            subscription_message(),
            _data_message(1000),
        ], path='b.ulg')
        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_UNDECIDABLE)
        self.assertEqual(foo.reason, param_group.REASON_DEFAULT_MISMATCH)

    def test_change_after_measurement_does_not_regroup(self):
        '''
        A parameter changed at/after the last sample stays in evidence but
        must not change the effective value used for grouping.
        '''
        # the change is stamped with the preceding data timestamp, which is
        # the interval end, so it is excluded from the effective value
        left = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
            _param_message('FOO', 2),
        ], path='a.ulg')
        right = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
        ], path='b.ulg')

        evidence = left.params['FOO']
        self.assertEqual(evidence.effective_value, 1)
        self.assertEqual(evidence.after_window_changes, [[2000, 2]])
        self.assertEqual(evidence.in_window_changes, [])

        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_EXPLICIT_EQUAL)
        groups, _ = param_group.group_profiles([left, right])
        self.assertEqual(groups, [['a.ulg', 'b.ulg']])

    def test_change_before_interval_sets_start_value(self):
        '''A data-section change before the first sample sets the start.'''
        profile = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _param_message('FOO', 5),
            _data_message(1000),
            _data_message(2000),
        ], start_timestamp=100)
        evidence = profile.params['FOO']
        self.assertEqual(evidence.start_value, 5)
        self.assertEqual(evidence.effective_value, 5)
        self.assertEqual(evidence.after_window_changes, [])

    def test_in_window_change_without_initial_record(self):
        '''A change is explicit evidence even without an initial parameter.'''
        profile = self._profile([
            subscription_message(),
            _data_message(1000),
            _param_message('FOO', 3),
            _data_message(2000),
        ])
        evidence = profile.params['FOO']
        self.assertEqual(evidence.in_window_changes, [[1000, 3]])
        self.assertEqual(evidence.effective_value, 3)
        self.assertEqual(evidence.effective_source,
                         param_group.SOURCE_CHANGED)
        self.assertTrue(evidence.has_explicit_evidence)

    def test_in_window_change_makes_batch_variable(self):
        left = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(1500),
            _param_message('FOO', 2),
            _data_message(2000),
        ], path='a.ulg')
        right = self._profile([
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(2000),
        ], path='b.ulg')
        self.assertTrue(left.params['FOO'].is_variable)
        self.assertEqual(left.params['FOO'].in_window_changes, [[1500, 2]])
        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_UNDECIDABLE)
        self.assertEqual(foo.reason, param_group.REASON_VARIABLE_INTERVAL)

    def test_identical_in_window_series_match(self):
        '''Same variable history on both sides is explicitly equal.'''
        messages = [
            _param_message('FOO', 1),
            subscription_message(),
            _data_message(1000),
            _data_message(1500),
            _param_message('FOO', 2),
            _data_message(2000),
        ]
        left = self._profile(messages, path='a.ulg')
        right = self._profile(messages, path='b.ulg')
        foo = next(v for v in param_group.compare_profiles(left, right)
                   if v.name == 'FOO')
        self.assertEqual(foo.verdict, param_group.VERDICT_EXPLICIT_EQUAL)

    def test_measured_interval_comes_from_data(self):
        profile = self._profile([
            subscription_message(),
            _data_message(3000),
            _data_message(9000),
        ], start_timestamp=100)
        self.assertTrue(profile.coverage_from_data)
        self.assertEqual(profile.interval_start, 3000)
        self.assertEqual(profile.interval_end, 9000)

    def test_verdict_recomputable_from_evidence(self):
        '''
        Independently replay the verdict from the measured interval and the
        raw evidence, without using the computed effective_* fields.
        '''
        profile = self._profile([
            _param_message('FOO', 1),
            _default_param_message('FOO', 7),
            subscription_message(),
            _data_message(1000),
            _data_message(1500),
            _param_message('FOO', 2),
            _data_message(2000),
            _param_message('FOO', 9),
        ])
        summary = profile.evidence_summary()
        param = summary['params']['FOO']
        interval_end = summary['measured_interval_us'][1]

        # replay: collect initial value and classify changes by timestamp
        initial = [value for source, _, value in param['sources']
                   if source == param_group.SOURCE_INITIAL][0]
        replayed_in_window = [[ts, value] for source, ts, value
                              in param['sources']
                              if source == param_group.SOURCE_CHANGED
                              and ts < interval_end and ts >= summary[
                                  'measured_interval_us'][0]]
        replayed_after = [[ts, value] for source, ts, value
                          in param['sources']
                          if source == param_group.SOURCE_CHANGED
                          and ts >= interval_end]
        self.assertEqual(replayed_in_window, param['in_window_changes'])
        self.assertEqual(replayed_after, param['after_window_changes'])
        self.assertEqual(initial, 1)
        self.assertEqual(replayed_after, [[2000, 9]])
        self.assertEqual(replayed_in_window, [[1500, 2]])

        # JSON evidence is fully serialisable and carries the interval
        payload = json.loads(json.dumps(summary))
        self.assertEqual(payload['measured_interval_us'], [1000, 2000])

    def test_grouping_connected_components(self):
        '''Incompatible batches end up in different groups.'''
        common = [subscription_message(), _data_message(1000), _data_message(2000)]
        a = self._profile([_param_message('FOO', 1)] + common, path='a.ulg')
        b = self._profile([_param_message('FOO', 1)] + common, path='b.ulg')
        c = self._profile([_param_message('FOO', 2)] + common, path='c.ulg')
        groups, decisions = param_group.group_profiles([a, b, c])
        self.assertEqual(len(decisions), 3)
        grouped = sorted(sorted(group) for group in groups)
        self.assertEqual(grouped, [['a.ulg', 'b.ulg'], ['c.ulg']])

    def test_cli_json_on_real_samples(self):
        '''The CLI runs end to end and emits complete JSON evidence.'''
        files = [
            os.path.join(TEST_PATH, 'sample.ulg'),
            os.path.join(TEST_PATH,
                         'sample_logging_tagged_and_default_params.ulg'),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, 'report.json')
            old_argv = __import__('sys').argv
            import contextlib
            with open(out_path, 'w', encoding='utf-8') as handle, \
                    contextlib.redirect_stdout(handle):
                __import__('sys').argv = ['ulog_param_group', '--json'] + files
                try:
                    param_group.main()
                finally:
                    __import__('sys').argv = old_argv
            with open(out_path, encoding='utf-8') as handle:
                payload = json.load(handle)
        self.assertEqual(len(payload['batches']), 2)
        for batch in payload['batches']:
            self.assertIn('measured_interval_us', batch)
            self.assertTrue(batch['coverage_from_data_topics'])
        self.assertIn('groups', payload)
        self.assertIn('pair_decisions', payload)

    def test_real_sample_changed_params_classified(self):
        '''Changes inside the measured window make the parameter variable.'''
        ulog = pyulog.ULog(os.path.join(TEST_PATH, 'sample.ulg'))
        profile = param_group.build_profile(ulog, 'sample.ulg')
        autos = profile.params['COM_AUTOS_PAR']
        self.assertTrue(autos.is_variable)
        self.assertEqual(autos.after_window_changes, [])
        # a change carrying the same value as the start does not vary
        vel = profile.params['MPC_Z_VEL_MAX_DN']
        self.assertFalse(vel.is_variable)

    def test_default_params_sample_builds(self):
        '''Stored defaults are captured but an explicit initial value wins.'''
        ulog = pyulog.ULog(os.path.join(
            TEST_PATH, 'sample_logging_tagged_and_default_params.ulg'))
        profile = param_group.build_profile(ulog, 'defaults.ulg')
        # BAT1_N_CELLS is stored with default 0 but configured to 4
        evidence = profile.params['BAT1_N_CELLS']
        self.assertEqual(evidence.default_value, 0)
        self.assertEqual(evidence.effective_value, 4)
        self.assertEqual(evidence.effective_source,
                         param_group.SOURCE_INITIAL)
        decision = param_group.pair_decision([profile, profile], 0, 1)
        self.assertTrue(decision.compatible)


# vim: set et fenc=utf-8 ft=python ff=unix sts=4 sw=4 ts=4
