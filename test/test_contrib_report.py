import json
import unittest

from contrib_report import (
    DIFFERENT,
    SAME,
    Relation,
    build_report,
    record_from_api,
    records_from_json,
    relations_from_json,
    to_public_dict,
)


def make_record(revid, month='2026-10', user=None, anon=False, comment='c'):
    entry = {
        'revid': revid,
        'timestamp': '%s-05T12:00:00Z' % month,
        'user': user,
        'comment': comment,
    }
    if anon:
        entry['anon'] = ''
    return record_from_api(entry)


def make_relation(kind, a, b):
    return Relation(kind=kind, revids=(a, b))


def report_for(records, relations=()):
    reports, ignored = build_report(records, list(relations))
    assert not ignored
    return reports


class TestRecordParsing(unittest.TestCase):

    def test_missing_user_and_comment_are_tolerated(self):
        record = record_from_api({
            'revid': 7,
            'timestamp': '2026-10-01T00:00:00Z',
            'userhidden': '',
            'commenthidden': '',
        })
        self.assertIsNone(record.user)
        self.assertIsNone(record.comment)
        self.assertFalse(record.anon)

    def test_anon_flag_marks_display_name_as_anonymous(self):
        record = record_from_api({
            'revid': 8,
            'timestamp': '2026-10-01T00:00:00Z',
            'user': '203.0.113.7',
            'anon': '',
            'comment': 'typo',
        })
        self.assertTrue(record.anon)
        self.assertEqual(record.user, '203.0.113.7')


class TestMonthlyBounds(unittest.TestCase):

    def test_same_account_merges_records(self):
        records = [
            make_record(1, user='Alice'),
            make_record(2, user='Alice'),
            make_record(3, user='Bob'),
        ]
        (report,) = report_for(records)
        self.assertEqual(report.status, 'ok')
        self.assertEqual(report.max_contributors, 2)
        self.assertEqual(report.min_contributors, 1)
        self.assertEqual(report.components, [[1, 2], [3]])

    def test_shared_anon_display_name_does_not_merge(self):
        records = [
            make_record(1, user='203.0.113.7', anon=True),
            make_record(2, user='203.0.113.7', anon=True),
        ]
        (report,) = report_for(records)
        self.assertEqual(report.max_contributors, 2)
        self.assertEqual(report.min_contributors, 1)

    def test_admin_same_relation_merges_anon_records(self):
        records = [
            make_record(1, anon=True),
            make_record(2, anon=True),
        ]
        (report,) = report_for(records, [make_relation(SAME, 1, 2)])
        self.assertEqual(report.max_contributors, 1)
        self.assertEqual(report.min_contributors, 1)

    def test_different_relation_raises_minimum(self):
        records = [make_record(i) for i in range(1, 4)]
        relations = [
            make_relation(DIFFERENT, 1, 2),
            make_relation(DIFFERENT, 2, 3),
            make_relation(DIFFERENT, 1, 3),
        ]
        (report,) = report_for(records, relations)
        self.assertEqual(report.min_contributors, 3)
        self.assertEqual(report.max_contributors, 3)
        self.assertTrue(report.min_is_exact)

    def test_coloring_certificate_covers_all_components(self):
        records = [make_record(i) for i in range(1, 5)]
        relations = [make_relation(DIFFERENT, 1, 2)]
        (report,) = report_for(records, relations)
        self.assertEqual(report.min_contributors, 2)
        self.assertEqual(report.max_contributors, 4)
        flat = sorted(idx for group in report.coloring for idx in group)
        self.assertEqual(flat, [0, 1, 2, 3])
        for group in report.coloring:
            self.assertFalse({0, 1} <= set(group))

    def test_missing_user_and_comment_are_counted(self):
        records = [
            record_from_api({'revid': 1, 'timestamp': '2026-10-01T00:00:00Z'}),
            make_record(2, user='Alice', comment=None),
        ]
        (report,) = report_for(records)
        self.assertEqual(report.records_without_account, 1)
        self.assertEqual(report.records_without_comment, 2)

    def test_identity_evidence_applies_across_months(self):
        records = [
            make_record(1, month='2026-10', anon=True),
            make_record(2, month='2026-11', anon=True),
        ]
        reports = report_for(records, [make_relation(SAME, 1, 2)])
        self.assertEqual([r.max_contributors for r in reports], [1, 1])

    def test_unknown_revids_are_reported_as_ignored(self):
        records = [make_record(1)]
        reports, ignored = build_report(
            records, [make_relation(SAME, 1, 999)]
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(ignored, [make_relation(SAME, 1, 999)])


class TestContradictions(unittest.TestCase):

    def test_transitive_contradiction_suppresses_count(self):
        records = [
            make_record(1, user='Alice'),
            make_record(2, user='Alice'),
            make_record(3, anon=True),
        ]
        relations = [
            make_relation(SAME, 2, 3),
            make_relation(DIFFERENT, 1, 3),
        ]
        (report,) = report_for(records, relations)
        self.assertEqual(report.status, 'contradiction')
        self.assertIsNone(report.min_contributors)
        self.assertIsNone(report.max_contributors)
        (conflict,) = report.conflicts
        self.assertEqual(conflict.different, (1, 3))
        self.assertEqual(conflict.same_chain, [1, 2, 3])
        reasons = [link['reason'] for link in conflict.links]
        self.assertEqual(reasons, ['same_account', 'admin_same'])

    def test_contradiction_marks_only_touched_months(self):
        records = [
            make_record(1, month='2026-10', anon=True),
            make_record(2, month='2026-10', anon=True),
            make_record(3, month='2026-11', user='Carol'),
        ]
        relations = [make_relation(SAME, 1, 2), make_relation(DIFFERENT, 1, 2)]
        reports = report_for(records, relations)
        self.assertEqual([r.status for r in reports], ['contradiction', 'ok'])
        self.assertEqual(reports[1].max_contributors, 1)

    def test_public_output_contains_no_account_names(self):
        records = [
            make_record(1, user='Alice'),
            make_record(2, user='Alice'),
            make_record(3, user='203.0.113.7', anon=True),
        ]
        relations = [
            make_relation(SAME, 2, 3),
            make_relation(DIFFERENT, 1, 3),
        ]
        reports, ignored = build_report(records, relations)
        public_dict = to_public_dict(reports, ignored)
        public = json.dumps(public_dict)
        self.assertNotIn('Alice', public)
        self.assertNotIn('203.0.113.7', public)
        conflict = public_dict['months'][0]['conflicts'][0]
        self.assertEqual(conflict['same_chain'], [1, 2, 3])


class TestJsonRoundTrip(unittest.TestCase):

    def test_records_and_relations_round_trip(self):
        records = records_from_json([{
            'revid': 5,
            'timestamp': '2026-10-02T08:00:00Z',
            'user': None,
            'anon': True,
            'comment': None,
        }])
        self.assertEqual(records[0].revid, 5)
        self.assertTrue(records[0].anon)
        relations = relations_from_json([{'type': 'same', 'revids': [1, 2]}])
        self.assertEqual(relations, [make_relation(SAME, 1, 2)])

    def test_invalid_relation_type_rejected(self):
        with self.assertRaises(ValueError):
            relations_from_json([{'type': 'maybe', 'revids': [1, 2]}])


if __name__ == '__main__':
    unittest.main()
