#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Tests for migrating loose collection records to strict SemVer."""

import datetime
import unittest
import warnings

import semantic_version
from semantic_version import (
    REASON_CHANNEL,
    REASON_CUTOFF,
    LegacyRecord,
    Release,
    ReleaseCatalogue,
    Version,
    migrate,
    migrate_record,
)


COLLECTION_DATE = datetime.date(2024, 6, 1)


def build_catalogue():
    return ReleaseCatalogue([
        # 1.2.x: only 1.2.7 ever reached pypi; other builds are internal.
        Release('1.2.7', 'pypi', '2024-01-10'),
        Release('1.2.7', 'internal', '2024-01-11'),
        Release('1.2.9', 'internal', '2024-02-01'),
        # 2.0.x: several patches available on pypi at collection time.
        Release('2.0.0', 'pypi', '2024-03-01'),
        Release('2.0.3', 'pypi', '2024-05-01'),
        # 3.4.x: the second patch was only published after collection.
        Release('3.4.0', 'pypi', '2024-04-01'),
        Release('3.4.1', 'pypi', '2024-07-15'),
        # An exact, fully recorded release.
        Release('4.1.2', 'pypi', '2024-02-02'),
        # 5.0.0 exists, but never reached pypi.
        Release('5.0.0', 'internal', '2024-01-01'),
        # 6.1.x has no .0 release at all.
        Release('6.1.5', 'pypi', '2024-03-10'),
        Release('6.1.8', 'pypi', '2024-05-20'),
        # A single 7.x release, recorded by major only.
        Release('7.1.2', 'pypi', '2024-05-01'),
        # 9.0.0 was published twice to the same channel.
        Release('9.0.0', 'pypi', '2024-01-01'),
        Release('9.0.0', 'pypi', '2024-03-03'),
    ])


class MigrationTestCase(unittest.TestCase):

    def setUp(self):
        self.catalogue = build_catalogue()

    def record(self, identifier, version_text, channel='pypi', collected_on=COLLECTION_DATE):
        return LegacyRecord(identifier, version_text, channel, collected_on)

    def test_partial_record_completes_when_channel_has_single_match(self):
        result = migrate_record(self.record('r12', '1.2'), self.catalogue)
        self.assertTrue(result.resolved)
        self.assertEqual(Version('1.2.7'), result.version)

        excluded_versions = {
            (item.release.version, item.reason) for item in result.excluded
        }
        self.assertIn((Version('1.2.7'), REASON_CHANNEL), excluded_versions)
        self.assertIn((Version('1.2.9'), REASON_CHANNEL), excluded_versions)
        self.assertTrue(all(
            item.release.channel == 'internal' for item in result.excluded
        ))

    def test_partial_record_keeps_feasible_set_with_several_matches(self):
        result = migrate_record(self.record('r20', '2.0'), self.catalogue)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.version)
        self.assertEqual(
            (Version('2.0.0'), Version('2.0.3')),
            result.feasible_versions,
        )
        self.assertEqual(2, len(result.candidates))
        self.assertIn('feasible set', result.reason)

    def test_missing_patch_is_never_filled_with_zero(self):
        result = migrate_record(self.record('r61', '6.1'), self.catalogue)
        self.assertFalse(result.resolved)
        self.assertNotIn(Version('6.1.0'), result.feasible_versions)
        self.assertEqual(
            (Version('6.1.5'), Version('6.1.8')),
            result.feasible_versions,
        )

    def test_release_after_collection_date_is_excluded_and_may_unlock_completion(self):
        result = migrate_record(self.record('r34', '3.4'), self.catalogue)
        self.assertTrue(result.resolved)
        self.assertEqual(Version('3.4.0'), result.version)

        cutoff = [item for item in result.excluded if item.reason == REASON_CUTOFF]
        self.assertEqual(1, len(cutoff))
        self.assertEqual(Version('3.4.1'), cutoff[0].release.version)
        self.assertEqual(
            datetime.date(2024, 7, 15), cutoff[0].release.released_on,
        )

    def test_exact_record_passes_through_with_unique_artifact(self):
        result = migrate_record(self.record('r41', '4.1.2'), self.catalogue)
        self.assertTrue(result.resolved)
        self.assertEqual(Version('4.1.2'), result.version)
        self.assertEqual((), result.excluded)

    def test_version_match_on_unknown_channel_is_not_a_candidate(self):
        result = migrate_record(self.record('r50', '5.0.0'), self.catalogue)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.version)
        self.assertEqual((), result.candidates)
        self.assertEqual(1, len(result.excluded))
        excluded = result.excluded[0]
        self.assertEqual(Version('5.0.0'), excluded.release.version)
        self.assertEqual('internal', excluded.release.channel)
        self.assertEqual(REASON_CHANNEL, excluded.reason)
        self.assertIn('ever published', result.reason)

    def test_major_only_record_completes_from_single_release(self):
        result = migrate_record(self.record('r7', '7'), self.catalogue)
        self.assertTrue(result.resolved)
        self.assertEqual(Version('7.1.2'), result.version)

    def test_unknown_version_line_leaves_unresolved_without_guessing(self):
        result = migrate_record(self.record('r8', '8.1.0'), self.catalogue)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.version)
        self.assertEqual((), result.candidates)
        self.assertEqual((), result.excluded)
        self.assertIn('No published release', result.reason)

    def test_repeated_publish_of_same_version_is_ambiguous(self):
        result = migrate_record(self.record('r9', '9.0.0'), self.catalogue)
        self.assertFalse(result.resolved)
        self.assertEqual(2, len(result.candidates))
        self.assertEqual((Version('9.0.0'),), result.feasible_versions)
        self.assertIn('Several builds', result.reason)

    def test_partial_parsing_does_not_emit_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            migrate_record(self.record('r12', '1.2'), self.catalogue)
        self.assertEqual(
            [], [warning for warning in caught if issubclass(warning.category, DeprecationWarning)],
        )

    def test_catalogue_level_cutoff(self):
        catalogue = ReleaseCatalogue(
            [Release('3.4.1', 'pypi', '2024-07-15')],
            as_of='2024-06-01',
        )
        record = self.record('r34', '3.4', collected_on=datetime.date(2024, 12, 1))
        result = migrate_record(record, catalogue)
        self.assertFalse(result.resolved)
        self.assertEqual((), result.candidates)
        self.assertEqual(REASON_CUTOFF, result.excluded[0].reason)

    def test_catalogue_from_channel_directories(self):
        directories = {
            'pypi': {'2024-02-02': ['4.1.2']},
            'internal': {'2024-01-01': ['5.0.0']},
        }
        catalogue = ReleaseCatalogue.from_directories(
            directories, as_of=COLLECTION_DATE,
        )
        result = migrate_record(self.record('r41', '4.1.2'), catalogue)
        self.assertTrue(result.resolved)
        self.assertEqual(Version('4.1.2'), result.version)

    def test_migrate_batch_accepts_raw_releases(self):
        records = [
            self.record('r41', '4.1.2'),
            self.record('r20', '2.0'),
        ]
        releases = [
            ('4.1.2', 'pypi', '2024-02-02'),
            ('2.0.0', 'pypi', '2024-03-01'),
            ('2.0.3', 'pypi', '2024-05-01'),
        ]
        results = migrate(records, releases)
        self.assertEqual(2, len(results))
        self.assertTrue(results[0].resolved)
        self.assertFalse(results[1].resolved)
        self.assertEqual(
            (Version('2.0.0'), Version('2.0.3')),
            results[1].feasible_versions,
        )

    def test_every_exclusion_is_auditable_with_a_known_reason(self):
        result = migrate_record(self.record('r12', '1.2'), self.catalogue)
        self.assertGreater(len(result.excluded), 0)
        for excluded in result.excluded:
            self.assertIn(
                excluded.reason, (REASON_CHANNEL, REASON_CUTOFF),
            )
            self.assertIsInstance(excluded.release, Release)
            self.assertTrue(str(excluded.release.version))
            self.assertTrue(excluded.release.channel)
            self.assertIsNotNone(excluded.release.released_on)


class PublicApiTestCase(unittest.TestCase):

    def test_public_exports(self):
        for name in (
            'Release', 'ReleaseCatalogue', 'LegacyRecord', 'MigrationResult',
            'ExcludedRelease', 'migrate', 'migrate_record',
            'REASON_CHANNEL', 'REASON_CUTOFF',
        ):
            self.assertTrue(hasattr(semantic_version, name), name)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
