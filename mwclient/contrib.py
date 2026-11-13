"""Contribution analysis across the visible revision history of a page.

A naive contribution report compares every revision with an empty page and
therefore counts an edit that restores old text as a whole page of newly
written content. The helpers in this module instead compare each visible
revision with its immediate parent, following the ``parentid`` links returned
by the MediaWiki API, and keep track of every paragraph that has been seen
earlier in the same parent chain.

This makes it possible to tell three kinds of changes apart:

* ``added``: text that does not occur in the parent revision and has never
  been seen in any earlier visible revision of the same chain.
* ``removed``: text that occurs in the parent revision but no longer occurs in
  the current revision.
* ``reappeared``: text that is absent from the parent revision but did occur
  in some earlier visible revision of the same chain, i.e. a restore of
  previously removed content.

Two unrelated parent chains can accidentally contain identical text. Identity
of content is never treated as evidence of a revert: a paragraph is only
classified as ``reappeared`` when the ``parentid`` linkage of the chain being
analysed shows that the text existed earlier in that very chain and was
missing from the immediate parent.

When a parent revision is hidden (revision deletion), its text is not
available through the API, so a parent diff cannot be computed. The affected
revision is then split into a verifiable part (paragraphs that are confirmed
to have existed in still-visible earlier revisions) and an unverifiable part
(paragraphs for which no visible basis exists), instead of guessing.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

if TYPE_CHECKING:
    from mwclient.page import Page

# Paragraph change kinds
ADDED = 'added'
REMOVED = 'removed'
REAPPEARED = 'reappeared'
OBSERVED = 'observed'
UNVERIFIABLE = 'unverifiable'

_PARAGRAPH_SPLIT = re.compile(r'\n[ \t]*\n')


@dataclass
class ParagraphChange:
    """A single paragraph-level change attributed to one revision.

    Attributes:
        text: The paragraph text.
        kind: One of :data:`ADDED`, :data:`REMOVED`, :data:`REAPPEARED`,
            :data:`OBSERVED` or :data:`UNVERIFIABLE`.
        revid: The revision the change is attributed to.
        parentid: The ``parentid`` declared by that revision. This may point
            at a hidden revision, in which case ``basis_revid`` is ``None``.
        basis_revid: The visible revision id actually used as the comparison
            basis, or ``None`` if no visible comparison basis exists.
        first_seen: The id of the earliest visible revision of this chain in
            which the paragraph text occurred, or ``None`` if it has never
            been seen in a visible revision.
    """

    text: str
    kind: str
    revid: int
    parentid: Optional[int]
    basis_revid: Optional[int] = None
    first_seen: Optional[int] = None


@dataclass
class RevisionContribution:
    """The contribution attributed to a single visible revision.

    For revisions whose parent is visible, ``added``, ``removed`` and
    ``reappeared`` are populated and ``verifiable``/``unverifiable`` are empty.
    For revisions whose parent is hidden, no parent diff is possible, so the
    first three lists stay empty and ``verifiable``/``unverifiable`` describe
    what can and cannot be backed by visible evidence instead.
    """

    revid: int
    parentid: Optional[int]
    hidden_parent: bool
    added: List[ParagraphChange] = field(default_factory=list)
    removed: List[ParagraphChange] = field(default_factory=list)
    reappeared: List[ParagraphChange] = field(default_factory=list)
    verifiable: List[ParagraphChange] = field(default_factory=list)
    unverifiable: List[ParagraphChange] = field(default_factory=list)

    @property
    def novel_count(self) -> int:
        """Number of genuinely new paragraphs (restores are excluded)."""
        return len(self.added)

    @property
    def is_verifiable(self) -> bool:
        """Whether every change in this revision has a visible basis."""
        return not self.hidden_parent and not self.unverifiable


@dataclass
class ContributionReport:
    """The contribution analysis of one parent chain.

    Attributes:
        revisions: Per-revision results, ordered from oldest to newest.
        hidden_revisions: Ids of revisions whose content is hidden and which
            therefore could not be used as a comparison basis.
    """

    revisions: List[RevisionContribution] = field(default_factory=list)
    hidden_revisions: List[int] = field(default_factory=list)

    def for_revision(self, revid: int) -> RevisionContribution:
        """Return the :class:`RevisionContribution` for ``revid``.

        Raises:
            KeyError: If the revision is not part of the visible analysis.
        """
        for contribution in self.revisions:
            if contribution.revid == revid:
                return contribution
        raise KeyError(revid)

    @property
    def added_count(self) -> int:
        return sum(len(item.added) for item in self.revisions)

    @property
    def removed_count(self) -> int:
        return sum(len(item.removed) for item in self.revisions)

    @property
    def reappeared_count(self) -> int:
        return sum(len(item.reappeared) for item in self.revisions)

    @property
    def unverifiable_count(self) -> int:
        return sum(len(item.unverifiable) for item in self.revisions)


def _split_paragraphs(text: Optional[str]) -> List[str]:
    """Split wikitext into trimmed, non-empty paragraphs."""
    if not text:
        return []
    return [part.strip() for part in _PARAGRAPH_SPLIT.split(text) if part.strip()]


def _revision_text(revision: Mapping[str, Any], slot: str = 'main') -> Optional[str]:
    """Extract the content of a revision, supporting the multi-slot format."""
    if 'slots' in revision:
        slot_data = revision['slots'].get(slot)
        if slot_data is None or '*' not in slot_data:
            return None
        content = slot_data['*']
    else:
        content = revision.get('*')
    if content is None:
        return None
    return str(content)


def _is_hidden(revision: Mapping[str, Any], slot: str) -> bool:
    """Whether the revision content is hidden (revision deletion)."""
    return _revision_text(revision, slot) is None


def _diff_visible_parent(
    revision_id: int,
    parent_id: int,
    parent_paragraphs: 'Counter[str]',
    child_paragraphs: 'Counter[str]',
    first_seen: Dict[str, int]
) -> RevisionContribution:
    """Diff a revision against its visible parent revision."""
    contribution = RevisionContribution(
        revid=revision_id, parentid=parent_id, hidden_parent=False
    )

    for text, child_count in child_paragraphs.items():
        parent_count = parent_paragraphs.get(text, 0)
        incoming = child_count - parent_count
        if incoming <= 0:
            continue
        if parent_count == 0 and text in first_seen:
            kind = REAPPEARED
        else:
            kind = ADDED
        for _ in range(incoming):
            change = ParagraphChange(
                text=text,
                kind=kind,
                revid=revision_id,
                parentid=parent_id,
                basis_revid=parent_id,
                first_seen=first_seen.get(text)
            )
            if kind == REAPPEARED:
                contribution.reappeared.append(change)
            else:
                contribution.added.append(change)

    for text, parent_count in parent_paragraphs.items():
        outgoing = parent_count - child_paragraphs.get(text, 0)
        for _ in range(outgoing):
            contribution.removed.append(ParagraphChange(
                text=text,
                kind=REMOVED,
                revid=revision_id,
                parentid=parent_id,
                basis_revid=parent_id,
                first_seen=first_seen.get(text)
            ))

    return contribution


def _diff_hidden_parent(
    revision_id: int,
    parent_id: int,
    child_paragraphs: 'Counter[str]',
    first_seen: Dict[str, int]
) -> RevisionContribution:
    """Record what is and is not verifiable when the parent is hidden."""
    contribution = RevisionContribution(
        revid=revision_id, parentid=parent_id, hidden_parent=True
    )

    for text, count in child_paragraphs.items():
        seen_before = text in first_seen
        kind = OBSERVED if seen_before else UNVERIFIABLE
        for _ in range(count):
            change = ParagraphChange(
                text=text,
                kind=kind,
                revid=revision_id,
                parentid=parent_id,
                basis_revid=None,
                first_seen=first_seen.get(text)
            )
            if seen_before:
                contribution.verifiable.append(change)
            else:
                contribution.unverifiable.append(change)

    return contribution


def analyze_revisions(
    revisions: List[Mapping[str, Any]],
    slot: str = 'main'
) -> ContributionReport:
    """Analyse an iterable of revision dictionaries from the MediaWiki API.

    Each revision must carry ``revid`` and ``parentid`` and, unless its
    content is hidden, the revision content either under the ``*`` key or
    under ``slots[slot]['*']``. Revisions are ordered by ``revid`` so the
    analysis follows the actual parent chain regardless of API ordering.

    Args:
        revisions: Revision dictionaries, e.g. from
            :meth:`mwclient.page.Page.revisions` with ``prop='content'``.
        slot: Content slot to read when the multi-slot format is used.

    Returns:
        ContributionReport: The per-revision classification of changes.
    """
    visible = sorted(
        (rev for rev in revisions if not _is_hidden(rev, slot)),
        key=lambda rev: rev['revid']
    )
    by_id = {rev['revid']: rev for rev in visible}

    report = ContributionReport()
    report.hidden_revisions = sorted(
        rev['revid'] for rev in revisions if _is_hidden(rev, slot)
    )

    # Paragraphs known from the visible chain, with the revision id in which
    # each distinct paragraph text was first observed.
    first_seen = {}  # type: Dict[str, int]

    for revision in visible:
        revision_id = revision['revid']
        parent_id = revision.get('parentid', 0) or 0
        child_paragraphs = Counter(_split_paragraphs(_revision_text(revision, slot)))

        parent = by_id.get(parent_id)
        if parent_id == 0 or parent is None:
            if parent_id == 0:
                # Page creation: every paragraph is genuinely new.
                contribution = RevisionContribution(
                    revid=revision_id, parentid=None, hidden_parent=False
                )
                for text, count in child_paragraphs.items():
                    for _ in range(count):
                        contribution.added.append(ParagraphChange(
                            text=text,
                            kind=ADDED,
                            revid=revision_id,
                            parentid=None,
                            basis_revid=None,
                            first_seen=None
                        ))
            else:
                # The declared parent exists but its content is not visible.
                contribution = _diff_hidden_parent(
                    revision_id, parent_id, child_paragraphs, first_seen
                )
        else:
            parent_paragraphs = Counter(
                _split_paragraphs(_revision_text(parent, slot))
            )
            contribution = _diff_visible_parent(
                revision_id, parent_id, parent_paragraphs,
                child_paragraphs, first_seen
            )

        report.revisions.append(contribution)

        for text in child_paragraphs:
            first_seen.setdefault(text, revision_id)

    return report


def analyze_page(page: 'Page', max_items: Optional[int] = None) -> ContributionReport:
    """Analyse the visible revision history of a :class:`~mwclient.page.Page`.

    The revisions are fetched with their content and parent ids so that each
    edit can be compared with the revision it was actually based on.

    Args:
        page: The page whose revision history should be analysed.
        max_items: Maximum number of revisions to fetch (newest first from
            the API; ordering is normalised internally).

    Returns:
        ContributionReport: The per-revision classification of changes.
    """
    revisions = list(page.revisions(
        prop='ids|timestamp|flags|content',
        max_items=max_items
    ))
    return analyze_revisions(revisions)
