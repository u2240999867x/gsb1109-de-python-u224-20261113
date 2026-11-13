"""Incremental knowledge-base ingestion driven by MediaWiki recentchanges.

The knowledge base is rebuilt incrementally from the recentchanges feed
(``action=query&list=recentchanges``). This module deliberately separates
two concepts that are easy to conflate:

* The **page identity** (the MediaWiki ``pageid``). A move renames a page but
  keeps its pageid, so the moved page stays one entity with one continuous
  history. A page created later under a freed title gets a new pageid and is
  therefore a second, independent entity.
* The **title assignment**. A title is merely a time-ranged pointer
  (``TitleInterval``) to a pageid. Titles can be reassigned (moves), left
  vacant (deletion / moves without a redirect) or held by a redirect that
  still points at the moved entity.

Three ingestion guarantees are supported:

1. Two pages that successively occupy the same title (the "old port" /
   "new port" scenario) keep two separate histories, keyed by pageid, while
   the title index remembers who the title pointed at on any given day.
2. Overlapping incremental fetches replay the boundary rows. Feed rows are
   de-duplicated by rcid (or a composite key when the rcid is absent), so a
   replayed move notification never spawns a second entity or extra title
   intervals.
3. A deletion notice without readable body text is never taken as proof of
   deletion on its own. A delete-log record marks ``ReadState.DELETED``;
   a fetch that fails with a permission-style API error, or that finds the
   title missing without a delete log on record, is marked
   ``ReadState.INACCESSIBLE`` (temporarily unreadable) instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import mwclient.errors

logger = logging.getLogger(__name__)

# recentchanges event types (rc_type)
RC_EDIT = 'edit'
RC_NEW = 'new'
RC_LOG = 'log'

# relevant log types / actions
LOG_TYPE_MOVE = 'move'
LOG_TYPE_DELETE = 'delete'
MOVE_LOG_ACTIONS = frozenset({'move', 'move_redir'})
DELETE_LOG_ACTIONS = frozenset({'delete', 'delete_redir'})

# API error codes that indicate the content exists (or may exist) but the
# current user is not allowed to read it, as opposed to a confirmed deletion.
DEFAULT_PERMISSION_ERROR_CODES = frozenset({
    'readapidenied',
    'revdeleted',
    'suppressedinfo',
    'hiddenpage',
    'permissiondenied',
})

_TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
_DAY_FORMAT = '%Y-%m-%d'
_EPOCH = '2001-01-01T00:00:00Z'


class ReadState:
    """How the body text of a revision was (or could not be) obtained."""

    VISIBLE = 'visible'
    """The body text was returned by the API."""

    DELETED = 'deleted'
    """A delete-log record confirms the page/revision was deleted."""

    INACCESSIBLE = 'inaccessible'
    """No body text is available right now, but deletion is not confirmed."""


class IngestionError(RuntimeError):
    """Base class for knowledge-base ingestion errors."""


class GapError(IngestionError):
    """Raised when a time range has no recentchanges coverage.

    The feed cannot prove that nothing happened in the gap; the caller must
    backfill it (e.g. via the full revision/log history) before the
    knowledge base can answer questions about that range.
    """

    def __init__(self, start: str, end: str) -> None:
        self.start = start
        self.end = end
        super().__init__(
            'recentchanges gap between {0} and {1}; change records are '
            'missing for that range'.format(start, end)
        )


class UnresolvableIdentityError(IngestionError):
    """Raised when a change cannot be attributed to a known page identity.

    This usually means an earlier change that created or moved the page was
    never ingested, i.e. there is a coverage gap ending at ``timestamp``.
    """

    def __init__(self, title: str, timestamp: str) -> None:
        self.title = title
        self.timestamp = timestamp
        super().__init__(
            'cannot resolve a pageid for {0!r} at {1}; an earlier change '
            'record is missing'.format(title, timestamp)
        )


def normalize_timestamp(value: str) -> str:
    """Normalize a MediaWiki timestamp to canonical UTC ``...Z`` form."""
    parsed = datetime.strptime(value, _TS_FORMAT)
    return parsed.strftime(_TS_FORMAT)


def day_bounds(day: str) -> Tuple[str, str]:
    """Return the ``[start, end)`` UTC timestamps of a ``YYYY-MM-DD`` day."""
    start = datetime.strptime(day, _DAY_FORMAT)
    end = start + timedelta(days=1)
    return start.strftime(_TS_FORMAT), end.strftime(_TS_FORMAT)


def coerce_point_in_time(value: str) -> str:
    """Accept either a full timestamp or a ``YYYY-MM-DD`` day.

    A day is resolved to its first second so point lookups are deterministic.
    """
    if len(value) == 10:
        return day_bounds(value)[0]
    return normalize_timestamp(value)


@dataclass
class ChangeRecord:
    """One parsed recentchanges row, independent of how it is stored."""

    rcid: Optional[int]
    timestamp: str
    type: str
    namespace: int
    title: str
    pageid: Optional[int] = None
    revid: Optional[int] = None
    old_revid: Optional[int] = None
    logtype: Optional[str] = None
    logaction: Optional[str] = None
    log_id: Optional[int] = None
    target_title: Optional[str] = None
    target_namespace: Optional[int] = None
    comment: Optional[str] = None
    user: Optional[str] = None
    redirect: bool = False
    suppress_redirect: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_rc(cls, entry: Mapping[str, Any]) -> 'ChangeRecord':
        """Build a record from a raw ``recentchanges`` API row."""
        logparams = dict(entry.get('logparams') or {})
        target_title = logparams.get('target_title') or logparams.get('target')
        target_namespace = logparams.get('target_ns')
        return cls(
            rcid=entry.get('rcid'),
            timestamp=normalize_timestamp(entry['timestamp']),
            type=entry['type'],
            namespace=entry.get('ns', 0),
            title=entry['title'],
            pageid=entry.get('pageid'),
            revid=entry.get('revid'),
            old_revid=entry.get('old_revid'),
            logtype=entry.get('logtype'),
            logaction=entry.get('logaction'),
            log_id=entry.get('logid'),
            target_title=target_title,
            target_namespace=target_namespace,
            comment=entry.get('comment'),
            user=entry.get('user'),
            redirect='redirect' in entry,
            suppress_redirect=bool(logparams.get('suppressredirect')),
            raw=dict(entry),
        )

    @property
    def is_move(self) -> bool:
        return (
            self.type == RC_LOG
            and self.logtype == LOG_TYPE_MOVE
            and self.logaction in MOVE_LOG_ACTIONS
        )

    @property
    def is_deletion(self) -> bool:
        return (
            self.type == RC_LOG
            and self.logtype == LOG_TYPE_DELETE
            and self.logaction in DELETE_LOG_ACTIONS
        )

    @property
    def dedupe_key(self) -> Tuple[Any, ...]:
        """Identity of the feed row itself.

        Prefer the rcid; rows without one (rare log events) fall back to a
        composite of everything that makes the row unique.
        """
        if self.rcid is not None:
            return ('rcid', self.rcid)
        return (
            'composite', self.type, self.log_id, self.revid,
            self.timestamp, self.title,
        )


@dataclass
class TitleInterval:
    """A time range during which a title pointed at ``pageid``.

    ``pageid is None`` means the title is known to be vacant. ``redirect``
    marks an interval held by the redirect left behind by a move: the title
    is still attributable to the moved entity, not to a new one.
    """

    start: str
    end: Optional[str]
    pageid: Optional[int]
    redirect: bool = False


@dataclass
class ContentSnapshot:
    """The result of trying to obtain the body of one revision."""

    revid: Optional[int]
    timestamp: str
    state: str
    text: Optional[str] = None


@dataclass
class PageEntity:
    """One page identity (pageid) with its full ingested history."""

    pageid: int
    namespace: int
    created: Optional[str] = None
    deleted_at: Optional[str] = None
    inaccessible: bool = False
    current_title: Optional[str] = None
    snapshots: List[ContentSnapshot] = field(default_factory=list)
    changes: List[ChangeRecord] = field(default_factory=list)

    def snapshot_at(self, timestamp: str) -> Optional[ContentSnapshot]:
        """Return the latest snapshot not newer than ``timestamp``."""
        candidate = None
        for snapshot in self.snapshots:
            if snapshot.timestamp <= timestamp:
                candidate = snapshot
            else:
                break
        return candidate


class TitleIndex:
    """In-memory time-ranged map of title -> pageid."""

    def __init__(self) -> None:
        self._intervals: Dict[str, List[TitleInterval]] = {}

    def assign(
        self, title: str, start: str, pageid: int, redirect: bool = False
    ) -> TitleInterval:
        """Point ``title`` at ``pageid`` starting at ``start``."""
        intervals = self._intervals.setdefault(title, [])
        self._close_open(intervals, start)
        interval = TitleInterval(start=start, end=None, pageid=pageid,
                                 redirect=redirect)
        intervals.append(interval)
        return interval

    def mark_vacant(self, title: str, start: str) -> None:
        """Record that ``title`` holds no page from ``start`` onward."""
        intervals = self._intervals.setdefault(title, [])
        self._close_open(intervals, start)
        intervals.append(TitleInterval(start=start, end=None, pageid=None))

    @staticmethod
    def _close_open(intervals: List[TitleInterval], start: str) -> None:
        if intervals and intervals[-1].end is None and intervals[-1].start < start:
            intervals[-1].end = start

    def lookup(self, title: str, timestamp: str) -> Optional[TitleInterval]:
        for interval in self._intervals.get(title, ()):
            if interval.start <= timestamp and (
                interval.end is None or timestamp < interval.end
            ):
                return interval
        return None

    def latest_before(
        self, title: str, timestamp: str
    ) -> Optional[TitleInterval]:
        """Return the last interval that started before ``timestamp``.

        Unlike :meth:`lookup`, this also matches an interval that ends
        exactly at the boundary (e.g. the page being moved at that instant).
        """
        candidate = None
        for interval in self._intervals.get(title, ()):
            if interval.start < timestamp:
                candidate = interval
            else:
                break
        return candidate

    def gaps(self, title: str, start: str, end: str) -> List[Tuple[str, str]]:
        """Return the uncovered sub-ranges of ``[start, end)`` for a title.

        Vacant intervals count as covered: knowing a title had no page is an
        answer, not a gap.
        """
        cursor = start
        uncovered: List[Tuple[str, str]] = []
        for interval in self._intervals.get(title, ()):
            if interval.end is not None and interval.end <= start:
                continue
            if interval.start >= end:
                break
            interval_start = max(interval.start, start)
            if interval_start > cursor:
                uncovered.append((cursor, min(interval_start, end)))
            interval_end = interval.end if interval.end is not None else end
            cursor = max(cursor, min(interval_end, end))
        if cursor < end:
            uncovered.append((cursor, end))
        return uncovered

    def day_assignments(
        self, title: str, day: str
    ) -> Tuple[List[TitleInterval], List[Tuple[str, str]]]:
        """Return clamped intervals and uncovered gaps for one day."""
        start, end = day_bounds(day)
        assignments = []
        for interval in self._intervals.get(title, ()):
            if interval.end is not None and interval.end <= start:
                continue
            if interval.start >= end:
                break
            clamped = TitleInterval(
                start=max(interval.start, start),
                end=min(interval.end, end) if interval.end is not None else end,
                pageid=interval.pageid,
                redirect=interval.redirect,
            )
            assignments.append(clamped)
        return assignments, self.gaps(title, start, end)

    def to_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        return {
            title: [asdict(interval) for interval in intervals]
            for title, intervals in self._intervals.items()
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, List[Mapping[str, Any]]]) -> 'TitleIndex':
        index = cls()
        for title, intervals in data.items():
            index._intervals[title] = [
                TitleInterval(**interval) for interval in intervals
            ]
        return index


@dataclass
class TitleResolution:
    """Result of asking what a title pointed at during a day."""

    title: str
    day: str
    assignments: List[TitleInterval]
    gaps: List[Tuple[str, str]]

    @property
    def is_complete(self) -> bool:
        return not self.gaps

    @property
    def pageids(self) -> List[int]:
        return [
            interval.pageid
            for interval in self.assignments
            if interval.pageid is not None
        ]


@dataclass
class SyncSummary:
    """Counts and watermarks produced by one :meth:`PageStore.sync` run."""

    start: Optional[str]
    end: Optional[str]
    applied: int = 0
    duplicates: int = 0
    cursor: Optional[str] = None


class PageStore:
    """Identity-aware, incrementally fed knowledge base.

    Feed it parsed recentchanges rows in chronological order. Replays are
    safe: rows already seen are counted as duplicates and skipped, so
    overlapping fetch windows at the watermark boundary do not create
    duplicate entities or title intervals.
    """

    def __init__(
        self,
        permission_error_codes: Sequence[str] = DEFAULT_PERMISSION_ERROR_CODES,
    ) -> None:
        self.pages: Dict[int, PageEntity] = {}
        self.title_index = TitleIndex()
        self.seen: Set[Tuple[Any, ...]] = set()
        self.cursor: Optional[str] = None
        self.applied_count = 0
        self.duplicate_count = 0
        self.permission_error_codes = frozenset(permission_error_codes)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def apply_change(self, change: ChangeRecord) -> bool:
        """Apply one change record.

        Returns ``False`` (without mutating the store) when the row was
        already ingested, e.g. a move notification replayed at the boundary
        between two incremental fetch windows.
        """
        key = change.dedupe_key
        if key in self.seen:
            self.duplicate_count += 1
            logger.info('ignoring duplicate recentchanges row %r', key)
            return False

        if change.type == RC_NEW:
            self._apply_new(change)
        elif change.is_move:
            self._apply_move(change)
        elif change.is_deletion:
            self._apply_deletion(change)
        else:
            self._apply_generic(change)

        self.seen.add(key)
        self.applied_count += 1
        if self.cursor is None or change.timestamp > self.cursor:
            self.cursor = change.timestamp
        return True

    def apply_changes(self, changes: Iterable[ChangeRecord]) -> int:
        return sum(1 for change in changes if self.apply_change(change))

    def _apply_new(self, change: ChangeRecord) -> None:
        entity = self._entity_for(change)
        if entity.created is None:
            entity.created = change.timestamp
        entity.deleted_at = None
        entity.current_title = change.title
        entity.changes.append(change)
        self.title_index.assign(change.title, change.timestamp, entity.pageid)

    def _apply_move(self, change: ChangeRecord) -> None:
        if not change.target_title:
            raise UnresolvableIdentityError(change.title, change.timestamp)
        # The rc row's pageid may identify the freshly created redirect
        # rather than the moved page. Resolve the moved entity through the
        # source title's previous assignment first.
        entity = None
        prior = self.title_index.latest_before(change.title, change.timestamp)
        if prior is not None and prior.pageid is not None:
            entity = self.pages.get(prior.pageid)
        if entity is None:
            entity = self._entity_for(change)
        entity.changes.append(change)
        entity.current_title = change.target_title

        # The source title stops naming the page at the move timestamp.
        # Unless the redirect was suppressed, the title still resolves to the
        # same (moved) entity through the leftover redirect; it must not be
        # treated as a new page identity.
        if change.suppress_redirect:
            self.title_index.mark_vacant(change.title, change.timestamp)
        else:
            self.title_index.assign(
                change.title, change.timestamp, entity.pageid, redirect=True
            )
        self.title_index.assign(
            change.target_title, change.timestamp, entity.pageid
        )

    def _apply_deletion(self, change: ChangeRecord) -> None:
        interval = self.title_index.lookup(change.title, change.timestamp)

        # Deleting the leftover redirect must neither delete the moved page
        # nor spawn an entity for the redirect: fold the record into the
        # moved entity's history and just vacate the title.
        if (
            interval is not None
            and interval.redirect
            and interval.pageid is not None
            and change.pageid not in self.pages
        ):
            entity = self.pages[interval.pageid]
            entity.changes.append(change)
            self.title_index.mark_vacant(change.title, change.timestamp)
            return

        entity = self._entity_for(change)
        entity.changes.append(change)

        # A non-redirect assignment is the entity itself; deleting the
        # redirect that points at it must not mark the entity deleted.
        if interval is None or not interval.redirect:
            entity.deleted_at = change.timestamp
        self.title_index.mark_vacant(change.title, change.timestamp)

    def _apply_generic(self, change: ChangeRecord) -> None:
        interval = self.title_index.lookup(change.title, change.timestamp)
        if (
            interval is not None
            and interval.redirect
            and interval.pageid is not None
            and change.pageid not in self.pages
        ):
            # Edits to the leftover redirect are folded into the moved
            # entity instead of creating a phantom page identity.
            entity = self.pages[interval.pageid]
        else:
            entity = self._entity_for(change)
        entity.changes.append(change)
        if change.pageid is not None:
            current = self.title_index.lookup(change.title, change.timestamp)
            if current is None or current.pageid != entity.pageid:
                self.title_index.assign(
                    change.title, change.timestamp, entity.pageid
                )
            entity.current_title = change.title

    def _entity_for(self, change: ChangeRecord) -> PageEntity:
        if change.pageid is not None:
            return self.pages.setdefault(
                change.pageid,
                PageEntity(pageid=change.pageid, namespace=change.namespace),
            )
        interval = self.title_index.lookup(change.title, change.timestamp)
        if interval is not None and interval.pageid is not None:
            entity = self.pages.get(interval.pageid)
            if entity is not None:
                return entity
        raise UnresolvableIdentityError(change.title, change.timestamp)

    # ------------------------------------------------------------------
    # Fetching from the wiki
    # ------------------------------------------------------------------
    def fetch_changes(
        self,
        site: Any,
        start: str,
        end: Optional[str] = None,
        namespace: Optional[int] = None,
        api_chunk_size: int = 500,
    ) -> List[ChangeRecord]:
        """Fetch a recentchanges window in chronological order."""
        entries = site.recentchanges(
            start=start,
            end=end,
            dir='newer',
            namespace=namespace,
            prop='ids|title|timestamp|redirect|comment|user|loginfo|flags',
            type='{0}|{1}|{2}'.format(RC_EDIT, RC_NEW, RC_LOG),
            api_chunk_size=api_chunk_size,
        )
        return [ChangeRecord.from_rc(entry) for entry in entries]

    def _verify_watermark_coverage(self, site: Any, watermark: str) -> None:
        """Make sure the watermark row is still inside recentchanges retention.

        A silent period (no rows after the watermark) is normal and not a
        gap, so the first fetched row being newer than the watermark proves
        nothing. Instead, probe the oldest row the feed still carries: if it
        is newer than the watermark (or the feed is empty), the watermark
        itself has aged out and any missed rows can no longer be recovered
        from recentchanges.
        """
        probe = list(
            site.recentchanges(
                start=_EPOCH,
                dir='newer',
                max_items=1,
                prop='timestamp',
            )
        )
        if not probe:
            raise GapError(watermark, watermark)
        retained_from = probe[0]['timestamp']
        if retained_from > watermark:
            raise GapError(watermark, retained_from)

    def sync(
        self,
        site: Any,
        start: Optional[str] = None,
        end: Optional[str] = None,
        namespace: Optional[int] = None,
    ) -> SyncSummary:
        """Incrementally ingest everything since the stored watermark.

        The watermark row is re-fetched on every run and de-duplicated, so
        callers do not need to add a second to ``start``. A
        :class:`GapError` is raised when recentchanges retention no longer
        reaches back to the watermark; the missing range then has to be
        backfilled from other sources.
        """
        window_start = normalize_timestamp(start) if start else self.cursor
        if window_start is None:
            raise ValueError(
                'no ingestion watermark stored; pass an explicit start on '
                'the first sync run'
            )
        if start is None and self.cursor is not None:
            self._verify_watermark_coverage(site, window_start)

        applied_before = self.applied_count
        duplicates_before = self.duplicate_count

        changes = self.fetch_changes(site, window_start, end, namespace)
        for change in changes:
            self.apply_change(change)

        return SyncSummary(
            start=window_start,
            end=end,
            applied=self.applied_count - applied_before,
            duplicates=self.duplicate_count - duplicates_before,
            cursor=self.cursor,
        )

    def fetch_snapshot(self, site: Any, change: ChangeRecord) -> ContentSnapshot:
        """Obtain the body text for a change, classifying readability.

        * A recorded delete log -> :data:`ReadState.DELETED` without any
          further API call.
        * A permission-style API error, or a ``missing`` response with no
          delete log on record -> :data:`ReadState.INACCESSIBLE`. The store
          never conflates "we cannot read it" with "it was deleted".
        * A successful response -> :data:`ReadState.VISIBLE` with the text.
        """
        if change.is_deletion:
            return ContentSnapshot(
                revid=change.revid,
                timestamp=change.timestamp,
                state=ReadState.DELETED,
            )
        if change.revid is None:
            return ContentSnapshot(
                revid=None,
                timestamp=change.timestamp,
                state=ReadState.INACCESSIBLE,
            )

        try:
            response = site.get(
                'query',
                prop='revisions',
                revids=change.revid,
                rvprop='ids|timestamp|content',
                rvslots='main',
            )
        except mwclient.errors.APIError as error:
            if error.code in self.permission_error_codes:
                return ContentSnapshot(
                    revid=change.revid,
                    timestamp=change.timestamp,
                    state=ReadState.INACCESSIBLE,
                )
            raise

        pages = response.get('query', {}).get('pages', {})
        page = next(iter(pages.values()), {}) if pages else {}
        revisions = page.get('revisions') or ()
        if 'missing' in page or not revisions:
            # Missing without a delete-log record is not proven deletion:
            # the revision may simply be hidden from this account.
            return ContentSnapshot(
                revid=change.revid,
                timestamp=change.timestamp,
                state=ReadState.INACCESSIBLE,
            )

        revision = revisions[0]
        if 'slots' in revision:
            text = revision['slots']['main'].get('*')
        else:
            text = revision.get('*')
        return ContentSnapshot(
            revid=revision.get('revid', change.revid),
            timestamp=revision.get('timestamp', change.timestamp),
            state=ReadState.VISIBLE,
            text=text,
        )

    def attach_snapshot(
        self, site: Any, change: ChangeRecord
    ) -> ContentSnapshot:
        """Fetch a snapshot and attach it to the change's page entity."""
        snapshot = self.fetch_snapshot(site, change)
        entity = self._entity_for(change)
        if snapshot.state == ReadState.INACCESSIBLE:
            entity.inaccessible = True
        if not entity.snapshots or entity.snapshots[-1].revid != snapshot.revid:
            entity.snapshots.append(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def resolve_title(self, title: str, day: str) -> TitleResolution:
        """Explain what ``title`` pointed at on ``day`` (``YYYY-MM-DD``).

        The returned resolution is either complete (every second of the day
        is covered by an assignment or a known vacancy) or carries the exact
        sub-ranges whose change records are still missing.
        """
        assignments, gaps = self.title_index.day_assignments(title, day)
        return TitleResolution(
            title=title, day=day, assignments=assignments, gaps=gaps
        )

    def page_at(self, title: str, when: str) -> Optional[PageEntity]:
        """Point lookup of the entity behind a title at a timestamp.

        A ``YYYY-MM-DD`` day is accepted and resolved to its first second;
        for whole-day questions (which may span several assignments, e.g.
        a move on that day) use :meth:`resolve_title` instead.

        Raises :class:`GapError` when the title's state at that moment is
        unknown, so callers can always distinguish "points at X" from "a
        change record is still owed". A vacant title returns ``None``.
        """
        timestamp = coerce_point_in_time(when)
        interval = self.title_index.lookup(title, timestamp)
        if interval is None:
            raise GapError(timestamp, timestamp)
        if interval.pageid is None:
            return None
        return self.pages[interval.pageid]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            'pages': {
                str(pageid): {
                    'pageid': entity.pageid,
                    'namespace': entity.namespace,
                    'created': entity.created,
                    'deleted_at': entity.deleted_at,
                    'inaccessible': entity.inaccessible,
                    'current_title': entity.current_title,
                    'snapshots': [asdict(item) for item in entity.snapshots],
                    'changes': [asdict(change) for change in entity.changes],
                }
                for pageid, entity in self.pages.items()
            },
            'titles': self.title_index.to_dict(),
            'seen': [list(key) for key in sorted(self.seen, key=str)],
            'cursor': self.cursor,
            'applied_count': self.applied_count,
            'duplicate_count': self.duplicate_count,
        }

    def save(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'PageStore':
        store = cls()
        store.title_index = TitleIndex.from_dict(data['titles'])
        for raw_pageid, payload in data['pages'].items():
            changes = [
                ChangeRecord(**change) for change in payload.get('changes', ())
            ]
            snapshots = [
                ContentSnapshot(**snapshot)
                for snapshot in payload.get('snapshots', ())
            ]
            entity = PageEntity(
                pageid=int(raw_pageid),
                namespace=payload['namespace'],
                created=payload.get('created'),
                deleted_at=payload.get('deleted_at'),
                inaccessible=payload.get('inaccessible', False),
                current_title=payload.get('current_title'),
                snapshots=snapshots,
                changes=changes,
            )
            store.pages[entity.pageid] = entity
        store.seen = {tuple(key) for key in data.get('seen', ())}
        store.cursor = data.get('cursor')
        store.applied_count = data.get('applied_count', 0)
        store.duplicate_count = data.get('duplicate_count', 0)
        return store

    @classmethod
    def load(cls, path: str) -> 'PageStore':
        with open(path, 'r', encoding='utf-8') as handle:
            return cls.from_dict(json.load(handle))
