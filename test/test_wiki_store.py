import unittest
import unittest.mock as mock

import pytest

from mwclient.errors import APIError
from mwclient.wiki_store import (
    ChangeRecord,
    GapError,
    PageStore,
    ReadState,
    UnresolvableIdentityError,
)

OLD_PORT = '旧港'
NEW_PORT = '新港'


def rc_row(rcid, timestamp, rctype, title, **kwargs):
    row = {
        'rcid': rcid,
        'timestamp': timestamp,
        'type': rctype,
        'ns': kwargs.pop('ns', 0),
        'title': title,
    }
    row.update(kwargs)
    return row


def new_page(rcid, timestamp, title, pageid, revid):
    return rc_row(rcid, timestamp, 'new', title, pageid=pageid, revid=revid,
                  old_revid=0)


def edit_row(rcid, timestamp, title, pageid, revid, old_revid):
    return rc_row(rcid, timestamp, 'edit', title, pageid=pageid, revid=revid,
                  old_revid=old_revid)


def move_row(rcid, timestamp, old_title, new_title, pageid,
             suppress_redirect=False):
    return rc_row(
        rcid, timestamp, 'log', old_title, pageid=pageid, logid=rcid + 1000,
        logtype='move', logaction='move',
        logparams={'target_ns': 0, 'target_title': new_title,
                   'suppressredirect': suppress_redirect},
    )


def delete_row(rcid, timestamp, title, pageid=None, action='delete'):
    return rc_row(rcid, timestamp, 'log', title, pageid=pageid,
                  logid=rcid + 2000, logtype='delete', logaction=action)


PORT_MOVE = move_row(3, '2026-11-03T10:00:00Z', OLD_PORT, NEW_PORT, 100)


def build_port_scenario():
    """Old port page is moved to new port, then a new page takes old port."""
    store = PageStore()
    store.apply_changes([
        ChangeRecord.from_rc(new_page(1, '2026-11-01T08:00:00Z', OLD_PORT, 100, 10)),
        ChangeRecord.from_rc(edit_row(2, '2026-11-02T09:00:00Z', OLD_PORT, 100, 11, 10)),
        ChangeRecord.from_rc(PORT_MOVE),
        ChangeRecord.from_rc(edit_row(4, '2026-11-04T11:00:00Z', NEW_PORT, 100, 12, 11)),
        ChangeRecord.from_rc(new_page(5, '2026-11-08T12:00:00Z', OLD_PORT, 200, 20)),
        ChangeRecord.from_rc(edit_row(6, '2026-11-09T13:00:00Z', OLD_PORT, 200, 21, 20)),
    ])
    return store


class TestPageIdentity(unittest.TestCase):

    def test_move_keeps_single_entity_with_continuous_history(self):
        store = build_port_scenario()

        assert set(store.pages) == {100, 200}
        moved = store.pages[100]
        assert moved.created == '2026-11-01T08:00:00Z'
        assert moved.current_title == NEW_PORT
        assert [change.revid for change in moved.changes] == [10, 11, None, 12]
        assert moved.deleted_at is None

    def test_new_page_at_freed_title_is_separate_history(self):
        store = build_port_scenario()

        newcomer = store.pages[200]
        assert newcomer.created == '2026-11-08T12:00:00Z'
        assert newcomer.current_title == OLD_PORT
        assert [change.revid for change in newcomer.changes] == [20, 21]

        # The two histories never share revisions.
        moved_revids = {c.revid for c in store.pages[100].changes}
        new_revids = {c.revid for c in store.pages[200].changes}
        assert moved_revids.isdisjoint(new_revids)

    def test_title_points_at_right_entity_for_any_day(self):
        store = build_port_scenario()

        before_move = store.resolve_title(OLD_PORT, '2026-11-02')
        assert before_move.is_complete
        assert before_move.pageids == [100]

        redirect_day = store.resolve_title(OLD_PORT, '2026-11-05')
        assert redirect_day.is_complete
        assert redirect_day.pageids == [100]
        assert redirect_day.assignments[0].redirect is True

        new_home = store.resolve_title(NEW_PORT, '2026-11-05')
        assert new_home.pageids == [100]

        recreated = store.resolve_title(OLD_PORT, '2026-11-09')
        assert recreated.is_complete
        assert recreated.pageids == [200]

    def test_title_resolution_reports_owed_ranges(self):
        store = build_port_scenario()

        # No change records before 2026-11-01: the whole day is owed.
        resolution = store.resolve_title(OLD_PORT, '2026-10-30')
        assert not resolution.is_complete
        assert resolution.gaps == [
            ('2026-10-30T00:00:00Z', '2026-10-31T00:00:00Z')
        ]

        with pytest.raises(GapError):
            store.page_at(OLD_PORT, '2026-10-30')

    def test_page_at_returns_entity_or_none_for_vacancy(self):
        store = PageStore()
        store.apply_change(ChangeRecord.from_rc(
            new_page(1, '2026-11-01T08:00:00Z', OLD_PORT, 100, 10)))
        store.apply_change(ChangeRecord.from_rc(
            move_row(2, '2026-11-02T09:00:00Z', OLD_PORT, NEW_PORT, 100,
                     suppress_redirect=True)))

        assert store.page_at(OLD_PORT, '2026-11-01T12:00:00Z').pageid == 100
        # Suppressed redirect: the old title is known to be vacant, not unknown.
        vacant = store.resolve_title(OLD_PORT, '2026-11-03')
        assert vacant.is_complete
        assert vacant.pageids == []
        assert store.page_at(OLD_PORT, '2026-11-03T12:00:00Z') is None
        assert store.page_at(NEW_PORT, '2026-11-03T12:00:00Z').pageid == 100

    def test_move_day_boundary_splits_title_intervals(self):
        store = build_port_scenario()
        move_day = store.resolve_title(OLD_PORT, '2026-11-03')

        # Before 10:00 the page itself, after 10:00 the redirect, both -> 100.
        assert [interval.start for interval in move_day.assignments] == [
            '2026-11-03T00:00:00Z', '2026-11-03T10:00:00Z'
        ]
        assert move_day.assignments[0].redirect is False
        assert move_day.assignments[1].redirect is True
        assert move_day.is_complete

    def test_deleting_redirect_does_not_delete_moved_entity(self):
        store = PageStore()
        store.apply_changes([
            ChangeRecord.from_rc(new_page(1, '2026-11-01T08:00:00Z', OLD_PORT, 100, 10)),
            ChangeRecord.from_rc(PORT_MOVE),
            ChangeRecord.from_rc(delete_row(7, '2026-11-06T14:00:00Z', OLD_PORT,
                                            action='delete_redir')),
            ChangeRecord.from_rc(new_page(5, '2026-11-08T12:00:00Z', OLD_PORT, 200, 20)),
        ])

        moved = store.pages[100]
        assert moved.deleted_at is None
        # While only the redirect existed, the title is known-vacant after
        # the redirect deletion, not unknown.
        assert store.resolve_title(OLD_PORT, '2026-11-07').pageids == []
        # The redirect deletion is part of the moved entity's trail.
        assert any(change.is_deletion for change in moved.changes)
        # And the later page under the same title is still a fresh entity.
        assert set(store.pages) == {100, 200}
        assert store.resolve_title(OLD_PORT, '2026-11-09').pageids == [200]


class TestReplayDeduplication(unittest.TestCase):

    def test_replayed_move_notification_creates_no_extra_entity(self):
        store = PageStore()
        move = ChangeRecord.from_rc(
            move_row(3, '2026-11-03T10:00:00Z', OLD_PORT, NEW_PORT, 100))

        assert store.apply_change(move) is True
        assert store.apply_change(move) is False
        assert store.duplicate_count == 1
        assert set(store.pages) == {100}

        intervals = store.title_index.to_dict()[OLD_PORT]
        new_intervals = store.title_index.to_dict()[NEW_PORT]
        assert len(intervals) == 1
        assert len(new_intervals) == 1

    def test_replayed_feed_window_is_idempotent(self):
        rows = [
            ChangeRecord.from_rc(new_page(1, '2026-11-01T08:00:00Z', OLD_PORT, 100, 10)),
            ChangeRecord.from_rc(PORT_MOVE),
        ]
        store = PageStore()
        store.apply_changes(rows)
        applied = store.apply_changes(rows)

        assert applied == 0
        assert store.duplicate_count == 2
        assert set(store.pages) == {100}

    def test_move_row_with_redirect_pageid_still_joins_moved_entity(self):
        # On some MediaWiki versions the move rc row's pageid identifies the
        # newly created redirect, not the moved page.
        store = PageStore()
        store.apply_change(ChangeRecord.from_rc(
            new_page(1, '2026-11-01T08:00:00Z', OLD_PORT, 100, 10)))
        redirect_id_move = rc_row(
            3, '2026-11-03T10:00:00Z', 'log', OLD_PORT, pageid=999,
            logid=1003, logtype='move', logaction='move',
            logparams={'target_ns': 0, 'target_title': NEW_PORT})

        store.apply_change(ChangeRecord.from_rc(redirect_id_move))

        assert set(store.pages) == {100}
        assert store.pages[100].current_title == NEW_PORT
        assert store.resolve_title(OLD_PORT, '2026-11-04').pageids == [100]
        assert store.resolve_title(NEW_PORT, '2026-11-04').pageids == [100]

    def test_composite_key_used_when_rcid_missing(self):
        first = ChangeRecord.from_rc(
            rc_row(None, '2026-11-03T10:00:00Z', 'log', OLD_PORT,
                   pageid=100, logid=99, logtype='move', logaction='move',
                   logparams={'target_title': NEW_PORT}))
        second = ChangeRecord.from_rc(
            rc_row(None, '2026-11-03T10:00:00Z', 'log', OLD_PORT,
                   pageid=100, logid=99, logtype='move', logaction='move',
                   logparams={'target_title': NEW_PORT}))

        store = PageStore()
        assert store.apply_change(first) is True
        assert store.apply_change(second) is False
        assert set(store.pages) == {100}

    def test_sync_replays_watermark_row_and_dedupes_it(self):
        first_window = [
            new_page(1, '2026-11-01T08:00:00Z', OLD_PORT, 100, 10),
            move_row(2, '2026-11-01T09:00:00Z', OLD_PORT, NEW_PORT, 100),
        ]
        # Second window overlaps the boundary by re-fetching the move row.
        second_window = [
            move_row(2, '2026-11-01T09:00:00Z', OLD_PORT, NEW_PORT, 100),
            edit_row(3, '2026-11-01T10:00:00Z', NEW_PORT, 100, 12, 11),
        ]
        site = mock.Mock()
        site.recentchanges.side_effect = [
            [{'timestamp': '2026-11-01T08:00:00Z'}],  # retention probe
            [ChangeRecord.from_rc(row).raw for row in second_window],
        ]

        store = PageStore()
        store.apply_changes(ChangeRecord.from_rc(row) for row in first_window)
        summary = store.sync(site)

        assert summary.applied == 1
        assert summary.duplicates == 1
        assert set(store.pages) == {100}
        assert len(store.title_index.to_dict()[NEW_PORT]) == 1

    def test_sync_raises_gap_when_feed_retention_lost(self):
        store = PageStore()
        store.apply_change(ChangeRecord.from_rc(
            new_page(1, '2026-10-01T08:00:00Z', OLD_PORT, 100, 10)))
        site = mock.Mock()
        # The oldest row the feed still retains is newer than our watermark.
        site.recentchanges.return_value = [
            {'timestamp': '2026-10-02T00:00:00Z'}
        ]

        with pytest.raises(GapError):
            store.sync(site)


class TestReadStateClassification(unittest.TestCase):

    def test_delete_log_is_known_deletion_without_body(self):
        store = PageStore()
        deletion = ChangeRecord.from_rc(
            delete_row(9, '2026-11-05T10:00:00Z', OLD_PORT, pageid=100))
        site = mock.Mock()

        snapshot = store.fetch_snapshot(site, deletion)

        assert snapshot.state == ReadState.DELETED
        site.get.assert_not_called()

    def test_permission_error_is_inaccessible_not_deleted(self):
        store = PageStore()
        change = ChangeRecord.from_rc(
            edit_row(8, '2026-11-05T10:00:00Z', OLD_PORT, 100, 55, 54))
        site = mock.Mock()
        site.get.side_effect = APIError('readapidenied', 'denied', None)

        snapshot = store.fetch_snapshot(site, change)

        assert snapshot.state == ReadState.INACCESSIBLE
        assert snapshot.text is None

    def test_missing_response_without_delete_log_is_inaccessible(self):
        store = PageStore()
        change = ChangeRecord.from_rc(
            edit_row(8, '2026-11-05T10:00:00Z', OLD_PORT, 100, 55, 54))
        site = mock.Mock()
        site.get.return_value = {
            'query': {'pages': {'-1': {'missing': '', 'title': OLD_PORT}}}
        }

        snapshot = store.fetch_snapshot(site, change)

        assert snapshot.state == ReadState.INACCESSIBLE

    def test_visible_body_is_stored(self):
        store = PageStore()
        change = ChangeRecord.from_rc(
            edit_row(8, '2026-11-05T10:00:00Z', OLD_PORT, 100, 55, 54))
        site = mock.Mock()
        site.get.return_value = {
            'query': {'pages': {'100': {
                'pageid': 100,
                'revisions': [{
                    'revid': 55,
                    'timestamp': '2026-11-05T10:00:00Z',
                    'slots': {'main': {'*': 'harbor body'}},
                }],
            }}}
        }

        snapshot = store.attach_snapshot(site, change)

        assert snapshot.state == ReadState.VISIBLE
        assert snapshot.text == 'harbor body'
        assert store.pages[100].snapshots == [snapshot]

    def test_attach_marks_entity_inaccessible(self):
        store = PageStore()
        change = ChangeRecord.from_rc(
            edit_row(8, '2026-11-05T10:00:00Z', OLD_PORT, 100, 55, 54))
        site = mock.Mock()
        site.get.side_effect = APIError('suppressedinfo', 'hidden', None)

        store.attach_snapshot(site, change)

        assert store.pages[100].inaccessible is True
        assert store.pages[100].deleted_at is None

    def test_unrelated_api_error_propagates(self):
        store = PageStore()
        change = ChangeRecord.from_rc(
            edit_row(8, '2026-11-05T10:00:00Z', OLD_PORT, 100, 55, 54))
        site = mock.Mock()
        site.get.side_effect = APIError('badcontinue', 'boom', None)

        with pytest.raises(APIError):
            store.fetch_snapshot(site, change)


class TestPersistence(unittest.TestCase):

    def test_roundtrip_preserves_identities_and_intervals(self):
        import os
        import tempfile

        store = build_port_scenario()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'store.json')
            store.save(path)
            reloaded = PageStore.load(path)

        assert set(reloaded.pages) == {100, 200}
        assert reloaded.pages[100].current_title == NEW_PORT
        assert reloaded.pages[200].created == '2026-11-08T12:00:00Z'
        assert reloaded.cursor == store.cursor
        assert reloaded.resolve_title(OLD_PORT, '2026-11-09').pageids == [200]

        # Replayed rows stay de-duplicated after reload.
        assert reloaded.apply_change(ChangeRecord.from_rc(
            move_row(3, '2026-11-03T10:00:00Z', OLD_PORT, NEW_PORT, 100))) is False

    def test_unresolvable_change_points_at_missing_record(self):
        store = PageStore()
        orphan = ChangeRecord.from_rc(
            edit_row(1, '2026-11-05T10:00:00Z', OLD_PORT, None, None, None))

        with pytest.raises(UnresolvableIdentityError) as exc:
            store.apply_change(orphan)
        assert exc.value.title == OLD_PORT
        assert exc.value.timestamp == '2026-11-05T10:00:00Z'


if __name__ == '__main__':
    unittest.main()
