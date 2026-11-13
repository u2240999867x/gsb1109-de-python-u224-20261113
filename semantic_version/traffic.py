# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Traffic-ledger impact bounds for a removed prerelease contract range.

A traffic ledger mixes precise build records (e.g. ``1.2.0-rc.2+sha.7f3``)
with legacy records that were only captured down to major.minor (e.g.
``1.2``). Every row carries a request volume and the set of concrete builds it
may legitimately be attributed to. Rows sharing a ``(source, release_batch)``
key describe one physical deployment, so all of them must be attributed to the
same build.

When a prerelease range is removed from a contract, attribution uncertainty
means the affected volume cannot be expressed as one number:

* the *lower bound* counts traffic that is certainly affected, regardless of
  how the uncertainty is resolved;
* the *upper bound* counts traffic that could be affected under some valid
  attribution.

Each bound is returned together with the explicit group-to-build attribution
that reaches it, so either figure can be attached to a customer notification.
Legacy rows are never silently folded into "the latest build": their candidate
sets and group constraints decide what is feasible.
"""

from collections import namedtuple, OrderedDict
import warnings

from .base import Version


class InfeasibleAttributionError(ValueError):
    """Raised when a group's rows share no admissible build."""

    def __init__(self, group, rows):
        self.group = group
        self.rows = rows
        super(InfeasibleAttributionError, self).__init__(
            "Rows of group %r have no candidate build in common: %s"
            % (group, ", ".join(row.row_id for row in rows))
        )


class LedgerValidationError(ValueError):
    """Raised when a ledger row is internally inconsistent."""


# A removed prerelease range is the half-open interval [lower, upper).
RemovedRange = namedtuple('RemovedRange', ('lower', 'upper'))


class LedgerRow(namedtuple('LedgerRow', (
        'row_id', 'group', 'recorded', 'volume', 'candidates', 'legacy'))):
    """One ledger line.

    ``recorded`` is the normalized :class:`Version`: a concrete version for a
    precise record, or a partial version (``patch is None``) for a legacy
    record. ``candidates`` is a tuple of concrete builds the row may map to.
    """
    __slots__ = ()

    def __new__(cls, row_id, group, recorded, volume, candidates):
        if not isinstance(volume, int) or volume < 0:
            raise LedgerValidationError(
                "Row %r has an invalid volume %r; expected a non-negative integer"
                % (row_id, volume))
        if not candidates:
            raise LedgerValidationError(
                "Row %r has no candidate builds" % (row_id,))

        legacy, recorded_version = cls._parse_recorded(row_id, recorded)
        candidate_versions = tuple(sorted(set(candidates), key=_version_sort_key))
        for candidate in candidate_versions:
            if candidate.partial or candidate.patch is None:
                raise LedgerValidationError(
                    "Row %r has a non-concrete candidate build %s" % (row_id, candidate))
            if legacy and not _in_legacy_family(recorded_version, candidate):
                raise LedgerValidationError(
                    "Legacy row %r recorded as %s cannot be attributed to %s, "
                    "which lies outside its legacy family %s"
                    % (row_id, recorded, candidate, _family_text(recorded_version)))
        if not legacy:
            recorded_key = recorded_version.truncate('prerelease')
            if not any(
                    candidate.truncate('prerelease') == recorded_key
                    for candidate in candidate_versions):
                # The observed build must remain an admissible attribution.
                raise LedgerValidationError(
                    "Precise row %r must keep its recorded build %s among the candidates"
                    % (row_id, recorded_version))

        return super(LedgerRow, cls).__new__(
            cls, row_id, group, recorded_version, volume, candidate_versions, legacy)

    @staticmethod
    def _parse_recorded(row_id, recorded):
        if isinstance(recorded, Version):
            version = recorded
            text = str(version)
        else:
            text = recorded
            version = None
        try:
            return False, version if version is not None else Version(text)
        except ValueError:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    partial = Version(text, partial=True)
            except ValueError:
                raise LedgerValidationError(
                    "Row %r has an unparseable recorded version %r" % (row_id, text))
            return True, partial


def _version_sort_key(version):
    # precedence_key includes build metadata, breaking ties between builds that
    # share a precedence; the string keeps ordering deterministic.
    return version.precedence_key, str(version)


def _in_legacy_family(recorded_partial, candidate):
    """Whether a concrete build is compatible with a legacy partial record."""
    if candidate.major != recorded_partial.major:
        return False
    if recorded_partial.minor is not None and candidate.minor != recorded_partial.minor:
        return False
    return True


def _family_text(recorded_partial):
    if recorded_partial.minor is None:
        return "%d.x" % recorded_partial.major
    return "%d.%d.x" % (recorded_partial.major, recorded_partial.minor)


class TrafficLedger(object):
    """A validated collection of :class:`LedgerRow` objects."""

    def __init__(self, rows):
        self.rows = tuple(rows)
        seen = set()
        for row in self.rows:
            if row.row_id in seen:
                raise LedgerValidationError("Duplicate row id %r" % row.row_id)
            seen.add(row.row_id)

    @classmethod
    def from_records(cls, records):
        """Build a ledger from plain dictionaries.

        Each record uses the keys ``id``, ``source``, ``batch``, ``recorded``,
        ``volume`` and ``candidates`` (an iterable of version strings). Rows
        sharing ``(source, batch)`` form one attribution group.
        """
        rows = []
        for record in records:
            rows.append(LedgerRow(
                row_id=record['id'],
                group=(record['source'], record['batch']),
                recorded=record['recorded'],
                volume=record['volume'],
                candidates=[Version(text) for text in record['candidates']],
            ))
        return cls(rows)

    def grouped_rows(self):
        groups = OrderedDict()
        for row in self.rows:
            groups.setdefault(row.group, []).append(row)
        return groups

    def group_options(self):
        """Return the admissible build set of every group.

        A group may only use a build that appears in the candidate set of each
        of its rows, hence the intersection.
        """
        options = OrderedDict()
        for group, rows in self.grouped_rows().items():
            builds = set(rows[0].candidates)
            for row in rows[1:]:
                builds &= set(row.candidates)
            if not builds:
                raise InfeasibleAttributionError(group, rows)
            options[group] = builds
        return options


RowAttribution = namedtuple(
    'RowAttribution', ('row_id', 'group', 'recorded', 'volume', 'build', 'affected'))


class ImpactResult(object):
    """Lower/upper affected-volume bounds with witness attributions."""

    __slots__ = (
        'ledger', 'removed_range', 'lower_bound', 'upper_bound', 'total',
        'lower_attribution', 'upper_attribution',
    )

    def __init__(self, ledger, removed_range, lower_bound, upper_bound, total,
                 lower_attribution, upper_attribution):
        self.ledger = ledger
        self.removed_range = removed_range
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.total = total
        self.lower_attribution = lower_attribution
        self.upper_attribution = upper_attribution

    @property
    def is_exact(self):
        return self.lower_bound == self.upper_bound

    def row_table(self, attribution):
        """Expand a group-level attribution into per-row attribution lines."""
        validate_attribution(self.ledger, attribution, self.removed_range)
        lines = []
        for row in self.ledger.rows:
            build = attribution[row.group]
            lines.append(RowAttribution(
                row_id=row.row_id,
                group=row.group,
                recorded=str(row.recorded),
                volume=row.volume,
                build=str(build),
                affected=is_affected(build, self.removed_range),
            ))
        return lines

    def lower_table(self):
        return self.row_table(self.lower_attribution)

    def upper_table(self):
        return self.row_table(self.upper_attribution)


def is_affected(version, removed_range):
    """Whether a concrete build falls inside the removed prerelease range."""
    return removed_range.lower <= version < removed_range.upper


def _pick(options, predicate):
    chosen = [option for option in options if predicate(option)]
    if not chosen:
        return None
    return min(chosen, key=_version_sort_key)


def assess_impact(ledger, removed_range):
    """Compute the certain-lower and possible-upper affected traffic.

    Groups are independent once each has picked one build from its admissible
    set, so each extremum is reached by choosing per group either a safe build
    (outside the removed range) or an affected build (inside it), whenever that
    choice exists. The returned attributions witness both bounds.
    """
    group_options = ledger.group_options()
    group_volume = OrderedDict(
        (group, sum(row.volume for row in rows))
        for group, rows in ledger.grouped_rows().items())

    lower_bound = 0
    upper_bound = 0
    lower_attribution = OrderedDict()
    upper_attribution = OrderedDict()

    for group, options in group_options.items():
        volume = group_volume[group]
        safe_build = _pick(options, lambda build: not is_affected(build, removed_range))
        affected_build = _pick(options, lambda build: is_affected(build, removed_range))

        if safe_build is None:
            # Every admissible build is removed: this traffic is certainly lost.
            lower_bound += volume
            lower_attribution[group] = affected_build
        else:
            lower_attribution[group] = safe_build

        if affected_build is None:
            # No admissible build is removed: this traffic is certainly safe.
            upper_attribution[group] = safe_build
        else:
            upper_bound += volume
            upper_attribution[group] = affected_build

    total = sum(row.volume for row in ledger.rows)
    return ImpactResult(
        ledger=ledger,
        removed_range=removed_range,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        total=total,
        lower_attribution=lower_attribution,
        upper_attribution=upper_attribution,
    )


def validate_attribution(ledger, attribution, removed_range):
    """Check an attribution against every ledger constraint.

    Returns the affected volume it implies, raising ``ValueError`` if a group
    is missing, assigned twice-inconsistently, or mapped to a build outside the
    candidate set of one of its rows.
    """
    expected_groups = set(ledger.grouped_rows())
    given_groups = set(attribution)
    if given_groups != expected_groups:
        raise LedgerValidationError(
            "Attribution groups %r do not match ledger groups %r"
            % (sorted(map(repr, given_groups)), sorted(map(repr, expected_groups))))

    affected_volume = 0
    for row in ledger.rows:
        build = attribution[row.group]
        if build not in row.candidates:
            raise LedgerValidationError(
                "Row %r cannot be attributed to %s, not in its candidate set"
                % (row.row_id, build))
        if is_affected(build, removed_range):
            affected_volume += row.volume
    return affected_volume


# A runnable illustration, also exercised by the test-suite:
#   python -m semantic_version.traffic
_DEMO_RECORDS = [
    {'id': 'r1', 'source': 'edge', 'batch': 'b1', 'recorded': '1.2.0-rc.2',
     'volume': 100, 'candidates': ('1.2.0-rc.1', '1.2.0-rc.2', '1.2.0')},
    {'id': 'r2', 'source': 'edge', 'batch': 'b2', 'recorded': '1.2.0-rc.1',
     'volume': 40, 'candidates': ('1.2.0-rc.1', '1.2.0-rc.2')},
    {'id': 'r3', 'source': 'origin', 'batch': 'b3', 'recorded': '1.2',
     'volume': 60, 'candidates': ('1.2.0-rc.1', '1.2.0', '1.2.1')},
    {'id': 'r4', 'source': 'origin', 'batch': 'b3', 'recorded': '1.2',
     'volume': 30, 'candidates': ('1.2.0-rc.1', '1.2.0')},
    {'id': 'r5', 'source': 'origin', 'batch': 'b4', 'recorded': '1.2',
     'volume': 20, 'candidates': ('1.2.0-rc.1', '1.2.0-rc.2')},
    {'id': 'r6', 'source': 'origin', 'batch': 'b4', 'recorded': '1.2',
     'volume': 25, 'candidates': ('1.2.0-rc.2',)},
    {'id': 'r7', 'source': 'mirror', 'batch': 'b5', 'recorded': '1.1.5',
     'volume': 200, 'candidates': ('1.1.5', '1.1.6')},
]


def _demo():
    ledger = TrafficLedger.from_records(_DEMO_RECORDS)
    removed_range = RemovedRange(Version('1.2.0-rc.1'), Version('1.2.0'))
    result = assess_impact(ledger, removed_range)
    print("Removed prerelease range: [%s, %s)" % removed_range)
    print("Total ledger traffic: %d" % result.total)
    print("Certainly affected (lower bound): %d" % result.lower_bound)
    print("Possibly affected (upper bound):  %d" % result.upper_bound)
    for label, table in (('Lower-bound witness', result.lower_table()),
                         ('Upper-bound witness', result.upper_table())):
        print("\n%s:" % label)
        for line in table:
            print("  %s %s vol=%d -> %s%s" % (
                line.row_id, line.group, line.volume, line.build,
                ' [AFFECTED]' if line.affected else ''))


if __name__ == '__main__':
    _demo()
