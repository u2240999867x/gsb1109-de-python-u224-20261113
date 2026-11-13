#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the archive calendar planner."""

import datetime
import json
import os
import subprocess
import sys
import unittest

from semantic_version.archive import (
    ArchivePlanner,
    Artifact,
    SupportDeclaration,
    plan_archive,
)


class ArchiveTestCase(unittest.TestCase):
    def plan(self, **kwargs):
        kwargs.setdefault('start', '2026-01-01')
        kwargs.setdefault('end', '2026-06-30')
        return ArchivePlanner(**kwargs).plan()

    def keys(self, plan):
        return [(window.start, window.end, window.artifact.key_text) for window in plan.windows]


class BasicWindowTests(ArchiveTestCase):
    def test_single_artifact_covering_calendar(self):
        plan = self.plan(
            artifacts=[{'version': '1.2.3', 'published': '2025-01-01'}],
            declarations=[{'consumer': 'A', 'spec': '>=1.0.0', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.changes, 0)
        window = plan.windows[0]
        self.assertEqual(window.start, datetime.date(2026, 1, 1))
        self.assertEqual(window.end, datetime.date(2026, 6, 30))
        self.assertEqual(window.artifact.key_text, '1.2.3')

    def test_multiple_consumers_intersect(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01'},
                {'version': '1.5.0', 'published': '2025-01-01'},
                {'version': '2.0.0', 'published': '2025-01-01'},
            ],
            declarations=[
                {'consumer': 'A', 'spec': '>=1.0.0,<2.0.0', 'start': '2025-01-01'},
                {'consumer': 'B', 'spec': '>=1.2.0', 'start': '2025-01-01'},
            ],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(str(plan.windows[0].artifact.version), '1.5.0')

    def test_empty_declaration_matches_any(self):
        plan = self.plan(
            artifacts=[{'version': '1.0.0', 'published': '2025-01-01'}],
            declarations=[{'consumer': 'A', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].artifact.display, '1.0.0')

    def test_declaration_objects_and_artifact_objects(self):
        plan = self.plan(
            artifacts=[
                Artifact('1.0.0', artifact_id='build-1', format='deb', published='2025-01-01'),
                Artifact('1.1.0', artifact_id='build-2', format='rpm', published='2025-01-01'),
            ],
            declarations=[
                SupportDeclaration('A', '>=1.0.0', start='2025-01-01', formats=['deb']),
            ],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].artifact.artifact_id, 'build-1')
        self.assertEqual(plan.windows[0].artifact.display, 'build-1 (1.0.0)')

    def test_format_filter(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0', 'format': 'deb', 'published': '2025-01-01'},
                {'version': '1.1.0', 'format': 'rpm', 'published': '2025-01-01'},
                {'version': '1.2.0', 'format': 'tar', 'published': '2025-01-01'},
            ],
            declarations=[
                {'consumer': 'A', 'spec': '*', 'start': '2025-01-01', 'formats': ['deb', 'rpm']},
                {'consumer': 'B', 'spec': '*', 'start': '2025-01-01', 'formats': ['rpm']},
            ],
        )
        self.assertEqual([str(w.artifact.version) for w in plan.windows], ['1.1.0'])


class RenewalGapTests(ArchiveTestCase):
    def test_renewal_gap_is_reported_with_dates(self):
        plan = self.plan(
            artifacts=[{'version': '2.0.0', 'published': '2025-01-01'}],
            declarations=[
                {'id': 'A-2025', 'consumer': 'A', 'spec': '>=1.0.0', 'start': '2025-01-01', 'end': '2026-03-01'},
                {'id': 'A-2026', 'consumer': 'A', 'spec': '>=1.0.0', 'start': '2026-05-01'},
                {'consumer': 'B', 'spec': '*', 'start': '2025-01-01'},
            ],
        )
        self.assertEqual(len(plan.windows), 2)
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 3, 1))
        self.assertEqual(plan.gaps[0].period_start, datetime.date(2026, 3, 1))
        self.assertEqual(plan.gaps[0].period_end, datetime.date(2026, 4, 30))
        self.assertEqual(plan.windows[0].end, datetime.date(2026, 2, 28))
        self.assertEqual(plan.windows[1].start, datetime.date(2026, 5, 1))

        blockers = plan.gaps[0].blockers
        self.assertEqual(len(blockers), 2)
        self.assertEqual(blockers[0]['status'], 'expired')
        self.assertEqual(blockers[0]['expired_on'], '2026-03-01')
        self.assertEqual(blockers[1]['status'], 'pending')
        self.assertEqual(blockers[1]['consumer'], 'A')

    def test_same_day_renewals_coexist(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.4.0', 'published': '2025-01-01'},
                {'version': '1.6.0', 'published': '2025-01-01'},
            ],
            declarations=[
                {'id': 'old', 'consumer': 'A', 'spec': '>=1.0.0,<1.5.0', 'start': '2025-01-01'},
                {'id': 'new', 'consumer': 'A', 'spec': '>=1.5.0', 'start': '2026-06-01'},
            ],
        )
        self.assertEqual(self.keys(plan), [
            (datetime.date(2026, 1, 1), datetime.date(2026, 5, 31), '1.4.0'),
            (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30), '1.6.0'),
        ])

    def test_open_ended_renewal_is_taken_over_by_next_start(self):
        plan = self.plan(
            artifacts=[{'version': '1.0.0', 'published': '2025-01-01'}],
            declarations=[
                {'consumer': 'A', 'spec': '>=1.0.0', 'start': '2025-01-01'},
                {'consumer': 'A', 'spec': '>=1.0.0', 'start': '2026-04-01', 'end': '2026-07-01'},
            ],
        )
        self.assertEqual(self.keys(plan), [
            (datetime.date(2026, 1, 1), datetime.date(2026, 6, 30), '1.0.0'),
        ])

    def test_renewal_with_narrower_spec_causes_switch(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.4.0', 'published': '2025-01-01'},
                {'version': '1.6.0', 'published': '2025-01-01'},
            ],
            declarations=[
                {'consumer': 'A', 'spec': '>=1.0.0,<1.5.0', 'start': '2025-01-01', 'end': '2026-03-01'},
                {'consumer': 'A', 'spec': '>=1.5.0,<2.0.0', 'start': '2026-03-01'},
            ],
        )
        self.assertEqual(self.keys(plan), [
            (datetime.date(2026, 1, 1), datetime.date(2026, 2, 28), '1.4.0'),
            (datetime.date(2026, 3, 1), datetime.date(2026, 6, 30), '1.6.0'),
        ])
        self.assertEqual(plan.changes, 1)


class SpanningRequirementTests(ArchiveTestCase):
    def test_endpoint_availability_does_not_imply_spanning_artifact(self):
        plan = self.plan(
            artifacts=[
                {'id': 'left', 'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-03-01'},
                {'id': 'right', 'version': '1.1.0', 'published': '2026-04-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 2)
        self.assertEqual(plan.windows[0].end, datetime.date(2026, 2, 28))
        self.assertEqual(plan.windows[1].start, datetime.date(2026, 4, 1))
        self.assertEqual(len(plan.gaps), 1)
        self.assertEqual(plan.gaps[0].reason, 'catalog-withdrawal')
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 3, 1))
        kinds = set(blocker['kind'] for blocker in plan.gaps[0].blockers)
        self.assertEqual(kinds, set(['withdrawal', 'not-published', 'declaration']))
        blockers = dict((blocker['kind'], blocker) for blocker in plan.gaps[0].blockers)
        self.assertEqual(blockers['withdrawal']['artifacts'], ['left'])
        self.assertEqual(blockers['not-published']['artifact'], 'right')

    def test_missing_artifact_so_no_window(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-02-01'},
                {'version': '2.0.0', 'published': '2025-01-01'},
            ],
            declarations=[
                {'consumer': 'A', 'spec': '>=1.0.0,<2.0.0', 'start': '2025-01-01'},
            ],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(len(plan.gaps), 1)
        self.assertEqual(plan.gaps[0].reason, 'catalog-withdrawal')
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 2, 1))

    def test_month_gap_reports_non_spanning_reason(self):
        plan = self.plan(
            start='2026-01',
            end='2026-03',
            granularity='month',
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-02-10'},
                {'version': '2.0.0', 'published': '2026-02-20'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 2)
        self.assertEqual(plan.windows[1].artifact.key_text, '2.0.0')
        self.assertEqual(plan.windows[1].start, datetime.date(2026, 3, 1))
        self.assertEqual(plan.gaps[0].period_start, datetime.date(2026, 2, 1))
        self.assertEqual(plan.gaps[0].period_end, datetime.date(2026, 2, 28))
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 2, 10))
        self.assertEqual(plan.gaps[0].reason, 'no-spanning-artifact')
        self.assertEqual(plan.gaps[0].lost_artifacts, ['1.0.0'])


class GapMergingTests(ArchiveTestCase):
    def test_distinct_catalog_blockers_do_not_merge(self):
        plan = self.plan(
            start='2026-01-01',
            end='2026-07-31',
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-02-01'},
                {'version': '1.1.0', 'published': '2026-04-01', 'withdrawn': '2026-05-01'},
                {'version': '1.2.0', 'published': '2026-06-15'},
            ],
            declarations=[{'consumer': 'A', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.gaps), 2)
        self.assertEqual(plan.gaps[0].period_start, datetime.date(2026, 2, 1))
        self.assertEqual(plan.gaps[0].period_end, datetime.date(2026, 3, 31))
        self.assertEqual(plan.gaps[1].period_start, datetime.date(2026, 5, 1))
        self.assertEqual(plan.gaps[1].period_end, datetime.date(2026, 6, 14))


class BoundaryTests(ArchiveTestCase):
    def test_publication_day_is_inclusive(self):
        plan = self.plan(
            start='2026-01-01',
            end='2026-01-31',
            artifacts=[{'version': '1.0.0', 'published': '2026-01-01'}],
            declarations=[{'consumer': 'A', 'start': '2025-01-01'}],
        )
        self.assertEqual(plan.windows[0].start, datetime.date(2026, 1, 1))

    def test_withdrawal_day_is_exclusive(self):
        plan = self.plan(
            start='2026-01-01',
            end='2026-01-31',
            artifacts=[{'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-02-01'}],
            declarations=[{'consumer': 'A', 'start': '2025-01-01'}],
        )
        self.assertEqual(plan.windows[0].end, datetime.date(2026, 1, 31))
        self.assertEqual(plan.gaps, [])

    def test_declaration_end_is_exclusive(self):
        plan = self.plan(
            start='2026-01-01',
            end='2026-01-31',
            artifacts=[{'version': '1.0.0', 'published': '2025-01-01'}],
            declarations=[{'consumer': 'A', 'start': '2025-01-01', 'end': '2026-02-01'}],
        )
        self.assertEqual(plan.windows[0].end, datetime.date(2026, 1, 31))

    def test_declaration_must_end_after_start(self):
        with self.assertRaises(ValueError):
            SupportDeclaration('A', '*', start='2026-01-31', end='2026-01-31')


class GapTriggerTests(ArchiveTestCase):
    def test_month_gap_reports_collapse_day_not_first_empty_day(self):
        plan = self.plan(
            start='2026-01',
            end='2026-03',
            granularity='month',
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-02-10'},
                {'version': '2.0.0', 'published': '2026-02-20'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 2, 10))
        self.assertEqual(plan.gaps[0].period_start, datetime.date(2026, 2, 1))
        self.assertIn('withdrawal', [b['kind'] for b in plan.gaps[0].blockers])

    def test_release_and_prerelease_use_precedence(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0-alpha', 'published': '2025-01-01'},
                {'version': '1.0.0', 'published': '2025-01-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(str(plan.windows[0].artifact.version), '1.0.0')


class WithdrawalTests(ArchiveTestCase):
    def test_specific_artifact_withdrawal(self):
        plan = self.plan(
            artifacts=[
                {'id': 'good', 'version': '1.0.0', 'published': '2025-01-01'},
                {'id': 'bad', 'version': '1.1.0', 'published': '2025-01-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
            withdrawals=[{'id': 'W-1', 'effective': '2026-04-01', 'artifact_ids': ['bad']}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].artifact.key_text, 'good')

    def test_range_withdrawal_blocks_all_candidates(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.2.0', 'published': '2025-01-01'},
                {'version': '1.3.0', 'published': '2025-01-01'},
                {'version': '2.0.0', 'published': '2025-01-01', 'withdrawn': '2026-01-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '>=1.0.0,<2.0.0', 'start': '2025-01-01'}],
            withdrawals=[{'id': 'W-1', 'effective': '2026-03-01', 'spec': '>=1.2.0,<1.4.0'}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].end, datetime.date(2026, 2, 28))
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 3, 1))
        self.assertEqual(plan.gaps[0].reason, 'catalog-withdrawal')
        blocker = plan.gaps[0].blockers[0]
        self.assertEqual(blocker['kind'], 'withdrawal')
        self.assertEqual(blocker['id'], 'W-1')
        self.assertEqual(sorted(blocker['artifacts']), ['1.2.0', '1.3.0'])

    def test_format_specific_withdrawal(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0', 'format': 'deb', 'published': '2025-01-01'},
                {'version': '1.0.0', 'format': 'rpm', 'published': '2025-01-01'},
            ],
            declarations=[
                {'consumer': 'A', 'spec': '*', 'start': '2025-01-01', 'formats': ['deb', 'rpm']},
            ],
            withdrawals=[{'effective': '2026-03-01', 'spec': '*', 'formats': ['deb']}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].artifact.key_text, '1.0.0/rpm')

    def test_temporary_withdrawal(self):
        plan = self.plan(
            artifacts=[{'version': '1.0.0', 'published': '2025-01-01'}],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
            withdrawals=[{
                'id': 'W-1', 'effective': '2026-03-01', 'until': '2026-04-01',
                'artifact_ids': ['1.0.0'],
            }],
        )
        self.assertEqual(self.keys(plan), [
            (datetime.date(2026, 1, 1), datetime.date(2026, 2, 28), '1.0.0'),
            (datetime.date(2026, 4, 1), datetime.date(2026, 6, 30), '1.0.0'),
        ])
        self.assertEqual(plan.gaps[0].date, datetime.date(2026, 3, 1))
        self.assertEqual(plan.gaps[0].period_start, datetime.date(2026, 3, 1))
        self.assertEqual(plan.gaps[0].period_end, datetime.date(2026, 3, 31))


class MinimalChangesTests(ArchiveTestCase):
    def test_prefers_fewest_switches(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01'},
                {'version': '1.1.0', 'published': '2025-01-01'},
                {'version': '1.2.0', 'published': '2025-01-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(str(plan.windows[0].artifact.version), '1.2.0')

    def test_prefers_fewer_changes_over_newest_version(self):
        plan = self.plan(
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01'},
                {'version': '2.0.0', 'published': '2026-03-01', 'withdrawn': '2026-05-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].artifact.display, '1.0.0')
        self.assertEqual(plan.changes, 0)

    def test_reaches_longest_available_candidate(self):
        plan = self.plan(
            start='2026-01-01',
            end='2026-12-31',
            artifacts=[
                {'version': '1.0.0', 'published': '2025-01-01', 'withdrawn': '2026-07-01'},
                {'version': '1.5.0', 'published': '2025-01-01', 'withdrawn': '2026-12-01'},
                {'version': '2.0.0', 'published': '2025-01-01'},
            ],
            declarations=[{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        )
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(str(plan.windows[0].artifact.version), '2.0.0')


class ConfigurationTests(ArchiveTestCase):
    def test_plan_archive_convenience(self):
        plan = plan_archive({
            'start': '2026-01-01',
            'end': '2026-12-31',
            'artifacts': [
                {'version': '1.0.0', 'published': '2025-01-01'},
            ],
            'consumers': {
                'A': [{'spec': '*', 'start': '2025-01-01'}],
                'B': [{'spec': '*', 'start': '2026-06-01'}],
            },
        })
        self.assertEqual(plan.windows[0].artifact.display, '1.0.0')
        self.assertEqual(plan.windows[0].start, datetime.date(2026, 6, 1))
        self.assertEqual(plan.gaps[0].period_start, datetime.date(2026, 1, 1))
        self.assertEqual(plan.gaps[0].period_end, datetime.date(2026, 5, 31))

    def test_json_structure_is_serializable(self):
        plan = plan_archive({
            'start': '2026-01-01',
            'end': '2026-02-28',
            'artifacts': [
                {'version': '1.0.0', 'published': '2025-01-01'},
            ],
            'declarations': [
                {'consumer': 'A', 'spec': '*', 'start': '2025-01-01'},
            ],
        })
        payload = json.loads(plan.to_json())
        self.assertEqual(payload['start'], '2026-01-01')
        self.assertEqual(payload['windows'][0]['artifact']['version'], '1.0.0')

    def test_cli_json_output(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env['PYTHONPATH'] = root
        payload = {
            'start': '2026-01-01',
            'end': '2026-01-31',
            'artifacts': [{'version': '1.0.0', 'published': '2025-01-01'}],
            'declarations': [{'consumer': 'A', 'spec': '*', 'start': '2025-01-01'}],
        }
        completed = subprocess.run(
            [sys.executable, '-m', 'semantic_version.archive', '--format', 'json'],
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            universal_newlines=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result['windows'][0]['artifact']['version'], '1.0.0')


class DeclarationParsingTests(unittest.TestCase):
    def test_month_dates_are_expanded_to_month_ends(self):
        declaration = SupportDeclaration(
            'A', '*', start='2026-02', end='2026-05',
        )
        self.assertEqual(declaration.start, datetime.date(2026, 2, 1))
        self.assertEqual(declaration.end, datetime.date(2026, 6, 1))

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            SupportDeclaration('A', '*', start='2026-13-01', end='2027-01-01')

    def test_npm_syntax_is_supported(self):
        from semantic_version import Version
        declaration = SupportDeclaration('A', '^1.2.0', syntax='npm', start='2026-01-01')
        self.assertEqual(declaration.syntax, 'npm')
        self.assertTrue(declaration.spec.match(Version('1.4.0')))
        self.assertFalse(declaration.spec.match(Version('2.0.0')))


if __name__ == '__main__':
    unittest.main()
