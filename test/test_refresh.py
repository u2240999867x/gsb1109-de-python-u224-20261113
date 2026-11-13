import unittest

from mwclient.refresh import (
    CrawlStore,
    DependencyRecord,
    MemoryCrawlStore,
    ReferenceGraph,
    TemplateRevision,
    plan_refresh,
    settle_crawl,
)


class FakeGraph(ReferenceGraph):
    def __init__(self, embedded, templates, revisions):
        self._embedded = embedded
        self._templates = set(templates)
        self._revisions = dict(revisions)

    def embedded_in(self, title):
        return tuple(self._embedded.get(title, ()))

    def is_template(self, title):
        return title in self._templates

    def current_revision(self, title):
        return self._revisions[title]


def build_graph():
    # T0 is the edited price template.
    # T1 transcludes T0; T2 transcludes T1 (two-layer indirect references).
    # Direct and indirect entry pages transclude the templates.
    embedded = {
        'T0': ('T1', 'DirectPage', 'ConditionalPage', 'FreshPage'),
        'T1': ('T2', 'IndirectPage', 'IncompletePage'),
        'T2': ('DeepPage',),
    }
    revisions = {'T0': 10, 'T1': 5, 'T2': 7}
    return FakeGraph(
        embedded,
        templates=('T0', 'T1', 'T2'),
        revisions=revisions,
    )


def seed_store():
    store = MemoryCrawlStore()
    store.record_crawl('DirectPage', 100, [
        DependencyRecord('T0', revision_id=9, expanded=True),
    ])
    store.record_crawl('IndirectPage', 200, [
        DependencyRecord('T1', revision_id=5, expanded=True),
        DependencyRecord('T0', revision_id=9, expanded=True),
    ])
    store.record_crawl('DeepPage', 300, [
        DependencyRecord('T2', revision_id=7, expanded=True),
        DependencyRecord('T1', revision_id=5, expanded=True),
        DependencyRecord('T0', revision_id=9, expanded=True),
    ])
    # The parameter-controlled branch never expanded at crawl time and the
    # record set is complete.
    store.record_crawl('ConditionalPage', 400, [
        DependencyRecord('T0', revision_id=None, expanded=False),
    ])
    # The crawl did not record the revision of the expanded T0 branch.
    store.record_crawl('IncompletePage', 500, [
        DependencyRecord('T1', revision_id=5, expanded=True),
        DependencyRecord('T0', revision_id=None, expanded=True),
    ])
    # Already harvested at the current T0 revision.
    store.record_crawl('FreshPage', 600, [
        DependencyRecord('T0', revision_id=10, expanded=True),
    ])
    return store


class TestPlanRefresh(unittest.TestCase):

    def setUp(self):
        self.graph = build_graph()
        self.store = seed_store()

    def test_direct_and_two_layer_indirect_pages_are_claimed(self):
        jobs = plan_refresh('T0', self.graph, self.store, max_depth=2)
        pages = {job.page for job in jobs}
        self.assertEqual(
            pages, {'DirectPage', 'IndirectPage', 'IncompletePage'}
        )

    def test_depth_one_excludes_two_layer_indirect_pages(self):
        jobs = plan_refresh('T0', self.graph, self.store, max_depth=1)
        self.assertEqual({job.page for job in jobs}, {'DirectPage'})

    def test_each_job_carries_a_change_path(self):
        jobs = {
            job.page: job for job in
            plan_refresh('T0', self.graph, self.store, max_depth=2)
        }
        self.assertEqual(
            jobs['DirectPage'].paths[0].edges, ('T0', 'DirectPage')
        )
        self.assertEqual(
            jobs['IndirectPage'].paths[0].edges,
            ('T0', 'T1', 'IndirectPage'),
        )

    def test_depth_two_excludes_three_layer_pages(self):
        jobs = plan_refresh('T0', self.graph, self.store, max_depth=2)
        # DeepPage is reachable only at the third template layer; the
        # two-layer bound must not reach it.
        self.assertNotIn('DeepPage', {job.page for job in jobs})

    def test_never_harvested_pages_are_not_offered(self):
        graph = FakeGraph(
            {'T0': ('UnharvestedPage',)},
            templates=('T0',),
            revisions={'T0': 10},
        )
        jobs = plan_refresh('T0', graph, self.store, max_depth=2)
        self.assertEqual(jobs, [])

    def test_unexpanded_branch_with_complete_record_is_skipped(self):
        jobs = plan_refresh('T0', self.graph, self.store, max_depth=2)
        self.assertNotIn('ConditionalPage', {job.page for job in jobs})

    def test_incomplete_record_can_never_be_safe_skipped(self):
        jobs = {
            job.page: job for job in
            plan_refresh('T0', self.graph, self.store, max_depth=2)
        }
        job = jobs['IncompletePage']
        self.assertEqual(job.reason, 'incomplete-dependencies')
        self.assertEqual(
            job.paths[0].edges, ('T0', 'T1', 'IncompletePage')
        )

    def test_stale_reason_for_outdated_expanded_revision(self):
        jobs = {
            job.page: job for job in
            plan_refresh('T0', self.graph, self.store, max_depth=2)
        }
        self.assertEqual(jobs['DirectPage'].reason, 'stale-template-revision')
        self.assertEqual(
            jobs['IndirectPage'].reason, 'stale-template-revision'
        )

    def test_page_already_at_batch_revision_is_skipped(self):
        jobs = plan_refresh('T0', self.graph, self.store, max_depth=2)
        self.assertNotIn('FreshPage', {job.page for job in jobs})

    def test_all_jobs_share_one_frozen_revision_batch(self):
        jobs = plan_refresh('T0', self.graph, self.store, max_depth=2)
        expected = (
            TemplateRevision('T0', 10),
            TemplateRevision('T1', 5),
        )
        for job in jobs:
            self.assertEqual(job.batch, expected)

    def test_claimed_pages_are_not_returned_again(self):
        jobs = plan_refresh(
            'T0', self.graph, self.store,
            max_depth=2, claimed_pages=('DirectPage',),
        )
        self.assertNotIn('DirectPage', {job.page for job in jobs})

    def test_multiple_change_paths_are_kept(self):
        graph = FakeGraph(
            {
                'T0': ('T1', 'T2', 'SharedPage'),
                'T1': ('SharedPage',),
                'T2': ('SharedPage',),
            },
            templates=('T0', 'T1', 'T2'),
            revisions={'T0': 10, 'T1': 5, 'T2': 8},
        )
        store = MemoryCrawlStore()
        store.record_crawl('SharedPage', 1, [
            DependencyRecord('T0', 9, True),
            DependencyRecord('T1', 5, True),
            DependencyRecord('T2', 8, True),
        ])
        jobs = plan_refresh('T0', graph, store, max_depth=2)
        self.assertEqual(len(jobs), 1)
        edges = {path.edges for path in jobs[0].paths}
        self.assertEqual(edges, {
            ('T0', 'SharedPage'),
            ('T0', 'T1', 'SharedPage'),
            ('T0', 'T2', 'SharedPage'),
        })

    def test_transclusion_cycle_does_not_loop_forever(self):
        graph = FakeGraph(
            {'T0': ('T1',), 'T1': ('T0', 'LoopPage')},
            templates=('T0', 'T1'),
            revisions={'T0': 10, 'T1': 5},
        )
        store = MemoryCrawlStore()
        store.record_crawl('LoopPage', 1, [
            DependencyRecord('T0', 9, True),
            DependencyRecord('T1', 5, True),
        ])
        jobs = plan_refresh('T0', graph, store, max_depth=5)
        self.assertEqual({job.page for job in jobs}, {'LoopPage'})

    def test_complete_record_missing_path_template_is_claimed(self):
        store = MemoryCrawlStore()
        # The record set is complete per se, but it never mentions T0 even
        # though T0 reaches the page through T1.
        store.record_crawl('IndirectPage', 201, [
            DependencyRecord('T1', revision_id=5, expanded=True),
        ])
        jobs = plan_refresh('T0', self.graph, store, max_depth=2)
        self.assertEqual({job.page for job in jobs}, {'IndirectPage'})
        self.assertEqual(jobs[0].reason, 'incomplete-dependencies')

    def test_one_expanded_branch_among_many_blocks_skip(self):
        store = MemoryCrawlStore()
        store.record_crawl('DirectPage', 101, [
            DependencyRecord('T0', revision_id=None, expanded=False),
            DependencyRecord('T0', revision_id=9, expanded=True),
        ])
        jobs = plan_refresh('T0', self.graph, store, max_depth=1)
        self.assertEqual({job.page for job in jobs}, {'DirectPage'})
        self.assertEqual(jobs[0].reason, 'stale-template-revision')

    def test_invalid_max_depth(self):
        with self.assertRaises(ValueError):
            plan_refresh('T0', self.graph, self.store, max_depth=0)

    def test_missing_dependency_record_is_treated_as_incomplete(self):
        class BareStore(CrawlStore):
            def get_page_revision(self, page):
                return 1 if page == 'BarePage' else None

            def get_dependencies(self, page):
                return None

            def record_crawl(self, page, page_revision, dependencies):
                raise AssertionError('should not record during planning')

        graph = FakeGraph(
            {'T0': ('BarePage',)},
            templates=('T0',),
            revisions={'T0': 10},
        )
        jobs = plan_refresh('T0', graph, BareStore(), max_depth=1)
        self.assertEqual({job.page for job in jobs}, {'BarePage'})
        self.assertEqual(jobs[0].reason, 'incomplete-dependencies')


class TestSettleCrawl(unittest.TestCase):

    def setUp(self):
        self.graph = build_graph()
        self.store = seed_store()
        self.jobs = plan_refresh(
            'T0', self.graph, self.store, max_depth=2
        )
        self.batch = self.jobs[0].batch

    def test_settle_with_batch_revisions_is_recorded(self):
        result = settle_crawl(
            'DirectPage', 101,
            [DependencyRecord('T0', 10, True)],
            self.batch, self.store,
        )
        self.assertTrue(result.settled)
        self.assertEqual(result.blocking, ())
        self.assertEqual(self.store.get_page_revision('DirectPage'), 101)

    def test_stale_crawl_is_rejected_and_not_recorded(self):
        result = settle_crawl(
            'DirectPage', 102,
            [DependencyRecord('T0', 9, True)],
            self.batch, self.store,
        )
        self.assertFalse(result.settled)
        self.assertEqual(len(result.stale), 1)
        self.assertEqual(result.stale[0].template, 'T0')
        self.assertEqual(self.store.get_page_revision('DirectPage'), 100)

    def test_crawl_missing_revision_is_rejected(self):
        result = settle_crawl(
            'IncompletePage', 501,
            [
                DependencyRecord('T1', 5, True),
                DependencyRecord('T0', None, True),
            ],
            self.batch, self.store,
        )
        self.assertFalse(result.settled)
        self.assertEqual(len(result.missing_revision), 1)

    def test_settled_page_is_not_claimed_again(self):
        settle_crawl(
            'DirectPage', 101,
            [DependencyRecord('T0', 10, True)],
            self.batch, self.store,
        )
        jobs = plan_refresh(
            'T0', self.graph, self.store, max_depth=2
        )
        self.assertNotIn('DirectPage', {job.page for job in jobs})

    def test_unexpanded_dependency_does_not_block_settlement(self):
        result = settle_crawl(
            'ConditionalPage', 401,
            [DependencyRecord('T0', None, False)],
            self.batch, self.store,
        )
        self.assertTrue(result.settled)


if __name__ == '__main__':
    unittest.main()
