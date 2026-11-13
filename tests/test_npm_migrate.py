#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tests for the npm -> restricted SimpleSpec migration tool.

Focus areas (per migration requirements): zero-major compatibility ranges
and prerelease versions, not just common final releases.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import npm_migrate
from semantic_version import NpmSpec, SimpleSpec, Version


class TranslationTests(unittest.TestCase):
    """The translator must mirror NpmSpec's expansion rules exactly."""

    def test_caret_zero_major(self):
        examples = {
            '^1.2.3': '>=1.2.3,<2.0.0',
            '^0.2.3': '>=0.2.3,<0.3.0',
            '^0.0.3': '>=0.0.3,<0.0.4',
            '^0.0.0': '>=0.0.0,<0.0.1',
            '^0.x': '>=0.0.0,<1.0.0',
            '^0.2.x': '>=0.2.0,<0.3.0',
            '^1.x': '>=1.0.0,<2.0.0',
        }
        for npm, simple in examples.items():
            with self.subTest(npm=npm):
                self.assertEqual(simple, npm_migrate.translate_npm(npm))

    def test_tilde_zero_major(self):
        examples = {
            '~1.2.3': '>=1.2.3,<1.3.0',
            '~0.2.3': '>=0.2.3,<0.3.0',
            '~0.0.3': '>=0.0.3,<0.1.0',
            '~1': '>=1.0.0,<2.0.0',
        }
        for npm, simple in examples.items():
            with self.subTest(npm=npm):
                self.assertEqual(simple, npm_migrate.translate_npm(npm))

    def test_pins_wildcards_hyphen(self):
        examples = {
            '=1.4.2': '==1.4.2',
            '=2.0.0+build42': '==2.0.0+build42',
            '1.2.x': '>=1.2.0,<1.3.0',
            '*': '>=0.0.0',
            '1.2.3 - 2.3.4': '>=1.2.3,<=2.3.4',
            '1.2.3 - 2.3': '>=1.2.3,<2.4.0',
            '<=1.2.3': '<=1.2.3',
            '>1.2.3': '>1.2.3',
        }
        for npm, simple in examples.items():
            with self.subTest(npm=npm):
                self.assertEqual(simple, npm_migrate.translate_npm(npm))

    def test_prerelease_targets_are_kept(self):
        examples = {
            '>=1.2.3-alpha <2.0.0': '>=1.2.3-alpha,<2.0.0',
            '^1.2.3-rc.1': '>=1.2.3-rc.1,<2.0.0',
            '<1.2.3-alpha': '<1.2.3-alpha',
        }
        for npm, simple in examples.items():
            with self.subTest(npm=npm):
                self.assertEqual(simple, npm_migrate.translate_npm(npm))

    def test_whitelist_enforced(self):
        for npm in ['^0.2.3', '~1.2.3', '*', '1.2.3 - 2.3.4', '=1.4.2']:
            with self.subTest(npm=npm):
                self.assertTrue(
                    npm_migrate.check_whitelist(npm_migrate.translate_npm(npm)))

    def test_union_is_untranslatable(self):
        with self.assertRaises(npm_migrate.Untranslatable):
            npm_migrate.translate_npm('^1.2.3 || ^2.0.0')

    def test_convex_hull(self):
        self.assertEqual('>=1.2.3,<3.0.0', npm_migrate.convex_hull('^1.2.3 || ^2.0.0'))


class AnalysisTests(unittest.TestCase):
    """End-to-end status and witness checks on the generated universe."""

    def analyze(self, expression, manifest=None):
        universe = npm_migrate.build_universe([expression], manifest)
        return npm_migrate.analyze(expression, manifest, universe)

    def test_exact_pin(self):
        result = self.analyze('=1.4.2')
        self.assertEqual(npm_migrate.STATUS_EXACT, result['status'])
        self.assertEqual('==1.4.2', result['simple'])

    def test_exact_zero_major_patch_locked_caret(self):
        # ^0.0.3 -> >=0.0.3,<0.0.4: no prerelease can sit strictly between
        # 0.0.3 and 0.0.4, so the translation is globally equivalent.
        result = self.analyze('^0.0.3')
        self.assertEqual(npm_migrate.STATUS_EXACT, result['status'])

    def test_zero_major_caret_matches_releases(self):
        result = self.analyze('^0.2.3')
        simple = SimpleSpec(result['simple'])
        for version in ['0.2.3', '0.2.9']:
            self.assertIn(Version(version), simple)
        for version in ['0.2.2', '0.3.0']:
            self.assertNotIn(Version(version), simple)

    def test_prerelease_witnesses_for_range(self):
        # Without a manifest, ^1.2.3 is divergent: the SimpleSpec candidate
        # accepts other-patch prereleases that npm's same-patch policy rejects.
        result = self.analyze('^1.2.3')
        self.assertEqual(npm_migrate.STATUS_DIVERGENT, result['status'])
        self.assertEqual([], result['witnesses']['npm_only'])
        self.assertIn('1.2.4-alpha', result['witnesses']['simple_only'])

    def test_npm_lt_quirk_witness(self):
        # This library's NpmSpec('<2.0.0') accepts 2.0.0-alpha (same-patch
        # policy); the SimpleSpec candidate rejects it (natural policy).
        result = self.analyze('<2.0.0')
        self.assertEqual(npm_migrate.STATUS_DIVERGENT, result['status'])
        self.assertIn('2.0.0-alpha', result['witnesses']['npm_only'])

    def test_manifest_downgrade(self):
        manifest = ['0.2.3', '0.2.4', '0.3.0', '1.2.3', '1.2.4', '1.3.0']
        result = self.analyze('^0.2.3', manifest)
        self.assertEqual(npm_migrate.STATUS_MANIFEST_ONLY, result['status'])
        self.assertEqual('manifest', result['scope'])
        # Global witnesses must still be reported alongside the downgrade.
        self.assertTrue(result['witnesses']['simple_only'])

    def test_manifest_divergence_is_caught(self):
        # A prerelease artifact in the frozen manifest breaks the equivalence:
        # the downgrade is verified per condition, not assumed.
        manifest = ['1.2.3', '1.5.0-alpha', '2.0.0']
        result = self.analyze('^1.2.3', manifest)
        self.assertEqual(npm_migrate.STATUS_DIVERGENT, result['status'])
        self.assertIn('1.5.0-alpha', result['manifest_witnesses']['simple_only'])

    def test_union_not_expressible(self):
        result = self.analyze('^1.2.3 || ^2.0.0')
        self.assertEqual(npm_migrate.STATUS_NOT_EXPRESSIBLE, result['status'])
        self.assertIsNone(result['simple'])
        self.assertEqual('>=1.2.3,<3.0.0', result['hull'])
        # The hull over-approximates: it accepts prereleases npm rejects.
        self.assertTrue(result['witnesses']['simple_only'])

    def test_universe_covers_zero_major_and_prereleases(self):
        universe = npm_migrate.build_universe(['^0.2.3'])
        self.assertIn(Version('0.0.1'), universe)
        self.assertIn(Version('0.2.4-alpha'), universe)
        self.assertIn(Version('0.3.0-rc.1'), universe)


if __name__ == '__main__':
    unittest.main()
