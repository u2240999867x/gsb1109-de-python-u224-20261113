#! /usr/bin/env python
"""
Verify how ULog batches can be grouped by the parameters that were in effect
during their measured time intervals.

Two batches must only be merged into the same group when the parameter
configuration actually covering the measurements can be shown to be equal.
For every parameter three outcomes are distinguished:

- explicit_equal: both batches carry the same explicit value (an initial
  parameter, or a logged parameter change covering the interval);
- default_equal: at least one side has no explicit value, but filling the gap
  with that log version's stored default parameter makes the values equal;
- undecidable: the evidence is missing on one side or the values disagree.

A parameter that was only changed at or after the end of the measured
interval does not affect the grouping. The change is kept as evidence but
excluded from the effective value, because pyulog stamps data-section parameter
messages with the timestamp of the preceding data message, so a change logged
after the last sample is never contemporaneous with a sample.

Every verdict is derived from the batch's measured coverage interval together
with its raw parameter evidence (initial parameters, in-window changes,
post-window changes and stored defaults), so the same decision can be
recomputed from the ULog files at any time.
"""

import argparse
import json
import sys

from dataclasses import dataclass, field, asdict

from .core import ULog

# default parameter types as stored in the ULog PARAMETER_DEFAULT message
DEFAULT_TYPE_SYSTEM = 0
DEFAULT_TYPE_CURRENT_SETUP = 1
DEFAULT_TYPE_NAMES = {
    DEFAULT_TYPE_SYSTEM: 'system',
    DEFAULT_TYPE_CURRENT_SETUP: 'current_setup',
}

VERDICT_EXPLICIT_EQUAL = 'explicit_equal'
VERDICT_DEFAULT_EQUAL = 'default_equal'
VERDICT_UNDECIDABLE = 'undecidable'

# reasons attached to undecidable verdicts
REASON_MISSING_EVIDENCE = 'missing_evidence'
REASON_EXPLICIT_MISMATCH = 'explicit_mismatch'
REASON_DEFAULT_MISMATCH = 'default_mismatch'
REASON_VARIABLE_INTERVAL = 'variable_in_interval'

SOURCE_INITIAL = 'initial'
SOURCE_CHANGED = 'changed'
SOURCE_DEFAULT = 'default'


@dataclass
class ParamEvidence:
    """Evidence and effective value of one parameter in one batch."""
    name: str
    # every raw piece of evidence found in the log:
    # (source label, timestamp or None, value or None)
    sources: list = field(default_factory=list)
    # value in effect when the measured interval starts, and where it came from
    start_value: object = None
    start_source: str = None
    # parameter changes strictly inside the measured interval
    in_window_changes: list = field(default_factory=list)
    # changes at or after the interval end, recorded but ignored for grouping
    after_window_changes: list = field(default_factory=list)
    # default value stored by this log version, if any
    default_value: object = None
    default_source: str = None
    # True when the value changes while measurements are being recorded
    is_variable: bool = False
    # ordered values in effect over the interval, starting with start_value
    effective_series: list = field(default_factory=list)
    # effective value when constant over the whole interval, else None
    effective_value: object = None
    effective_source: str = None

    @property
    def has_explicit_evidence(self):
        """True if the batch itself (not its defaults) determines the value."""
        return any(source in (SOURCE_INITIAL, SOURCE_CHANGED)
                   for source, _, _ in self.sources)


@dataclass
class BatchProfile:
    """Parameter evidence and measured coverage of one ULog batch."""
    path: str
    interval_start: int
    interval_end: int
    coverage_from_data: bool
    topic_count: int
    params: dict = field(default_factory=dict)

    def evidence_summary(self):
        """Compact, JSON-serialisable representation."""
        return {
            'path': self.path,
            'measured_interval_us': [self.interval_start, self.interval_end],
            'coverage_from_data_topics': self.coverage_from_data,
            'topic_count': self.topic_count,
            'params': {name: asdict(evidence)
                       for name, evidence in sorted(self.params.items())},
        }


@dataclass
class ParamVerdict:
    """Grouping decision for one parameter between two batches."""
    name: str
    verdict: str
    reason: str = None
    note: str = None
    left: dict = None
    right: dict = None


@dataclass
class PairDecision:
    """Recomputable merge decision for a pair of batches."""
    left_path: str
    right_path: str
    compatible: bool
    verdicts: list = field(default_factory=list)

    def counts(self):
        """Verdict counts keyed by verdict name."""
        result = {}
        for verdict in self.verdicts:
            result[verdict.verdict] = result.get(verdict.verdict, 0) + 1
        return result


def measured_interval(ulog):
    """
    Return (start, end, from_data) actually covered by data samples.

    The interval is the span from the first to the last logged data sample.
    When a log contains no data topics, the header start/last timestamps are
    used as a fallback and from_data is False.
    """
    starts = []
    ends = []
    for dataset in ulog.data_list:
        timestamps = dataset.data['timestamp']
        # zero timestamps are placeholders, not valid sample times
        timestamps = timestamps[timestamps != 0]
        if len(timestamps) > 0:
            starts.append(int(min(timestamps)))
            ends.append(int(max(timestamps)))
    if starts:
        return min(starts), max(ends), True
    return int(ulog.start_timestamp), int(ulog.last_timestamp), False


def _values_equal(left, right):
    """Numerically aware equality for int/float parameter values."""
    return left == right


def _side_snapshot(evidence):
    """Compact per-side snapshot embedded in a verdict."""
    if evidence is None:
        return None
    return {
        'effective_value': evidence.effective_value,
        'effective_source': evidence.effective_source,
        'is_variable': evidence.is_variable,
        'effective_series': evidence.effective_series,
        'start_value': evidence.start_value,
        'start_source': evidence.start_source,
        'default_value': evidence.default_value,
        'in_window_changes': evidence.in_window_changes,
        'after_window_changes': evidence.after_window_changes,
    }


def build_profile(ulog, path, default_type=DEFAULT_TYPE_SYSTEM):
    """
    Build the parameter evidence profile for one parsed ULog batch.

    :param ulog: a parsed ULog object
    :param path: identifier of the batch (usually the file name)
    :param default_type: ULog default parameter type used to fill gaps
    """
    interval_start, interval_end, from_data = measured_interval(ulog)
    defaults = ulog.get_default_parameters(default_type)
    default_source = 'default:' + DEFAULT_TYPE_NAMES[default_type]

    evidence_by_name = {}

    def evidence_for(name):
        if name not in evidence_by_name:
            evidence_by_name[name] = ParamEvidence(name=name)
        return evidence_by_name[name]

    for name, value in ulog.initial_parameters.items():
        evidence = evidence_for(name)
        evidence.sources.append((SOURCE_INITIAL, None, value))
        evidence.start_value = value
        evidence.start_source = SOURCE_INITIAL

    for name, value in defaults.items():
        evidence = evidence_for(name)
        evidence.sources.append((default_source, None, value))
        evidence.default_value = value
        evidence.default_source = default_source

    # pyulog stores data-section parameter changes as (timestamp, name, value);
    # the timestamp is the last data timestamp seen before the message.
    for timestamp, name, value in ulog.changed_parameters:
        evidence = evidence_for(name)
        evidence.sources.append((SOURCE_CHANGED, int(timestamp), value))
        if timestamp < interval_start:
            # happened before the measured interval: it sets the start value
            evidence.start_value = value
            evidence.start_source = SOURCE_CHANGED
        elif timestamp >= interval_end:
            # at or after the last sample: never covers a recorded sample
            evidence.after_window_changes.append([int(timestamp), value])
        else:
            evidence.in_window_changes.append([int(timestamp), value])

    for evidence in evidence_by_name.values():
        if evidence.start_source is None and evidence.default_value is not None:
            # no explicit value at all: fall back to this version's default
            evidence.start_value = evidence.default_value
            evidence.start_source = SOURCE_DEFAULT

        series = []
        if evidence.start_value is not None:
            series.append(evidence.start_value)
        for _, value in evidence.in_window_changes:
            series.append(value)
        evidence.effective_series = series

        if not series:
            # nothing determines the value, not even a default
            continue

        distinct = set()
        for value in series:
            distinct.add(_hashable(value))
        evidence.is_variable = len(distinct) > 1
        if not evidence.is_variable:
            evidence.effective_value = series[0]
            if evidence.start_source is not None:
                evidence.effective_source = evidence.start_source
            elif evidence.in_window_changes:
                evidence.effective_source = SOURCE_CHANGED

    return BatchProfile(
        path=path,
        interval_start=interval_start,
        interval_end=interval_end,
        coverage_from_data=from_data,
        topic_count=len(ulog.data_list),
        params=evidence_by_name,
    )


def _hashable(value):
    """Build a hash key that treats numerically equal int/float values alike."""
    if isinstance(value, bool):
        return ('bool', value)
    if isinstance(value, int):
        return ('num', float(value))
    if isinstance(value, float):
        return ('num', value)
    return ('other', value)


def compare_profiles(left, right):
    """
    Compare two batch profiles parameter by parameter.

    Every parameter known to either batch is considered and the returned
    verdicts carry both sides' evidence so each decision is recomputable.
    """
    verdicts = []
    for name in sorted(set(left.params) | set(right.params)):
        left_evidence = left.params.get(name)
        right_evidence = right.params.get(name)
        verdict = ParamVerdict(
            name=name,
            verdict=VERDICT_UNDECIDABLE,
            left=_side_snapshot(left_evidence),
            right=_side_snapshot(right_evidence),
        )

        left_variable = left_evidence is not None and left_evidence.is_variable
        right_variable = right_evidence is not None and right_evidence.is_variable
        if left_variable or right_variable:
            if (left_variable and right_variable and
                    left_evidence.effective_series ==
                    right_evidence.effective_series):
                verdict.verdict = VERDICT_EXPLICIT_EQUAL
                verdict.note = 'identical in-window change series'
            else:
                verdict.reason = REASON_VARIABLE_INTERVAL
            verdicts.append(verdict)
            continue

        left_value = getattr(left_evidence, 'effective_value', None)
        right_value = getattr(right_evidence, 'effective_value', None)
        if left_value is None or right_value is None:
            verdict.reason = REASON_MISSING_EVIDENCE
            verdicts.append(verdict)
            continue

        if not _values_equal(left_value, right_value):
            if (left_evidence.has_explicit_evidence and
                    right_evidence.has_explicit_evidence):
                verdict.reason = REASON_EXPLICIT_MISMATCH
            else:
                verdict.reason = REASON_DEFAULT_MISMATCH
            verdicts.append(verdict)
            continue

        both_explicit = (left_evidence.has_explicit_evidence and
                         right_evidence.has_explicit_evidence)
        if both_explicit:
            verdict.verdict = VERDICT_EXPLICIT_EQUAL
        else:
            verdict.verdict = VERDICT_DEFAULT_EQUAL
            filled = []
            if not left_evidence.has_explicit_evidence:
                filled.append(left.path)
            if not right_evidence.has_explicit_evidence:
                filled.append(right.path)
            verdict.note = 'default-filled: ' + ', '.join(filled)
        verdicts.append(verdict)

    return verdicts


def pair_decision(profiles, left_index, right_index):
    """Build the recomputable merge decision for one pair of profiles."""
    left = profiles[left_index]
    right = profiles[right_index]
    verdicts = compare_profiles(left, right)
    compatible = all(verdict.verdict != VERDICT_UNDECIDABLE
                     for verdict in verdicts)
    return PairDecision(
        left_path=left.path,
        right_path=right.path,
        compatible=compatible,
        verdicts=verdicts,
    )


def group_profiles(profiles):
    """
    Group batches whose parameter coverage is provably equal.

    Two batches are connected when every parameter is either explicitly equal
    or equal after default filling. Groups are the connected components of
    that relation; every edge is returned as an evidence-backed pair decision.
    """
    decisions = []
    parent = list(range(len(profiles)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left_index in range(len(profiles)):
        for right_index in range(left_index + 1, len(profiles)):
            decision = pair_decision(profiles, left_index, right_index)
            decisions.append(decision)
            if decision.compatible:
                parent[find(right_index)] = find(left_index)

    groups = {}
    for index, profile in enumerate(profiles):
        groups.setdefault(find(index), []).append(profile.path)
    return list(groups.values()), decisions


def _format_value(value):
    if value is None:
        return '<unknown>'
    return str(value)


def _format_side(snapshot):
    if snapshot is None:
        return 'absent'
    source = snapshot['effective_source'] or 'no-evidence'
    return '{} ({})'.format(_format_value(snapshot['effective_value']), source)


def print_report(profiles, groups, decisions, as_json=False):
    """Print the coverage, evidence, pair decisions and groups."""
    if as_json:
        payload = {
            'batches': [profile.evidence_summary() for profile in profiles],
            'pair_decisions': [
                {
                    'left': decision.left_path,
                    'right': decision.right_path,
                    'compatible': decision.compatible,
                    'counts': decision.counts(),
                    'verdicts': [asdict(verdict) for verdict in decision.verdicts],
                }
                for decision in decisions
            ],
            'groups': groups,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    for profile in profiles:
        explicit = sum(1 for evidence in profile.params.values()
                       if evidence.has_explicit_evidence)
        default_filled = sum(1 for evidence in profile.params.values()
                             if evidence.effective_source == SOURCE_DEFAULT)
        unknown = sum(1 for evidence in profile.params.values()
                      if evidence.effective_value is None)
        print('batch: {}'.format(profile.path))
        print('  measured interval: [{}, {}] us ({} data topics, '
              'coverage_from_data={})'.format(
                  profile.interval_start, profile.interval_end,
                  profile.topic_count, profile.coverage_from_data))
        print('  params: {} explicit, {} default-filled, {} undecidable'.format(
            explicit, default_filled, unknown))
        late = [(name, evidence.after_window_changes)
                for name, evidence in profile.params.items()
                if evidence.after_window_changes]
        for name, changes in late:
            print('  ignored post-measurement changes: {} {}'.format(name, changes))

    print('pair decisions:')
    for decision in decisions:
        counts = decision.counts()
        status = 'MERGE' if decision.compatible else 'BLOCKED'
        print('  {} ~ {}: {} {}'.format(
            decision.left_path, decision.right_path, status,
            ', '.join('{}={}'.format(key, counts[key])
                      for key in sorted(counts))))
        for verdict in decision.verdicts:
            if verdict.verdict == VERDICT_UNDECIDABLE:
                print('    {}: undecidable ({}) left={} right={}'.format(
                    verdict.name, verdict.reason,
                    _format_side(verdict.left), _format_side(verdict.right)))
            elif verdict.note:
                print('    {}: {} ({})'.format(
                    verdict.name, verdict.verdict, verdict.note))

    print('groups:')
    for index, group in enumerate(sorted(groups, key=lambda item: item[0])):
        print('  G{}: {}'.format(index + 1, ', '.join(group)))


def main(argv=None):
    """Command line interface."""
    parser = argparse.ArgumentParser(
        description='Group ULog batches by the parameters effectively in use '
                    'during their measured intervals')
    parser.add_argument('filenames', metavar='file.ulg', nargs='+',
                        help='ULog input files (one batch each)')
    parser.add_argument('-d', '--default', dest='default_type',
                        action='store', choices=['system', 'current_setup'],
                        default='system',
                        help='stored default parameters used to fill batches '
                             'without explicit values (default: system)')
    parser.add_argument('--json', dest='as_json', action='store_true',
                        help='emit machine-readable JSON with full evidence')
    args = parser.parse_args(argv)

    default_type = DEFAULT_TYPE_SYSTEM
    if args.default_type == 'current_setup':
        default_type = DEFAULT_TYPE_CURRENT_SETUP

    profiles = []
    for filename in args.filenames:
        ulog = ULog(filename)
        profiles.append(build_profile(ulog, filename, default_type))

    groups, decisions = group_profiles(profiles)
    print_report(profiles, groups, decisions, args.as_json)


if __name__ == '__main__':
    sys.exit(main())
