#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Test the upgrade-path planner."""

import json
import unittest

from semantic_version import upgrade


def _drops(transformation):
    return transformation.drops_fields


class PlanUpgradeTests(unittest.TestCase):

    def test_direct_cheapest(self):
        steps = [
            upgrade.Transformation('slow', '1.0.0', '1.2.0', 9),
            upgrade.Transformation('fast', '1.0.0', '1.2.0', 2),
        ]
        path = upgrade.plan_upgrade('1.0.0', '1.2.0', steps)
        self.assertEqual(['fast'], [step.name for step in path.steps])
        self.assertEqual(2, path.cost)

    def test_multi_hop_beats_expensive_direct(self):
        steps = [
            upgrade.Transformation('direct', '1.0.0', '2.0.0', 10),
            upgrade.Transformation('a', '1.0.0', '1.1.0', 1),
            upgrade.Transformation('b', '1.1.0', '2.0.0', 1),
        ]
        path = upgrade.plan_upgrade('1.0.0', '2.0.0', steps)
        self.assertEqual(['a', 'b'], [step.name for step in path.steps])
        self.assertEqual(2, path.cost)
        self.assertEqual(
            ['1.0.0', '1.1.0', '2.0.0'],
            [str(version) for version in path.versions])

    def test_cost_is_summed(self):
        steps = [
            upgrade.Transformation('a', '1.0.0', '1.1.0', 0),
            upgrade.Transformation('b', '1.1.0', '1.2.0', 3),
            upgrade.Transformation('c', '1.2.0', '1.3.0', 4),
        ]
        path = upgrade.plan_upgrade('1.0.0', '1.3.0', steps)
        self.assertEqual(7, path.cost)

    def test_no_transformation_needed(self):
        path = upgrade.plan_upgrade(
            '1.0.0', '1.0.0',
            [upgrade.Transformation('never', '0.1.0', '0.2.0', 1)])
        self.assertEqual((), path.steps)
        self.assertEqual(0, path.cost)

    def test_state_distinguishes_same_version_by_fields(self):
        # Both routes land on 1.1.0.  The cheaper one drops "owner"; only the
        # pricier one keeps it, and "owner" is required downstream.
        steps = [
            upgrade.Transformation('cheap', '1.0.0', '1.1.0', 1,
                                   drops_fields=['owner']),
            upgrade.Transformation('safe', '1.0.0', '1.1.0', 5),
            upgrade.Transformation('final', '1.1.0', '1.2.0', 1),
        ]
        path = upgrade.plan_upgrade(
            '1.0.0', '1.2.0', steps, required_fields=['owner'])
        self.assertEqual(['safe', 'final'],
                         [step.name for step in path.steps])
        self.assertEqual(6, path.cost)

    def test_cheaper_state_survives_only_when_it_keeps_required_fields(self):
        # The cheap branch loses nothing, so it wins over the costly branch.
        steps = [
            upgrade.Transformation('cheap', '1.0.0', '1.1.0', 1),
            upgrade.Transformation('safe', '1.0.0', '1.1.0', 5),
            upgrade.Transformation('final', '1.1.0', '1.2.0', 1),
        ]
        path = upgrade.plan_upgrade(
            '1.0.0', '1.2.0', steps, required_fields=['owner'])
        self.assertEqual(['cheap', 'final'],
                         [step.name for step in path.steps])

    def test_only_path_loses_required_field(self):
        steps = [
            upgrade.Transformation('lossy', '1.0.0', '1.1.0', 1,
                                   drops_fields=['owner']),
        ]
        with self.assertRaises(upgrade.RequiredFieldLostError):
            upgrade.plan_upgrade(
                '1.0.0', '1.1.0', steps, required_fields=['owner'])

    def test_version_gap(self):
        steps = [
            upgrade.Transformation('a', '1.0.0', '1.1.0', 1),
        ]
        with self.assertRaises(upgrade.VersionGapError):
            upgrade.plan_upgrade('1.0.0', '2.0.0', steps)

    def test_version_gap_with_unrelated_islands(self):
        # 1.1.0 exists, but nothing accepts it, so 2.0.0 stays unreachable.
        steps = [
            upgrade.Transformation('a', '1.0.0', '1.1.0', 1),
            upgrade.Transformation('b', '9.0.0', '2.0.0', 1),
        ]
        with self.assertRaises(upgrade.VersionGapError):
            upgrade.plan_upgrade('1.0.0', '2.0.0', steps)

    def test_required_field_already_lost_in_source(self):
        steps = [
            upgrade.Transformation('a', '1.0.0', '1.1.0', 1),
        ]
        with self.assertRaises(upgrade.RequiredFieldLostError):
            upgrade.plan_upgrade(
                '1.0.0', '1.1.0', steps, required_fields=['owner'],
                source_fields=['payload'])

    def test_source_fields_allow_optional_retention(self):
        steps = [
            upgrade.Transformation('a', '1.0.0', '1.1.0', 1),
        ]
        path = upgrade.plan_upgrade(
            '1.0.0', '1.1.0', steps, required_fields=[],
            source_fields=['payload'])
        self.assertEqual(['a'], [step.name for step in path.steps])

    def test_versions_with_prerelease_and_build(self):
        steps = [
            upgrade.Transformation(
                'pre', '1.0.0-rc.1', '1.0.0', 1),
            upgrade.Transformation(
                'build', '1.0.0', '1.0.1+build.7', 1),
        ]
        path = upgrade.plan_upgrade(
            '1.0.0-rc.1', '1.0.1+build.7', steps)
        self.assertEqual(2, len(path.steps))

    def test_range_inputs(self):
        steps = [
            upgrade.Transformation(
                'patch', output_version='1.0.5', cost=1, accepts='>=1.0.0,<1.1.0'),
            upgrade.Transformation('late', '1.0.5', '1.1.0', 1),
        ]
        path = upgrade.plan_upgrade('1.0.3', '1.1.0', steps)
        self.assertEqual(['patch', 'late'],
                         [step.name for step in path.steps])

    def test_range_does_not_accept_start(self):
        steps = [
            upgrade.Transformation(
                'patch', output_version='1.1.0', cost=1, accepts='>=1.0.0,<1.1.0'),
        ]
        with self.assertRaises(upgrade.VersionGapError):
            upgrade.plan_upgrade('0.9.0', '1.1.0', steps)

    def test_negative_cost_rejected(self):
        with self.assertRaises(ValueError):
            upgrade.Transformation('bad', '1.0.0', '1.1.0', -1)

    def test_missing_inputs_rejected(self):
        with self.assertRaises(ValueError):
            upgrade.Transformation('bad', [], '1.1.0', 1)

    def test_invalid_version_rejected(self):
        with self.assertRaises(ValueError):
            upgrade.plan_upgrade('not-a-version', '1.1.0', [])

    def test_equally_cheap_paths_are_deterministic(self):
        steps = [
            upgrade.Transformation('a', '1.0.0', '1.1.0', 1),
            upgrade.Transformation('b', '1.0.0', '1.1.0', 1),
            upgrade.Transformation('final', '1.1.0', '1.2.0', 1),
        ]
        names = []
        for _ in range(5):
            path = upgrade.plan_upgrade('1.0.0', '1.2.0', steps)
            names.append([step.name for step in path.steps])
        self.assertEqual(1, len(set(tuple(name) for name in names)))

    def test_as_dict_round_trips_through_json(self):
        path = upgrade.plan_upgrade(
            '1.0.0', '1.2.0',
            [upgrade.Transformation('a', '1.0.0', '1.2.0', 2,
                                    drops_fields=['legacy'])])
        data = json.loads(json.dumps(path.as_dict()))
        self.assertEqual('ok', data['status'])
        self.assertEqual(2, data['cost'])
        self.assertEqual(['legacy'], data['steps'][0]['drops_fields'])


class TransformationFromDictTests(unittest.TestCase):

    def test_aliases(self):
        step = upgrade.Transformation.from_dict({
            'id': 'rename',
            'inputs': ['1.0.0'],
            'produces': '1.1.0',
            'weight': 4,
            'lost_fields': ['owner'],
        })
        self.assertEqual('rename', step.name)
        self.assertEqual(4, step.cost)
        self.assertEqual(frozenset(['owner']), _drops(step))

    def test_field_name_aliases(self):
        step = upgrade.Transformation.from_dict({
            'name': 'rename',
            'input_versions': ['1.0.0'],
            'output_version': '1.1.0',
            'drops': ['owner'],
            'cost': 2,
        })
        self.assertEqual(frozenset(['owner']), _drops(step))

    def test_canonical_keys(self):
        step = upgrade.Transformation.from_dict({
            'name': 'rename',
            'input_versions': ['1.0.0'],
            'output_version': '1.1.0',
            'cost': 4,
            'drops_fields': ['owner'],
        })
        self.assertEqual(['1.0.0'],
                         [str(version) for version in step.input_versions])


class CliTests(unittest.TestCase):

    def setUp(self):
        import shutil
        import tempfile
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tempdir, True)

    def run_cli(self, document):
        import io
        import os
        from semantic_version import _upgrade
        input_path = os.path.join(self.tempdir, 'input.json')
        output_path = os.path.join(self.tempdir, 'output.json')
        with io.open(input_path, 'w', encoding='utf-8') as handle:
            json.dump(document, handle)
        status = _upgrade.main([input_path, '--output', output_path])
        with io.open(output_path, 'r', encoding='utf-8') as handle:
            return status, json.load(handle)

    def test_success(self):
        status, result = self.run_cli({
            'start_version': '1.0.0',
            'target_version': '1.2.0',
            'required_fields': ['owner'],
            'transformations': [
                {'name': 'a', 'input_versions': ['1.0.0'],
                 'output_version': '1.1.0', 'cost': 1,
                 'drops_fields': ['legacy']},
                {'name': 'b', 'input_versions': ['1.1.0'],
                 'output_version': '1.2.0', 'cost': 2},
            ],
        })
        self.assertEqual(0, status)
        self.assertEqual('ok', result['status'])
        self.assertEqual(3, result['cost'])
        self.assertEqual(['a', 'b'], [step['name'] for step in result['steps']])

    def test_version_gap(self):
        status, result = self.run_cli({
            'start_version': '1.0.0',
            'target_version': '2.0.0',
            'transformations': [
                {'name': 'a', 'input_versions': ['1.0.0'],
                 'output_version': '1.1.0', 'cost': 1},
            ],
        })
        self.assertEqual(2, status)
        self.assertEqual('no_path', result['status'])
        self.assertEqual('version_gap', result['reason'])

    def test_required_field_lost(self):
        status, result = self.run_cli({
            'start_version': '1.0.0',
            'target_version': '1.1.0',
            'required_fields': ['owner'],
            'transformations': [
                {'name': 'lossy', 'input_versions': ['1.0.0'],
                 'output_version': '1.1.0', 'cost': 1,
                 'drops_fields': ['owner']},
            ],
        })
        self.assertEqual(2, status)
        self.assertEqual('no_path', result['status'])
        self.assertEqual('required_field_lost', result['reason'])
        self.assertEqual(['owner'], result['fields'])

    def test_missing_input_file(self):
        import os
        from semantic_version import _upgrade
        output_path = os.path.join(self.tempdir, 'output.json')
        status = _upgrade.main(['does-not-exist.json',
                                '--output', output_path])
        self.assertEqual(1, status)
        import io
        with io.open(output_path, 'r', encoding='utf-8') as handle:
            result = json.load(handle)
        self.assertEqual('invalid_input', result['reason'])


if __name__ == '__main__':
    unittest.main()
