#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Tests for traffic-ledger impact bounds."""

import unittest

from semantic_version import Version
from semantic_version.traffic import (
    InfeasibleAttributionError,
    LedgerRow,
    LedgerValidationError,
    RemovedRange,
    TrafficLedger,
    assess_impact,
    is_affected,
    validate_attribution,
)


def rc_range():
    return RemovedRange(Version('1.2.0-rc.1'), Version('1.2.0'))


DEMO_RECORDS = [
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


class ImpactBoundsTestCase(unittest.TestCase):

    def setUp(self):
        self.ledger = TrafficLedger.from_records(DEMO_RECORDS)
        self.result = assess_impact(self.ledger, rc_range())

    def test_total_and_bounds(self):
        self.assertEqual(475, self.result.total)
        # b2 (40) and b4 (45) have no safe admissible build; everything else
        # but the mirror group (200) could be affected under some attribution.
        self.assertEqual(85, self.result.lower_bound)
        self.assertEqual(275, self.result.upper_bound)
        self.assertFalse(self.result.is_exact)
        self.assertLessEqual(self.result.lower_bound, self.result.upper_bound)

    def test_witnesses_reach_their_bounds(self):
        lower_volume = validate_attribution(
            self.ledger, self.result.lower_attribution, rc_range())
        upper_volume = validate_attribution(
            self.ledger, self.result.upper_attribution, rc_range())
        self.assertEqual(self.result.lower_bound, lower_volume)
        self.assertEqual(self.result.upper_bound, upper_volume)

    def test_group_picks_one_shared_build(self):
        for attribution in (self.result.lower_attribution,
                            self.result.upper_attribution):
            b4_build = attribution[('origin', 'b4')]
            for row in self.ledger.rows:
                if row.group == ('origin', 'b4'):
                    self.assertIn(b4_build, row.candidates)

    def test_forced_group_is_certainly_affected(self):
        # b4's row intersection is only 1.2.0-rc.2: 20 + 25 = 45.
        forced = self.result.lower_attribution[('origin', 'b4')]
        self.assertEqual(Version('1.2.0-rc.2'), forced)
        self.assertTrue(is_affected(forced, rc_range()))
        b3_build = self.result.lower_attribution[('origin', 'b3')]
        self.assertFalse(is_affected(b3_build, rc_range()))

    def test_upper_bound_uses_affected_build_whenever_available(self):
        for group, build in self.result.upper_attribution.items():
            options = self.ledger.group_options()[group]
            if any(is_affected(option, rc_range()) for option in options):
                self.assertTrue(is_affected(build, rc_range()))

    def test_row_tables(self):
        lower_lines = self.result.lower_table()
        self.assertEqual(7, len(lower_lines))
        self.assertEqual(85, sum(line.volume for line in lower_lines if line.affected))
        upper_lines = self.result.upper_table()
        self.assertEqual(275, sum(line.volume for line in upper_lines if line.affected))

    def test_safe_traffic_never_counts_toward_either_bound(self):
        mirror_group = ('mirror', 'b5')
        for attribution in (self.result.lower_attribution,
                            self.result.upper_attribution):
            self.assertFalse(is_affected(attribution[mirror_group], rc_range()))


class ExactLedgerTestCase(unittest.TestCase):

    def test_single_option_groups_collapse_bounds(self):
        records = [
            {'id': 'a', 'source': 's1', 'batch': 'x', 'recorded': '1.2.0-rc.1',
             'volume': 10, 'candidates': ('1.2.0-rc.1',)},
            {'id': 'b', 'source': 's2', 'batch': 'y', 'recorded': '1.1.0',
             'volume': 7, 'candidates': ('1.1.0',)},
        ]
        result = assess_impact(TrafficLedger.from_records(records), rc_range())
        self.assertEqual(10, result.lower_bound)
        self.assertEqual(10, result.upper_bound)
        self.assertTrue(result.is_exact)


class BuildMetadataTestCase(unittest.TestCase):

    def test_build_metadata_follows_precedence_range(self):
        row = LedgerRow(
            'm1', ('s', 'g'), '1.2.0-rc.2', 5,
            (Version('1.2.0-rc.2+sha.aaa'), Version('1.2.0-rc.2+sha.bbb')))
        result = assess_impact(TrafficLedger([row]), rc_range())
        self.assertEqual(5, result.lower_bound)
        self.assertEqual(5, result.upper_bound)
        self.assertTrue(str(result.lower_attribution[('s', 'g')]).startswith('1.2.0-rc.2'))

    def test_metadata_variant_of_release_is_safe(self):
        row = LedgerRow(
            'm2', ('s', 'g'), '1.2.0', 8,
            (Version('1.2.0+sha.ccc'), Version('1.2.0')))
        result = assess_impact(TrafficLedger([row]), rc_range())
        self.assertEqual(0, result.lower_bound)
        self.assertEqual(0, result.upper_bound)


class LegacyFamilyTestCase(unittest.TestCase):

    def test_major_only_family_spans_minors(self):
        row = LedgerRow(
            'legacy-major', ('s', 'g'), '1', 12,
            (Version('1.0.5'), Version('1.2.0-rc.2'), Version('1.9.9')))
        self.assertTrue(row.legacy)
        result = assess_impact(TrafficLedger([row]), rc_range())
        self.assertEqual(0, result.lower_bound)
        self.assertEqual(12, result.upper_bound)

    def test_major_family_rejects_other_major(self):
        with self.assertRaises(LedgerValidationError):
            LedgerRow('bad-major', ('s', 'g'), '1', 1, (Version('2.0.0'),))

    def test_minor_only_family_rejects_outside_candidate(self):
        with self.assertRaises(LedgerValidationError):
            LedgerRow('bad', ('s', 'g'), '1.2', 1, (Version('1.3.0'),))

    def test_unparseable_record(self):
        with self.assertRaises(LedgerValidationError):
            LedgerRow('bad', ('s', 'g'), 'not-a-version', 1, (Version('1.2.0'),))


class ValidationTestCase(unittest.TestCase):

    def test_bad_volume(self):
        with self.assertRaises(LedgerValidationError):
            LedgerRow('neg', ('s', 'g'), '1.2.0', -1, (Version('1.2.0'),))

    def test_empty_candidates(self):
        with self.assertRaises(LedgerValidationError):
            LedgerRow('empty', ('s', 'g'), '1.2.0', 1, ())

    def test_non_concrete_candidate(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            partial = Version('1.2', partial=True)
        with self.assertRaises(LedgerValidationError):
            LedgerRow('partial', ('s', 'g'), '1.2.0', 1, (partial,))

    def test_precise_record_pins_version(self):
        with self.assertRaises(LedgerValidationError):
            LedgerRow('pinned', ('s', 'g'), '1.2.0-rc.1', 1,
                      (Version('1.2.0-rc.2'),))

    def test_duplicate_row_id(self):
        records = [
            {'id': 'dup', 'source': 's1', 'batch': 'x', 'recorded': '1.1.0',
             'volume': 1, 'candidates': ('1.1.0',)},
            {'id': 'dup', 'source': 's2', 'batch': 'y', 'recorded': '1.1.0',
             'volume': 2, 'candidates': ('1.1.0',)},
        ]
        with self.assertRaises(LedgerValidationError):
            TrafficLedger.from_records(records)

    def test_infeasible_group_intersection(self):
        records = [
            {'id': 'g1a', 'source': 's', 'batch': 'g', 'recorded': '1.2',
             'volume': 1, 'candidates': ('1.2.0-rc.1', '1.2.0')},
            {'id': 'g1b', 'source': 's', 'batch': 'g', 'recorded': '1.2',
             'volume': 1, 'candidates': ('1.2.1',)},
        ]
        ledger = TrafficLedger.from_records(records)
        with self.assertRaises(InfeasibleAttributionError):
            assess_impact(ledger, rc_range())

    def test_validate_attribution_rejects_foreign_build(self):
        ledger = TrafficLedger.from_records(DEMO_RECORDS)
        attribution = dict(ledger.group_options())
        for group in attribution:
            attribution[group] = next(iter(attribution[group]))
        attribution[('edge', 'b1')] = Version('9.9.9')
        with self.assertRaises(LedgerValidationError):
            validate_attribution(ledger, attribution, rc_range())


if __name__ == '__main__':
    unittest.main()
