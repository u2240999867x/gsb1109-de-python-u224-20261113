#! /usr/bin/env python
"""
Recover the calibration parameters that were actually in effect while a
data set (bench measurement table) was sampled.

A ULog file records the initial parameters in the definitions section and
parameter changes in the data section. A change is stamped with the
timestamp of the most recently logged data message, so a change and a
sample can carry the same timestamp while the two streams (samples and
parameter updates) share no common order. This module resolves what can
be confirmed and keeps the feasible values where the order cannot be
determined:

- changes within the parameter stream keep their logged order, even when
  their timestamps tie (a known order is never treated as unknown),
- a sample whose timestamp ties with a parameter change reports the set
  of feasible values for that parameter (a conflict),
- each conflict knows the next observation (a later sample of the same
  data set or a later parameter change) that ends the ambiguous interval.
"""

import argparse
import sys
from bisect import bisect_left, bisect_right

from .core import ULog

#pylint: disable=too-few-public-methods


class Observation(object):
    """
    A single observation that can end an ambiguous interval: either a
    sample of the annotated data set or a parameter change.
    """

    def __init__(self, kind, timestamp, index, detail):
        """
        :param kind: 'sample' or 'parameter'
        :param timestamp: timestamp of the observation
        :param index: sample index within the data set, or change index
               within the parameter stream
        :param detail: data set name for samples, 'NAME=VALUE' for changes
        """
        self.kind = kind
        self.timestamp = timestamp
        self.index = index
        self.detail = detail

    def __repr__(self):
        return 'Observation({!r}, {!r}, {!r}, {!r})'.format(
            self.kind, self.timestamp, self.index, self.detail)

    def __eq__(self, other):
        if not isinstance(other, Observation):
            return NotImplemented
        return (self.kind == other.kind and
                self.timestamp == other.timestamp and
                self.index == other.index and
                self.detail == other.detail)

    def describe(self):
        """ human readable one-line description """
        if self.kind == 'sample':
            return "sample #{:d} of '{:s}' at t={:d}".format(
                self.index, self.detail, self.timestamp)
        return "parameter change #{:d} ({:s}) at t={:d}".format(
            self.index, self.detail, self.timestamp)


class Conflict(object):
    """
    A parameter whose value at a sample cannot be pinned to a single
    value, because the sample ties in time with one or more changes and
    the two streams have no common order.
    """

    def __init__(self, parameter, feasible_values, ends_at):
        """
        :param parameter: parameter name
        :param feasible_values: tuple of the feasible values, ordered by
               increasing number of applied same-timestamp changes (the
               first entry is the value confirmed before the tie)
        :param ends_at: the Observation that ends the ambiguous interval,
               or None if no later observation exists
        """
        self.parameter = parameter
        self.feasible_values = tuple(feasible_values)
        self.ends_at = ends_at

    def __repr__(self):
        return 'Conflict({!r}, {!r}, {!r})'.format(
            self.parameter, self.feasible_values, self.ends_at)

    def __eq__(self, other):
        if not isinstance(other, Conflict):
            return NotImplemented
        return (self.parameter == other.parameter and
                self.feasible_values == other.feasible_values and
                self.ends_at == other.ends_at)

    def describe(self):
        """ human readable one-line description """
        values = ', '.join(str(value) for value in self.feasible_values)
        text = "parameter '{:s}' has {:d} feasible values ({:s})".format(
            self.parameter, len(self.feasible_values), values)
        if self.ends_at is None:
            text += '; no later observation ends the ambiguous interval'
        else:
            text += '; the interval ends at ' + self.ends_at.describe()
        return text


class SampleCalibration(object):
    """
    The calibration parameters in effect for a single sample: confirmed
    values plus the conflicts for parameters with several feasible values.
    """

    def __init__(self, index, timestamp, values, conflicts):
        """
        :param index: sample index within the data set
        :param timestamp: sample timestamp
        :param values: dict of confirmed parameter values (name -> value)
        :param conflicts: dict of Conflict objects (name -> Conflict)
        """
        self.index = index
        self.timestamp = timestamp
        self.values = values
        self.conflicts = conflicts

    def effective(self, parameter):
        """
        The effective value of a parameter at this sample: the confirmed
        value, or the tuple of feasible values if the sample conflicts.
        """
        if parameter in self.conflicts:
            return self.conflicts[parameter].feasible_values
        return self.values[parameter]


class CalibrationTimeline(object):
    """
    Ordered history of all calibration parameters of a log.

    The parameter stream is a single physical stream: changes keep their
    logged order, so a stable sort by timestamp orders changes with equal
    timestamps by their position in the log.
    """

    def __init__(self, initial_parameters=None, changed_parameters=None):
        """
        :param initial_parameters: dict of initial values (name -> value)
        :param changed_parameters: list of (timestamp, name, value) tuples
               in logged order
        """
        self._initial = dict(initial_parameters or {})
        # stable sort: ties keep the logged (stream) order
        self._changes = sorted(changed_parameters or [],
                               key=lambda change: change[0])
        self._change_times = [change[0] for change in self._changes]
        # per-parameter ordered history: name -> (times, values)
        self._history = {}
        for timestamp, name, value in self._changes:
            times, values = self._history.setdefault(name, ([], []))
            times.append(timestamp)
            values.append(value)

    @classmethod
    def from_ulog(cls, ulog):
        """ build a timeline from a parsed ULog object """
        return cls(ulog.initial_parameters, ulog.changed_parameters)

    @property
    def parameter_names(self):
        """ sorted names of all known parameters """
        return sorted(set(self._initial) | set(self._history))

    @property
    def changes(self):
        """ all changes as (timestamp, name, value) tuples, ordered by
        timestamp with ties in logged (parameter stream) order """
        return list(self._changes)

    def confirmed_value(self, name, timestamp):
        """
        The value of a parameter confirmed before the given timestamp:
        the last change with a strictly smaller timestamp, or the initial
        value. Raises KeyError if the parameter is unknown.
        """
        if name not in self._initial and name not in self._history:
            raise KeyError(name)
        times, values = self._history.get(name, ([], []))
        index = bisect_left(times, timestamp)
        if index > 0:
            return values[index - 1]
        if name in self._initial:
            return self._initial[name]
        raise KeyError('no value of {!r} confirmed before t={}'.format(
            name, timestamp))

    def feasible_values(self, name, timestamp):
        """
        The distinct values a parameter can have at the given timestamp,
        in feasible order: the value confirmed before the timestamp,
        followed by the values of the changes tying with the timestamp in
        parameter stream order (any prefix of them may have been applied).
        A single entry means the value is confirmed.
        """
        if name not in self._initial and name not in self._history:
            raise KeyError(name)
        times, values = self._history.get(name, ([], []))
        first_tied = bisect_left(times, timestamp)
        past_tied = bisect_right(times, timestamp)
        feasible = []
        if first_tied > 0:
            feasible.append(values[first_tied - 1])
        elif name in self._initial:
            feasible.append(self._initial[name])
        for value in values[first_tied:past_tied]:
            if value not in feasible:
                feasible.append(value)
        return tuple(feasible)

    def next_change(self, timestamp):
        """
        The first parameter change with a strictly greater timestamp, as
        an Observation, or None if there is none.
        """
        index = bisect_right(self._change_times, timestamp)
        if index >= len(self._changes):
            return None
        change_time, name, value = self._changes[index]
        return Observation('parameter', change_time, index,
                           '{}={}'.format(name, value))

    def annotate(self, dataset, parameters=None):
        """
        Annotate the samples of a ULog.Data data set with the effective
        calibration parameters. Returns a BenchTable.

        :param dataset: a ULog.Data object
        :param parameters: parameter names to consider (default: all)
        """
        return BenchTable(self, dataset, parameters)


class BenchTable(object):
    """
    A bench measurement table: the samples of one data set annotated with
    the calibration parameters in effect at each sample.
    """

    def __init__(self, timeline, dataset, parameters=None):
        """
        :param timeline: a CalibrationTimeline
        :param dataset: a ULog.Data object
        :param parameters: parameter names to consider (default: all)
        """
        self._timeline = timeline
        self._dataset = dataset
        self._parameters = (list(timeline.parameter_names)
                            if parameters is None else list(parameters))
        timestamps = dataset.data['timestamp']
        self._rows = []
        for index, timestamp in enumerate(timestamps):
            timestamp = int(timestamp)
            values = {}
            conflicts = {}
            for name in self._parameters:
                feasible = timeline.feasible_values(name, timestamp)
                if len(feasible) == 1:
                    values[name] = feasible[0]
                elif len(feasible) > 1:
                    conflicts[name] = Conflict(
                        name, feasible, self._interval_end(index))
            self._rows.append(SampleCalibration(index, timestamp,
                                                values, conflicts))

    @property
    def dataset(self):
        """ the annotated ULog.Data object """
        return self._dataset

    @property
    def parameters(self):
        """ the considered parameter names """
        return list(self._parameters)

    @property
    def rows(self):
        """ the per-sample SampleCalibration rows, in data set order """
        return list(self._rows)

    def _interval_end(self, index):
        """
        The next observation that ends the ambiguous interval of a
        conflict at the given sample index: the earlier of the next
        sample of this data set and the next parameter change, both with
        a strictly greater timestamp. None if there is no such
        observation.
        """
        timestamps = self._dataset.data['timestamp']
        timestamp = int(timestamps[index])
        candidates = []
        for later in range(index + 1, len(timestamps)):
            if int(timestamps[later]) > timestamp:
                candidates.append(Observation('sample', int(timestamps[later]),
                                              later, self._dataset.name))
                break
        change = self._timeline.next_change(timestamp)
        if change is not None:
            candidates.append(change)
        if not candidates:
            return None
        # on a timestamp tie prefer the sample of the annotated data set
        return min(candidates,
                   key=lambda obs: (obs.timestamp, 0 if obs.kind == 'sample' else 1))

    def conflicts(self):
        """ iterate over (SampleCalibration, Conflict) pairs of all rows """
        for row in self._rows:
            for conflict in row.conflicts.values():
                yield row, conflict

    def conflict_at(self, index):
        """ the list of Conflict objects of the sample with the given index """
        return [self._rows[index].conflicts[name] for name in self._parameters
                if name in self._rows[index].conflicts]

    def explain(self, index):
        """
        Answer a query about a sample: which parameters have several
        feasible values, and which observation ends each ambiguous
        interval. Returns a human readable string.
        """
        row = self._rows[index]
        prefix = "sample #{:d} (t={:d})".format(row.index, row.timestamp)
        if not row.conflicts:
            return prefix + ': all parameters confirmed'
        lines = [prefix + ':']
        for name in self._parameters:
            if name in row.conflicts:
                lines.append('  ' + row.conflicts[name].describe())
        return '\n'.join(lines)


def main():
    """Command line interface"""
    parser = argparse.ArgumentParser(
        description='Annotate the samples of a ULog topic with the calibration '
                    'parameters in effect at each sample')
    parser.add_argument('filename', metavar='file.ulg', help='ULog input file')
    parser.add_argument('topic', metavar='topic',
                        help='Name of the topic (data set) to annotate')
    parser.add_argument('-m', '--multi-id', dest='multi_id', action='store',
                        type=int, help='Multi id of the topic (default 0)',
                        default=0)
    parser.add_argument('-p', '--parameters', dest='parameters', action='store',
                        help="Only consider the given parameters, as a "
                             "comma-separated list (default: all)", default=None)
    parser.add_argument('-d', '--delimiter', dest='delimiter', action='store',
                        help="Use delimiter in CSV (default is ',')", default=',')
    parser.add_argument('-o', '--output', dest='output', action='store',
                        help='Output filename (default=stdout)', default=None)
    parser.add_argument('-c', '--conflicts', dest='conflicts',
                        action='store_true', default=False,
                        help='Print a report of the conflicting samples')
    parser.add_argument('-i', '--ignore', dest='ignore', action='store_true',
                        help='Ignore string parsing exceptions', default=False)

    args = parser.parse_args()

    ulog = ULog(args.filename, None, args.ignore)
    dataset = ulog.get_dataset(args.topic, args.multi_id)
    parameters = (args.parameters.split(',')
                  if args.parameters is not None else None)
    table = CalibrationTimeline.from_ulog(ulog).annotate(dataset, parameters)

    output_file = sys.stdout if args.output is None \
        else open(args.output, 'w', encoding='utf-8') #pylint: disable=consider-using-with

    field_names = [field.field_name for field in dataset.field_data]
    output_file.write(args.delimiter.join(
        field_names + ['param:' + name for name in table.parameters]) + '\n')
    for row in table.rows:
        columns = [str(dataset.data[field][row.index]) for field in field_names]
        for name in table.parameters:
            if name in row.conflicts:
                columns.append('|'.join(
                    str(value) for value in row.conflicts[name].feasible_values))
            elif name in row.values:
                columns.append(str(row.values[name]))
            else:
                columns.append('')
        output_file.write(args.delimiter.join(columns) + '\n')

    if args.conflicts:
        for row in table.rows:
            if row.conflicts:
                output_file.write(table.explain(row.index) + '\n')

    if output_file is not sys.stdout:
        output_file.close()


if __name__ == '__main__':
    main()
