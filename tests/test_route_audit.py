# -*- coding: utf-8 -*-

import unittest

from semantic_version import NpmSpec, Version

from route_audit import DeadRoute, Route, RouteSet


class EffectiveConflictTestCase(unittest.TestCase):
    def test_overlap_with_different_contracts_is_reported(self):
        routeset = RouteSet([
            Route('a', '>=1.0.0,<2.0.0', 'contract-a'),
            Route('b', '>=1.5.0,<3.0.0', 'contract-b'),
        ])
        report = routeset.audit()
        self.assertEqual(1, len(report.conflicts))
        conflict = report.conflicts[0]
        self.assertTrue(conflict.effective)
        self.assertEqual('a', conflict.higher.name)
        self.assertEqual('b', conflict.lower.name)
        # The witness is verifiable: both specs actually match it.
        self.assertIn(conflict.version, conflict.higher.spec)
        self.assertIn(conflict.version, conflict.lower.spec)
        self.assertTrue(report.verify())

    def test_disjoint_rules_do_not_conflict(self):
        routeset = RouteSet([
            Route('a', '<1.0.0', 'contract-a'),
            Route('b', '>=1.0.0', 'contract-b'),
        ])
        report = routeset.audit()
        self.assertEqual([], report.conflicts)
        self.assertTrue(report.ok)

    def test_same_contract_overlap_is_not_a_conflict(self):
        routeset = RouteSet([
            Route('a', '>=1.0.0', 'contract-a'),
            Route('b', '>=1.5.0', 'contract-a'),
        ])
        report = routeset.audit()
        self.assertEqual([], report.conflicts)


class ShadowingTestCase(unittest.TestCase):
    def test_shadowed_overlap_is_distinguished(self):
        routeset = RouteSet([
            Route('top', '>=1.0.0', 'contract-top'),
            Route('b', '>=1.0.0,<2.0.0', 'contract-b'),
            Route('c', '>=1.5.0,<2.5.0', 'contract-c'),
        ])
        report = routeset.audit()
        by_pair = {
            (c.higher.name, c.lower.name): c for c in report.conflicts
        }
        # b vs c overlap on [1.5.0, 2.0.0) but 'top' captures all of it.
        bc = by_pair[('b', 'c')]
        self.assertFalse(bc.effective)
        self.assertEqual(['top'], [r.name for r in bc.shadowed_by])
        # top vs b is effective below c's range.
        top_b = by_pair[('top', 'b')]
        self.assertTrue(top_b.effective)
        self.assertNotIn(top_b.version, routeset._route('c').spec)
        self.assertTrue(report.verify())

    def test_fully_shadowed_route_is_dead(self):
        routeset = RouteSet([
            Route('top', '>=1.0.0', 'contract-top'),
            Route('bottom', '>=1.5.0', 'contract-bottom'),
        ])
        report = routeset.audit()
        self.assertEqual(1, len(report.dead_routes))
        dead = report.dead_routes[0]
        self.assertEqual('bottom', dead.route.name)
        self.assertEqual(DeadRoute.KIND_SHADOWED, dead.kind)
        self.assertEqual(['top'], [r.name for r in dead.shadowed_by])
        self.assertTrue(report.verify())

    def test_empty_rule_is_dead(self):
        routeset = RouteSet([
            Route('broken', '>=2.0.0,<1.0.0', 'contract-x'),
        ])
        report = routeset.audit()
        self.assertEqual(1, len(report.dead_routes))
        self.assertEqual(DeadRoute.KIND_EMPTY, report.dead_routes[0].kind)


class TripleIntersectionTestCase(unittest.TestCase):
    def setUp(self):
        self.routeset = RouteSet([
            Route('a', '>=1.0.0,<2.0.0', 'contract-a'),
            Route('b', '>=2.0.0,<3.0.0', 'contract-b'),
            Route('c', '>=1.5.0,<2.5.0', 'contract-c'),
        ])

    def test_pairwise_overlap_does_not_imply_triple(self):
        # a and b each overlap c...
        self.assertIsNotNone(self.routeset.common_witness('a', 'c'))
        self.assertIsNotNone(self.routeset.common_witness('b', 'c'))
        # ...but a and b never meet, so all three never hit together.
        self.assertIsNone(self.routeset.common_witness('a', 'b'))
        self.assertIsNone(self.routeset.common_witness('a', 'b', 'c'))

    def test_audit_reports_real_intersections_only(self):
        report = self.routeset.audit()
        pairs = {(c.higher.name, c.lower.name) for c in report.conflicts}
        self.assertEqual({('a', 'c'), ('b', 'c')}, pairs)
        for conflict in report.conflicts:
            self.assertTrue(conflict.effective)
        self.assertTrue(report.verify())

    def test_each_pair_conflict_replays_to_its_own_region(self):
        report = self.routeset.audit()
        replays = {
            (c.higher.name, c.lower.name): self.routeset.replay(c.version)
            for c in report.conflicts
        }
        ac = replays[('a', 'c')]
        self.assertEqual('a', ac.winner.name)
        self.assertEqual(['a', 'c'], [r.name for r in ac.matched])
        bc = replays[('b', 'c')]
        self.assertEqual('b', bc.winner.name)
        self.assertEqual(['b', 'c'], [r.name for r in bc.matched])


class ReplayTestCase(unittest.TestCase):
    def test_replay_from_conflict_version_shows_the_contending_pair(self):
        routeset = RouteSet([
            Route('legacy', '<1.0.0', 'contract-legacy'),
            Route('stable', '>=1.0.0,<2.0.0', 'contract-a'),
            Route('lts', '>=1.5.0,<2.5.0', 'contract-b'),
        ])
        report = routeset.audit()
        self.assertEqual(1, len(report.conflicts))
        conflict = report.conflicts[0]
        replay = routeset.replay(conflict.version)
        self.assertEqual('stable', replay.winner.name)
        self.assertEqual(
            ('stable', 'lts'),
            (replay.dispute[0].name, replay.dispute[1].name))
        self.assertIn('stable', replay.format())
        self.assertIn('dispute', replay.format())

    def test_replay_unrouted_version(self):
        routeset = RouteSet([Route('a', '>=1.0.0,<1.5.0', 'contract-a')])
        replay = routeset.replay('2.0.0')
        self.assertIsNone(replay.winner)
        self.assertIsNone(replay.dispute)

    def test_resolve_picks_highest_priority(self):
        routeset = RouteSet([
            Route('a', '>=1.0.0', 'contract-a'),
            Route('b', '>=1.5.0', 'contract-b'),
        ])
        self.assertEqual('a', routeset.resolve('1.7.0').name)
        self.assertIsNone(routeset.resolve('0.9.0'))


class SpecSyntaxTestCase(unittest.TestCase):
    def test_npm_specs_can_be_mixed_in(self):
        routeset = RouteSet([
            Route('npm', NpmSpec('^1.2.0'), 'contract-npm'),
            Route('simple', '>=1.2.3,<2.0.0', 'contract-simple'),
        ])
        report = routeset.audit()
        self.assertEqual(1, len(report.conflicts))
        conflict = report.conflicts[0]
        self.assertTrue(conflict.effective)
        self.assertIn(conflict.version, conflict.higher.spec)
        self.assertIn(conflict.version, conflict.lower.spec)
        self.assertTrue(report.verify())

    def test_duplicate_names_rejected(self):
        self.assertRaises(ValueError, RouteSet, [
            Route('a', '>=1.0.0', 'x'),
            Route('a', '>=2.0.0', 'y'),
        ])


if __name__ == '__main__':
    unittest.main()
