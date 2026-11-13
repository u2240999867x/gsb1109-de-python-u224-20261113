import unittest
import unittest.mock as mock

from mwclient import contrib
from mwclient.contrib import (
    ADDED, OBSERVED, REMOVED, REAPPEARED, UNVERIFIABLE,
    ContributionReport, analyze_page, analyze_revisions
)


def rev(revid, parentid, text=None, slot=None):
    data = {'revid': revid, 'parentid': parentid}
    if text is not None:
        if slot is None:
            data['*'] = text
        else:
            data['slots'] = {slot: {'*': text}}
    return data


class TestContributionAnalysis(unittest.TestCase):

    def test_page_creation_is_all_new(self):
        report = analyze_revisions([rev(1, 0, 'Alpha\n\nBeta')])

        contribution = report.for_revision(1)
        assert contribution.hidden_parent is False
        assert [change.text for change in contribution.added] == ['Alpha', 'Beta']
        assert contribution.removed == [] and contribution.reappeared == []
        assert all(change.kind == ADDED for change in contribution.added)
        assert all(change.basis_revid is None for change in contribution.added)
        assert report.added_count == 2

    def test_removed_paragraphs_are_attributed_to_the_edit(self):
        revisions = [
            rev(1, 0, 'Alpha\n\nBeta'),
            rev(2, 1, 'Alpha'),
        ]
        report = analyze_revisions(revisions)

        contribution = report.for_revision(2)
        assert len(contribution.removed) == 1
        removed = contribution.removed[0]
        assert removed.text == 'Beta'
        assert removed.kind == REMOVED
        # The diff is taken against the actual parent revision, rev 1.
        assert removed.parentid == 1
        assert removed.basis_revid == 1

    def test_restored_text_is_reappeared_not_new(self):
        # rev 1 introduces Alpha and Beta, rev 2 deletes Beta,
        # rev 3 restores the old body text including Beta.
        revisions = [
            rev(1, 0, 'Alpha\n\nBeta'),
            rev(2, 1, 'Alpha'),
            rev(3, 2, 'Alpha\n\nBeta'),
        ]
        report = analyze_revisions(revisions)

        restore = report.for_revision(3)
        assert restore.novel_count == 0
        assert restore.added == []
        assert len(restore.reappeared) == 1
        restored = restore.reappeared[0]
        assert restored.text == 'Beta'
        assert restored.kind == REAPPEARED
        # Points back at the revisions used for the comparison.
        assert restored.parentid == 2
        assert restored.basis_revid == 2
        # And at the visible revision in which the text first appeared.
        assert restored.first_seen == 1
        assert restore.removed == []
        assert report.added_count == 2  # only the two paragraphs from rev 1
        assert report.reappeared_count == 1

    def test_restore_of_whole_page_is_not_counted_as_full_page_addition(self):
        # The concrete mis-counted edit: a full-page restore must report zero
        # newly written paragraphs even though every paragraph is "incoming"
        # relative to an empty diff base.
        body = 'First\n\nSecond\n\nThird'
        revisions = [
            rev(1, 0, body),
            rev(2, 1, 'Stub'),
            rev(3, 2, body),
        ]
        report = analyze_revisions(revisions)

        restore = report.for_revision(3)
        assert restore.novel_count == 0
        assert {change.text for change in restore.reappeared} == {
            'First', 'Second', 'Third'
        }
        assert {change.basis_revid for change in restore.reappeared} == {2}
        assert {change.first_seen for change in restore.reappeared} == {1}
        # The stub paragraph was removed by the restore edit.
        assert [change.text for change in restore.removed] == ['Stub']

    def test_identical_text_in_separate_chains_is_not_a_revert(self):
        # Two independent parent chains accidentally share the same text.
        # Content equality alone must never be taken as evidence of a revert.
        chain_one = [
            rev(10, 0, 'Shared paragraph'),
            rev(11, 10, 'Shared paragraph\n\nChain one addition'),
        ]
        chain_two = [
            rev(20, 0, 'Shared paragraph'),
            rev(21, 20, 'Shared paragraph\n\nChain two addition'),
        ]

        report_one = analyze_revisions(chain_one)
        report_two = analyze_revisions(chain_two)

        # In each chain the shared paragraph is a genuine creation edit.
        assert [change.text for change in report_one.for_revision(10).added] == [
            'Shared paragraph'
        ]
        assert [change.text for change in report_two.for_revision(20).added] == [
            'Shared paragraph'
        ]
        assert report_one.for_revision(11).reappeared == []
        assert report_two.for_revision(21).reappeared == []

    def test_hidden_parent_splits_verifiable_and_baseless_paragraphs(self):
        # rev 2 is hidden: rev 3 cannot be diffed against its real parent.
        revisions = [
            rev(1, 0, 'Known paragraph'),
            rev(2, 1),  # content hidden by revision deletion
            rev(3, 2, 'Known paragraph\n\nMystery paragraph'),
        ]
        report = analyze_revisions(revisions)

        assert report.hidden_revisions == [2]
        contribution = report.for_revision(3)
        assert contribution.hidden_parent is True
        assert contribution.added == []
        assert contribution.removed == []
        assert contribution.reappeared == []

        verifiable = {change.text: change for change in contribution.verifiable}
        assert set(verifiable) == {'Known paragraph'}
        known = verifiable['Known paragraph']
        assert known.kind == OBSERVED
        assert known.basis_revid is None
        assert known.first_seen == 1
        assert known.parentid == 2

        unverifiable = contribution.unverifiable
        assert len(unverifiable) == 1
        mystery = unverifiable[0]
        assert mystery.text == 'Mystery paragraph'
        assert mystery.kind == UNVERIFIABLE
        assert mystery.basis_revid is None
        assert mystery.first_seen is None
        assert contribution.is_verifiable is False
        assert report.unverifiable_count == 1

    def test_revisions_with_visible_parent_remain_verifiable(self):
        report = analyze_revisions([
            rev(1, 0, 'Alpha'),
            rev(2, 1, 'Alpha\n\nBeta'),
        ])
        assert report.for_revision(1).is_verifiable is True
        assert report.for_revision(2).is_verifiable is True
        assert report.hidden_revisions == []

    def test_multi_slot_content_is_supported(self):
        revisions = [
            rev(1, 0, 'Alpha', slot='main'),
            rev(2, 1, 'Alpha\n\nBeta', slot='main'),
        ]
        report = analyze_revisions(revisions, slot='main')
        assert [change.text for change in report.for_revision(2).added] == ['Beta']

    def test_api_ordering_is_normalised_along_the_parent_chain(self):
        # The API returns newest first; analysis must still follow parentid.
        revisions = [
            rev(3, 2, 'Alpha\n\nBeta'),
            rev(2, 1, 'Alpha'),
            rev(1, 0, 'Alpha\n\nBeta'),
        ]
        report = analyze_revisions(revisions)
        assert [item.revid for item in report.revisions] == [1, 2, 3]
        assert report.for_revision(3).reappeared[0].text == 'Beta'

    def test_duplicate_paragraphs_are_counted_per_occurrence(self):
        report = analyze_revisions([
            rev(1, 0, 'Alpha'),
            rev(2, 1, 'Alpha\n\nAlpha'),
        ])
        contribution = report.for_revision(2)
        assert len(contribution.added) == 1
        assert contribution.added[0].text == 'Alpha'

    def test_for_unknown_revision_raises_keyerror(self):
        report = analyze_revisions([rev(1, 0, 'Alpha')])
        with self.assertRaises(KeyError):
            report.for_revision(999)

    def test_analyze_page_fetches_content_and_parent_ids(self):
        page = mock.MagicMock()
        page.revisions.return_value = iter([
            rev(2, 1, 'Alpha\n\nBeta'),
            rev(1, 0, 'Alpha'),
        ])
        report = analyze_page(page)

        page.revisions.assert_called_once_with(
            prop='ids|timestamp|flags|content',
            max_items=None
        )
        assert isinstance(report, ContributionReport)
        assert report.for_revision(2).added[0].text == 'Beta'
        assert contrib.ADDED == ADDED
