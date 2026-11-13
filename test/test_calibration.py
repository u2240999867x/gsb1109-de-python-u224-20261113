'''
Tests for the calibration timeline and the annotated bench table
'''

import struct
import sys
import tempfile
import unittest
from io import BytesIO, StringIO

import pyulog
from pyulog import calibration
from pyulog.calibration import CalibrationTimeline, Observation


def _param_message(name, value):
    ''' Build the payload of a PARAMETER message. '''
    if isinstance(value, int):
        key = 'int32_t ' + name
        packed_value = struct.pack('<i', value)
    else:
        key = 'float ' + name
        packed_value = struct.pack('<f', value)
    key_bytes = key.encode('utf-8')
    return bytes([len(key_bytes)]) + key_bytes + packed_value


def _build_log(samples, changes, extra=None):
    '''
    Build a minimal ULog in memory with initial parameters CAL_A=1 and
    CAL_B=2.5, a 'bench' topic sampled at the given timestamps, and
    parameter changes as (after_sample_index, name, value) tuples: each
    change is written right after the given sample, so it gets stamped
    with that sample's timestamp. `extra` maps a sample index to
    additional (msg_type, payload) messages written after that sample.
    '''
    content = pyulog.ULog.HEADER_BYTES + b'\x01' + struct.pack('<Q', 0)

    def add(msg_type, payload):
        nonlocal content
        content += struct.pack('<HB', len(payload), ord(msg_type)) + payload

    add('F', b'bench:uint64_t timestamp;float reading;')
    add('F', b'aux:uint64_t timestamp;')
    add('P', _param_message('CAL_A', 1))
    add('P', _param_message('CAL_B', 2.5))
    add('A', struct.pack('<BH', 0, 5) + b'bench')
    add('A', struct.pack('<BH', 0, 6) + b'aux')
    changes_by_index = {}
    for sample_index, name, value in changes:
        changes_by_index.setdefault(sample_index, []).append((name, value))
    for index, timestamp in enumerate(samples):
        add('D', struct.pack('<HQf', 5, timestamp, 0.5 + index / 10.0))
        for msg_type, payload in (extra or {}).get(index, []):
            add(msg_type, payload)
        for name, value in changes_by_index.get(index, []):
            add('P', _param_message(name, value))
    return BytesIO(content)


def _make_table(samples, changes, extra=None):
    ''' Parse a built log and annotate the 'bench' topic. '''
    ulog = pyulog.ULog(_build_log(samples, changes, extra))
    dataset = ulog.get_dataset('bench')
    return ulog, CalibrationTimeline.from_ulog(ulog).annotate(dataset)


class TestCalibrationTimeline(unittest.TestCase):
    '''
    Tests for the CalibrationTimeline value recovery.
    '''

    def test_confirmed_and_feasible_values(self):
        ''' Confirmed values come from strictly earlier changes; tied
        changes extend the feasible set in parameter stream order. '''
        timeline = CalibrationTimeline(
            {'CAL_A': 1, 'CAL_B': 2.5},
            [(100, 'CAL_A', 2), (100, 'CAL_A', 7), (200, 'CAL_B', 3.5)])
        # strictly earlier changes are confirmed
        self.assertEqual(timeline.confirmed_value('CAL_A', 100), 1)
        self.assertEqual(timeline.confirmed_value('CAL_A', 101), 7)
        self.assertEqual(timeline.confirmed_value('CAL_B', 200), 2.5)
        # a tie keeps every feasible prefix of the same-timestamp changes
        self.assertEqual(timeline.feasible_values('CAL_A', 100), (1, 2, 7))
        self.assertEqual(timeline.feasible_values('CAL_A', 99), (1,))
        # the known order within the parameter stream is not unknown:
        # after the tie the last change is confirmed, not ambiguous
        self.assertEqual(timeline.feasible_values('CAL_A', 150), (7,))
        self.assertEqual(timeline.feasible_values('CAL_B', 200), (2.5, 3.5))
        self.assertEqual(timeline.feasible_values('CAL_B', 201), (3.5,))

    def test_change_to_same_value_is_no_conflict(self):
        ''' A change that keeps the current value collapses the feasible
        set to a single value. '''
        timeline = CalibrationTimeline({'CAL_A': 1}, [(100, 'CAL_A', 1)])
        self.assertEqual(timeline.feasible_values('CAL_A', 100), (1,))

    def test_unsorted_changes_keep_stream_order_on_ties(self):
        ''' Changes are ordered by timestamp; ties keep the logged order
        of the parameter stream. '''
        timeline = CalibrationTimeline(
            {'CAL_A': 1},
            [(200, 'CAL_A', 3), (100, 'CAL_A', 2), (100, 'CAL_A', 4)])
        self.assertEqual(timeline.feasible_values('CAL_A', 100), (1, 2, 4))
        self.assertEqual(timeline.feasible_values('CAL_A', 150), (4,))
        self.assertEqual(timeline.feasible_values('CAL_A', 250), (3,))

    def test_unknown_parameter(self):
        ''' Unknown parameters raise a KeyError. '''
        timeline = CalibrationTimeline({'CAL_A': 1}, [])
        with self.assertRaises(KeyError):
            timeline.feasible_values('NOPE', 0)
        with self.assertRaises(KeyError):
            timeline.confirmed_value('NOPE', 0)

    def test_next_change(self):
        ''' The next change strictly after a timestamp is reported as an
        observation of the parameter stream. '''
        timeline = CalibrationTimeline({}, [(100, 'CAL_A', 2),
                                            (300, 'CAL_B', 4.0)])
        self.assertEqual(timeline.next_change(50),
                         Observation('parameter', 100, 0, 'CAL_A=2'))
        self.assertEqual(timeline.next_change(100),
                         Observation('parameter', 300, 1, 'CAL_B=4.0'))
        self.assertIsNone(timeline.next_change(300))


class TestBenchTable(unittest.TestCase):
    '''
    Tests for the per-sample annotation of a data set.
    '''

    def test_changed_parameters_parsed(self):
        ''' The parser stamps changes with the last sample timestamp. '''
        ulog, _ = _make_table([100, 200], [(0, 'CAL_A', 2), (1, 'CAL_B', 3.5)])
        self.assertEqual(ulog.changed_parameters,
                         [(100, 'CAL_A', 2), (200, 'CAL_B', 3.5)])

    def test_confirmed_values(self):
        ''' Samples away from changes get confirmed values. '''
        _, table = _make_table([100, 200, 300], [(1, 'CAL_A', 2)])
        rows = table.rows
        self.assertEqual(rows[0].values, {'CAL_A': 1, 'CAL_B': 2.5})
        self.assertEqual(rows[1].conflicts['CAL_A'].feasible_values, (1, 2))
        self.assertEqual(rows[2].values, {'CAL_A': 2, 'CAL_B': 2.5})
        self.assertEqual(rows[2].conflicts, {})

    def test_tied_sample_and_change_conflict(self):
        ''' A sample with the same timestamp as a change keeps both
        feasible values; the next sample ends the interval. '''
        _, table = _make_table([100, 100, 200, 300],
                               [(0, 'CAL_A', 2),
                                (2, 'CAL_A', 3), (2, 'CAL_B', 3.5)])
        rows = table.rows
        # both samples at t=100 conflict with the change stamped 100
        for row in rows[:2]:
            conflict = row.conflicts['CAL_A']
            self.assertEqual(conflict.feasible_values, (1, 2))
            self.assertEqual(conflict.ends_at,
                             Observation('sample', 200, 2, 'bench'))
            self.assertEqual(row.values['CAL_B'], 2.5)
        # the sample at t=200 confirms the earlier change and conflicts
        # with the two changes stamped 200
        self.assertEqual(rows[2].conflicts['CAL_A'].feasible_values, (2, 3))
        self.assertEqual(rows[2].conflicts['CAL_B'].feasible_values, (2.5, 3.5))
        self.assertEqual(rows[2].conflicts['CAL_A'].ends_at,
                         Observation('sample', 300, 3, 'bench'))
        # the last sample is fully confirmed
        self.assertEqual(rows[3].values, {'CAL_A': 3, 'CAL_B': 3.5})
        self.assertEqual(rows[3].conflicts, {})

    def test_same_timestamp_changes_keep_stream_order(self):
        ''' Two changes to the same parameter at one timestamp give a
        prefix-ordered feasible set, and later samples confirm the last
        change only. '''
        _, table = _make_table([100, 200], [(0, 'CAL_A', 2), (0, 'CAL_A', 7)])
        rows = table.rows
        self.assertEqual(rows[0].conflicts['CAL_A'].feasible_values, (1, 2, 7))
        self.assertEqual(rows[1].values['CAL_A'], 7)
        self.assertEqual(rows[1].conflicts, {})

    def test_interval_ended_by_parameter_change(self):
        ''' A parameter change can be the next observation that ends the
        ambiguous interval. '''
        # the bench topic jumps from t=100 to t=500, but the aux topic
        # observes t=300 in between and a change gets stamped with it;
        # that change ends the interval of the t=100 conflict
        interleaved = [('P', _param_message('CAL_A', 2)),
                       ('D', struct.pack('<HQ', 6, 300)),
                       ('P', _param_message('CAL_B', 9.5))]
        _, table = _make_table([100, 500], [], extra={0: interleaved})
        conflict = table.rows[0].conflicts['CAL_A']
        self.assertEqual(conflict.feasible_values, (1, 2))
        self.assertEqual(conflict.ends_at,
                         Observation('parameter', 300, 1, 'CAL_B=9.5'))
        # the aux sample itself is not part of the annotated table
        self.assertEqual([row.timestamp for row in table.rows], [100, 500])

    def test_open_interval(self):
        ''' A conflict without any later observation stays open. '''
        _, table = _make_table([100, 200], [(1, 'CAL_A', 2)])
        conflict = table.rows[1].conflicts['CAL_A']
        self.assertEqual(conflict.feasible_values, (1, 2))
        self.assertIsNone(conflict.ends_at)

    def test_query_conflicting_sample(self):
        ''' The query of a conflicting sample names the parameter, the
        feasible values and the observation that ends the interval. '''
        _, table = _make_table([100, 200], [(0, 'CAL_A', 2)])
        self.assertEqual(table.conflict_at(0),
                         [table.rows[0].conflicts['CAL_A']])
        self.assertEqual(table.conflict_at(1), [])
        explanation = table.explain(0)
        self.assertIn('CAL_A', explanation)
        self.assertIn('2 feasible values (1, 2)', explanation)
        self.assertIn("sample #1 of 'bench' at t=200", explanation)
        self.assertEqual(table.explain(1),
                         'sample #1 (t=200): all parameters confirmed')

    def test_effective_value(self):
        ''' effective() returns the confirmed value or the feasible
        tuple for a conflict. '''
        _, table = _make_table([100, 200], [(0, 'CAL_A', 2)])
        self.assertEqual(table.rows[0].effective('CAL_A'), (1, 2))
        self.assertEqual(table.rows[0].effective('CAL_B'), 2.5)
        self.assertEqual(table.rows[1].effective('CAL_A'), 2)

    def test_parameter_subset(self):
        ''' Only the requested parameters are annotated. '''
        ulog = pyulog.ULog(_build_log([100, 200], [(0, 'CAL_A', 2)]))
        dataset = ulog.get_dataset('bench')
        table = CalibrationTimeline.from_ulog(ulog).annotate(
            dataset, parameters=['CAL_A'])
        self.assertEqual(table.parameters, ['CAL_A'])
        self.assertEqual(table.rows[1].values, {'CAL_A': 2})


class TestCommandLineInterface(unittest.TestCase):
    '''
    Tests for the ulog_calibration command line tool.
    '''

    def test_csv_output_and_conflict_report(self):
        ''' The CSV carries the effective parameters and the conflict
        report explains the conflicting samples. '''
        ulog, _ = _make_table([100, 200], [(0, 'CAL_A', 2)])
        with tempfile.NamedTemporaryFile(suffix='.ulg') as tmp_file:
            ulog.write_ulog(tmp_file.name)
            saved_argv = sys.argv
            saved_stdout = sys.stdout
            out = StringIO()
            try:
                sys.argv = ['ulog_calibration', tmp_file.name, 'bench',
                            '--conflicts']
                sys.stdout = out
                calibration.main()
            finally:
                sys.argv = saved_argv
                sys.stdout = saved_stdout
        lines = out.getvalue().strip().split('\n')
        self.assertEqual(
            lines[0], 'timestamp,reading,param:CAL_A,param:CAL_B')
        # the conflicting sample keeps both feasible values
        self.assertTrue(lines[1].endswith(',1|2,2.5'))
        self.assertTrue(lines[2].endswith(',2,2.5'))
        # the conflict report names the parameter, the feasible values
        # and the observation that ends the interval
        report = '\n'.join(lines[3:])
        self.assertIn('CAL_A', report)
        self.assertIn('2 feasible values (1, 2)', report)
        self.assertIn("sample #1 of 'bench' at t=200", report)


if __name__ == '__main__':
    unittest.main()
