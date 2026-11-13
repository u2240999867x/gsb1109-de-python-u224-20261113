#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Tests for the artifact index (semantic_version.index)."""

import unittest

from semantic_version import base
from semantic_version import index as pkg_index


Version = base.Version
SimpleSpec = base.SimpleSpec
ArtifactRecord = pkg_index.ArtifactRecord
PackageIndex = pkg_index.PackageIndex
PackageRelease = pkg_index.PackageRelease
Selection = pkg_index.Selection


class ArtifactRecordTestCase(unittest.TestCase):

    def test_coercion_and_fields(self):
        record = ArtifactRecord('2.4.0+cn', 'sha256:aaa', 'mirror-cn')
        self.assertEqual(Version('2.4.0+cn'), record.version)
        self.assertEqual('sha256:aaa', record.digest)
        self.assertEqual('mirror-cn', record.source)

    def test_optional_source(self):
        record = ArtifactRecord(Version('2.4.0'), 'sha256:aaa')
        self.assertIsNone(record.source)

    def test_invalid_digest(self):
        with self.assertRaises(ValueError):
            ArtifactRecord('2.4.0', '')
        with self.assertRaises(ValueError):
            ArtifactRecord('2.4.0', b'raw-bytes')

    def test_invalid_source(self):
        with self.assertRaises(TypeError):
            ArtifactRecord('2.4.0', 'sha256:aaa', source=42)

    def test_equality_hash_repr(self):
        first = ArtifactRecord('2.4.0+cn', 'd1', 'm1')
        second = ArtifactRecord('2.4.0+cn', 'd1', 'm1')
        other = ArtifactRecord('2.4.0+cn', 'd1', 'm2')
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertNotEqual(first, other)
        self.assertEqual(
            "ArtifactRecord('2.4.0+cn', 'd1', 'm1')", repr(first))

    def test_comparison_with_other_types(self):
        record = ArtifactRecord('2.4.0+cn', 'd1', 'm1')
        self.assertNotEqual(record, ('2.4.0+cn', 'd1'))
        self.assertFalse(record == '2.4.0+cn')


class PackageReleaseTestCase(unittest.TestCase):

    def test_single_digest_merges_sources(self):
        release = PackageRelease('2.4.0+cn')
        release._add('d1', 'mirror-a')
        release._add('d1', 'mirror-b')
        # Same digest seen on several mirrors: one digest, merged sources.
        self.assertEqual(frozenset(['d1']), release.digests)
        self.assertEqual(frozenset(['mirror-a', 'mirror-b']), release.sources)
        self.assertEqual(
            frozenset(['mirror-a', 'mirror-b']),
            release.sources_for('d1'))
        self.assertFalse(release.has_divergent_digests)

    def test_two_digests_remain_visible(self):
        release = PackageRelease('2.4.0')
        release._add('d1', 'mirror-cn')
        release._add('d2', 'mirror-eu')
        self.assertEqual(frozenset(['d1', 'd2']), release.digests)
        self.assertTrue(release.has_divergent_digests)
        self.assertEqual(frozenset(['mirror-cn']), release.sources_for('d1'))
        self.assertEqual(frozenset(['mirror-eu']), release.sources_for('d2'))
        self.assertEqual(frozenset(), release.sources_for('unknown'))

    def test_same_digest_source_and_sourceless_observations(self):
        release = PackageRelease('2.4.0')
        release._add('d1')
        release._add('d1', 'mirror-a')
        self.assertEqual(frozenset(['d1']), release.digests)
        self.assertEqual(frozenset(['mirror-a']), release.sources)

    def test_artifacts_are_deterministic(self):
        release = PackageRelease('2.4.0')
        release._add('d2', 'mirror-b')
        release._add('d1', 'mirror-a')
        release._add('d1', 'mirror-b')
        self.assertEqual(
            (
                ArtifactRecord('2.4.0', 'd1', 'mirror-a'),
                ArtifactRecord('2.4.0', 'd1', 'mirror-b'),
                ArtifactRecord('2.4.0', 'd2', 'mirror-b'),
            ),
            release.artifacts)

    def test_equality_hash(self):
        left = PackageRelease('2.4.0', {'d1': ['m1']})
        right = PackageRelease('2.4.0', {'d1': ['m1']})
        diverging = PackageRelease('2.4.0', {'d1': ['m1'], 'd2': ['m2']})
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))
        self.assertNotEqual(left, diverging)

    def test_constructor_rejects_invalid_digest(self):
        with self.assertRaises(ValueError):
            PackageRelease('2.4.0', {'': ['m1']})

    def test_sourceless_digest_artifacts(self):
        release = PackageRelease('2.4.0')
        release._add('d1')
        self.assertEqual(
            (ArtifactRecord('2.4.0', 'd1'),), release.artifacts)

    def test_comparison_with_other_types(self):
        release = PackageRelease('2.4.0', {'d1': ['m1']})
        self.assertNotEqual(release, '2.4.0')
        self.assertFalse(release == object())
        self.assertNotEqual(release, object())


class PackageIndexTestCase(unittest.TestCase):

    def test_add_and_get(self):
        idx = PackageIndex()
        idx.add('2.4.0+cn', 'd1', 'mirror-cn')
        release = idx.get('2.4.0+cn')
        self.assertIsInstance(release, PackageRelease)
        self.assertEqual(Version('2.4.0+cn'), release.version)
        self.assertEqual(frozenset(['d1']), release.digests)
        self.assertIn(Version('2.4.0+cn'), idx)
        self.assertIn('2.4.0+cn', idx)
        self.assertEqual(1, len(idx))
        self.assertIsNone(idx.get('9.9.9'))

    def test_same_digest_from_mirrors_merges_sources(self):
        idx = PackageIndex([
            ArtifactRecord('2.4.0+cn', 'd1', 'mirror-a'),
            ArtifactRecord('2.4.0+cn', 'd1', 'mirror-b'),
        ])
        self.assertEqual(1, len(idx))
        release = idx.get('2.4.0+cn')
        self.assertEqual(frozenset(['d1']), release.digests)
        self.assertEqual(frozenset(['mirror-a', 'mirror-b']), release.sources)

    def test_same_version_two_digests_both_visible(self):
        idx = PackageIndex()
        idx.add_record(ArtifactRecord('2.4.0', 'd1', 'mirror-cn'))
        idx.add_record(ArtifactRecord('2.4.0', 'd2', 'mirror-eu'))
        self.assertEqual(1, len(idx))
        release = idx.get('2.4.0')
        self.assertTrue(release.has_divergent_digests)
        self.assertEqual(frozenset(['d1', 'd2']), release.digests)

    def test_add_returns_index_for_chaining(self):
        idx = PackageIndex()
        self.assertIs(idx, idx.add('1.0.0', 'd1'))
        self.assertIs(
            idx, idx.add_record(ArtifactRecord('1.0.1', 'd2')))

    def test_add_rejects_non_records(self):
        with self.assertRaises(TypeError):
            PackageIndex().add_record(('2.4.0', 'd1'))

    def test_invalid_version_argument(self):
        idx = PackageIndex()
        with self.assertRaises(TypeError):
            idx.add(42, 'd1')
        with self.assertRaises(TypeError):
            idx.get(42)
        with self.assertRaises(TypeError):
            _ = 42 in idx

    def test_repr_lists_releases(self):
        idx = PackageIndex([ArtifactRecord('2.4.0', 'd1', 'mirror-a')])
        self.assertIn('PackageIndex(', repr(idx))
        self.assertIn('2.4.0', repr(idx))
        self.assertEqual(
            [release.version for release in idx],
            [release.version for release in idx.releases])

    def test_releases_follow_version_precedence(self):
        idx = PackageIndex([
            ArtifactRecord('2.4.0', 'd0'),
            ArtifactRecord('1.8.2', 'd1'),
            ArtifactRecord('2.4.1-alpha', 'd2'),
            ArtifactRecord('2.4.0-rc1', 'd3'),
        ])
        versions = [release.version for release in idx.releases]
        self.assertEqual(
            [
                Version('1.8.2'),
                Version('2.4.0-rc1'),
                Version('2.4.0'),
                Version('2.4.1-alpha'),
            ],
            versions)

    def test_build_variants_do_not_rank_each_other(self):
        idx = PackageIndex([
            ArtifactRecord('2.4.0+eu', 'd2'),
            ArtifactRecord('2.4.0+cn', 'd1'),
        ])
        versions = [str(release.version) for release in idx.releases]
        self.assertEqual(sorted(versions), versions)
        for first, second in zip(versions, versions[1:]):
            self.assertLessEqual(
                Version(first).precedence_key,
                Version(second).precedence_key)

    def test_insertion_order_does_not_affect_index(self):
        records = [
            ArtifactRecord('2.4.0+cn', 'd1', 'mirror-a'),
            ArtifactRecord('2.4.0+cn', 'd1', 'mirror-b'),
            ArtifactRecord('2.4.0+eu', 'd2', 'mirror-c'),
            ArtifactRecord('1.0.0', 'd0', 'mirror-a'),
        ]
        forward = PackageIndex(records)
        backward = PackageIndex(reversed(records))
        self.assertEqual(
            sorted(forward, key=lambda r: str(r.version)),
            sorted(backward, key=lambda r: str(r.version)))
        self.assertEqual(
            forward.get('2.4.0+cn'), backward.get('2.4.0+cn'))


class SelectionTestCase(unittest.TestCase):

    def build_index(self, order='forward'):
        records = [
            ArtifactRecord('2.3.0', 'd-old', 'mirror-a'),
            ArtifactRecord('2.4.0+cn', 'd-cn', 'mirror-cn'),
            ArtifactRecord('2.4.0+eu', 'd-eu', 'mirror-eu'),
        ]
        if order == 'backward':
            records = list(reversed(records))
        return PackageIndex(records)

    def test_no_compatible_release(self):
        selection = self.build_index().select('>=3.0.0')
        self.assertFalse(selection)
        self.assertEqual(0, len(selection))
        self.assertIsNone(selection.chosen)
        self.assertFalse(selection.is_tie)
        self.assertEqual(frozenset(), selection.digests)
        self.assertEqual((), tuple(selection))

    def test_unique_winner(self):
        idx = PackageIndex([
            ArtifactRecord('2.4.0+cn', 'd-cn'),
            ArtifactRecord('2.5.0', 'd-new'),
        ])
        selection = idx.select(SimpleSpec('>=2.0.0'))
        self.assertTrue(selection)
        self.assertFalse(selection.is_tie)
        self.assertEqual(Version('2.5.0'), selection.chosen.version)
        self.assertEqual(frozenset(['d-new']), selection.digests)
        self.assertFalse(selection.content_differs)

    def test_tie_returns_all_candidates(self):
        selection = self.build_index().select('>=2.0.0')
        self.assertTrue(selection.is_tie)
        self.assertIsNone(selection.chosen)
        versions = [release.version for release in selection.candidates]
        self.assertEqual(
            [Version('2.4.0+cn'), Version('2.4.0+eu')], versions)
        # The older, lower-precedence release is not a candidate.
        self.assertNotIn(Version('2.3.0'), versions)

    def test_tie_surfaces_content_differences(self):
        selection = self.build_index().select('>=2.0.0')
        self.assertTrue(selection.content_differs)
        self.assertEqual(frozenset(['d-cn', 'd-eu']), selection.digests)

    def test_tie_with_identical_content_does_not_differ(self):
        # Same digest mirrored under two build variants: still a precedence
        # tie, but the underlying content is identical.
        idx = PackageIndex([
            ArtifactRecord('2.4.0+cn', 'd-shared', 'mirror-cn'),
            ArtifactRecord('2.4.0+eu', 'd-shared', 'mirror-eu'),
        ])
        selection = idx.select('*')
        self.assertTrue(selection.is_tie)
        self.assertIsNone(selection.chosen)
        self.assertFalse(selection.content_differs)
        self.assertEqual(frozenset(['d-shared']), selection.digests)

    def test_divergent_digest_inside_one_candidate_is_visible(self):
        idx = PackageIndex([
            ArtifactRecord('2.4.0+cn', 'd1', 'mirror-cn'),
            ArtifactRecord('2.4.0+cn', 'd2', 'mirror-bad'),
            ArtifactRecord('2.4.0+eu', 'd3', 'mirror-eu'),
        ])
        selection = idx.select('*')
        self.assertTrue(selection.is_tie)
        self.assertEqual(frozenset(['d1', 'd2', 'd3']), selection.digests)
        self.assertTrue(selection.content_differs)
        cn = next(
            release for release in selection.candidates
            if release.version == Version('2.4.0+cn'))
        self.assertTrue(cn.has_divergent_digests)

    def test_tie_never_broken_by_download_order(self):
        forward = self.build_index('forward').select('>=2.0.0')
        backward = self.build_index('backward').select('>=2.0.0')
        self.assertEqual(forward, backward)
        self.assertEqual(
            [Version('2.4.0+cn'), Version('2.4.0+eu')],
            [release.version for release in backward.candidates])

    def test_spec_filters_before_selection(self):
        idx = self.build_index()
        selection = idx.select('<2.4.0')
        self.assertFalse(selection.is_tie)
        self.assertEqual(Version('2.3.0'), selection.chosen.version)

    def test_accepts_spec_instance(self):
        selection = self.build_index().select(SimpleSpec('>=2.4.0'))
        self.assertTrue(selection.is_tie)

    def test_invalid_spec_argument(self):
        with self.assertRaises(TypeError):
            self.build_index().select(42)

    def test_selection_equality_and_repr(self):
        selection = self.build_index().select('*')
        same = self.build_index().select('*')
        self.assertEqual(selection, same)
        self.assertEqual(hash(selection), hash(same))
        self.assertIn('Selection(', repr(selection))

    def test_comparison_with_other_types(self):
        selection = self.build_index().select('*')
        self.assertNotEqual(selection, ('2.4.0+cn', '2.4.0+eu'))
        self.assertFalse(selection == object())
        self.assertNotEqual(selection, object())
        self.assertNotEqual(selection, self.build_index().select('>=3.0.0'))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
