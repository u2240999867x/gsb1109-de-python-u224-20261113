#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Tests for re-publication reconciliation."""

import unittest

import semantic_version
from semantic_version import reconciliation as svr


# A version name re-published under two distinct builds.
ORIGINAL = svr.Build(label='build.1', digest='sha256-aaa')
REBUILT = svr.Build(label='build.2', digest='sha256-bbb')


def make_notice(notice_id='N-1', version_range='>=2.4.0,<2.5.0',
                fields=('build',), expected=REBUILT,
                builds=(ORIGINAL, REBUILT), affected=('2.4.1',)):
    return svr.CorrectionNotice(
        notice_id=notice_id,
        version_range=version_range,
        fields=fields,
        expected_build=expected,
        builds=builds,
        affected_versions=affected,
    )


class ReconcileTests(unittest.TestCase):

    def setUp(self):
        self.notice = make_notice()
        self.records = [
            # Kept the new build -> already correct, reusable.
            svr.SourceRecord('alpha', '2026-11-01', '2.4.1',
                             svr.ArtifactDigest('build.2', 'sha256-bbb')),
            # Rolled back to the original build -> must be re-fetched.
            svr.SourceRecord('beta', '2026-11-01', '2.4.1',
                             svr.ArtifactDigest('build.1', 'sha256-aaa')),
            svr.SourceRecord('beta', '2026-11-02', '2.4.1',
                             svr.ArtifactDigest('build.1', 'sha256-aaa')),
            # Legacy batch: only the version name, no artifact summary.
            svr.SourceRecord('beta', '2026-10-30', '2.4.1',
                             svr.ArtifactDigest(None)),
            # Outside the corrected version range.
            svr.SourceRecord('gamma', '2026-11-01', '2.6.0',
                             svr.ArtifactDigest('build.9', 'sha256-zzz')),
            # In range but carrying an unknown build label.
            svr.SourceRecord('delta', '2026-11-01', '2.4.1',
                             svr.ArtifactDigest('build.X', 'sha256-???')),
        ]

    def test_classification(self):
        report = svr.reconcile(self.records, [self.notice])

        self.assertEqual(
            sorted(report.refetch),
            [('beta', '2026-11-01'), ('beta', '2026-11-02')],
        )
        self.assertEqual(sorted(report.reusable), [('alpha', '2026-11-01')])
        self.assertEqual(
            sorted(report.unconfirmed),
            [('beta', '2026-10-30'), ('delta', '2026-11-01')],
        )
        self.assertEqual(sorted(report.out_of_scope), [('gamma', '2026-11-01')])

    def test_refetch_dates_per_source(self):
        report = svr.reconcile(self.records, [self.notice])
        self.assertEqual(
            report.refetch_dates('beta'),
            ['2026-11-01', '2026-11-02'],
        )
        self.assertEqual(report.refetch_dates('alpha'), [])
        self.assertEqual(
            report.refetch_dates(),
            ['2026-11-01', '2026-11-02'],
        )

    def test_records_are_sorted(self):
        report = svr.reconcile(self.records, [self.notice])
        self.assertEqual(
            [r.key for r in report.refetch_records],
            [('beta', '2026-11-01'), ('beta', '2026-11-02')],
        )

    def test_version_range_bounds(self):
        # 2.5.0 is excluded by the upper bound; 2.3.9 by the lower one.
        for outside in ('2.3.9', '2.5.0'):
            self.assertFalse(
                self.notice.spec.match(semantic_version.Version(outside)))
        for inside in ('2.4.0', '2.4.1', '2.4.99'):
            self.assertTrue(
                self.notice.spec.match(semantic_version.Version(inside)))

    def test_explicit_affected_versions_narrows_scope(self):
        # The range matches 2.4.0 but only 2.4.1 is explicitly affected.
        self.assertFalse(
            self.notice.applies_to(semantic_version.Version('2.4.0')))
        self.assertTrue(
            self.notice.applies_to(semantic_version.Version('2.4.1')))

    def test_field_scope_prerelease_only(self):
        notice = make_notice(
            fields=('prerelease',),
            version_range='>=2.4.0,<2.5.0',
            affected=('2.4.1-rc1',),
        )
        records = [
            # No prerelease component -> the notice does not touch it.
            svr.SourceRecord('a', '2026-11-01', '2.4.1',
                             svr.ArtifactDigest('build.1', 'sha256-aaa')),
            # Carries a prerelease -> in scope, wrong digest -> refetch.
            svr.SourceRecord('b', '2026-11-01', '2.4.1-rc1',
                             svr.ArtifactDigest('build.1', 'sha256-aaa')),
        ]
        report = svr.reconcile(records, [notice])
        self.assertEqual(sorted(report.out_of_scope), [('a', '2026-11-01')])
        self.assertEqual(sorted(report.refetch), [('b', '2026-11-01')])

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            make_notice(fields=('checksum',))

    def test_legacy_reusable_when_builds_clean_identically(self):
        # Both builds share a digest: cleaning output cannot differ.
        same_a = svr.Build('build.1', 'sha256-same')
        same_b = svr.Build('build.2', 'sha256-same')
        notice = make_notice(
            expected=same_b, builds=(same_a, same_b),
        )
        record = svr.SourceRecord('legacy', '2026-10-30', '2.4.1',
                                  svr.ArtifactDigest(None))
        report = svr.reconcile([record], [notice])
        self.assertEqual(sorted(report.reusable), [('legacy', '2026-10-30')])
        self.assertFalse(notice.builds_clean_differently())

    def test_builds_clean_differently(self):
        self.assertTrue(self.notice.builds_clean_differently())
        single = make_notice(builds=(REBUILT,))
        self.assertFalse(single.builds_clean_differently())

    def test_tuple_artifact_without_digest(self):
        record = svr.SourceRecord('legacy', '2026-10-30', '2.4.1', (None,))
        self.assertIsNone(record.artifact.digest)
        self.assertFalse(record.artifact.has_summary)

    def test_reimport_same_notice_is_idempotent(self):
        reconciler = svr.Reconciler()
        self.assertTrue(reconciler.add_notice(self.notice))
        # Second import of the identical notice is a no-op.
        self.assertFalse(reconciler.add_notice(self.notice))
        self.assertFalse(reconciler.add_notice(make_notice()))

        first = reconciler.reconcile(self.records)
        second = reconciler.reconcile(self.records)

        self.assertEqual(first.processed_notice_ids, ['N-1'])
        self.assertEqual(second.processed_notice_ids, ['N-1'])
        self.assertEqual(sorted(first.refetch), sorted(second.refetch))
        self.assertEqual(len(second.refetch), 2)  # no extra scheduled round
        self.assertEqual(len(second.reusable), 1)
        self.assertEqual(len(second.unconfirmed), 2)

    def test_distinct_notices_both_imported(self):
        reconciler = svr.Reconciler()
        reconciler.add_notice(self.notice)
        other = make_notice(notice_id='N-2', version_range='>=2.6.0,<2.7.0',
                            affected=('2.6.0',))
        self.assertTrue(reconciler.add_notice(other))
        report = reconciler.reconcile(self.records)
        self.assertEqual(sorted(report.processed_notice_ids), ['N-1', 'N-2'])
        # The formerly out-of-scope 2.6.0 record is now covered.
        self.assertNotIn(('gamma', '2026-11-01'), report.out_of_scope)

    def test_functional_wrapper_matches_reconciler(self):
        wrapper = svr.reconcile(self.records, [self.notice])
        reconciler = svr.Reconciler()
        reconciler.add_notice(self.notice)
        direct = reconciler.reconcile(self.records)
        self.assertEqual(sorted(wrapper.refetch), sorted(direct.refetch))
        self.assertEqual(sorted(wrapper.reusable), sorted(direct.reusable))
        self.assertEqual(
            sorted(wrapper.unconfirmed), sorted(direct.unconfirmed))


if __name__ == '__main__':
    unittest.main()
