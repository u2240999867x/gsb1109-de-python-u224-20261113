"""Template-change impact analysis and bounded re-crawl planning.

When a shared template is edited, every entry page that expands the template
may have to be re-crawled. Transclusion is indirect: a page can pull in the
changed template through one or more intermediate templates, so the reference
graph has to be walked outwards from the edited template.

The functions in this module implement four rules that matter in practice:

* Traversal is bounded by a template nesting depth, so two layers of indirect
  references are covered without walking the whole wiki.
* Static transclusion over-approximates expansion: parameter conditions can
  keep a reference branch from ever expanding. A *complete* crawl record that
  proves the branch did not expand justifies skipping the page.
* A page whose previous crawl did not record its dependencies completely can
  never be safe-skipped, because there is no evidence either way.
* Re-crawls are settled against one frozen batch of template revisions. A
  page already harvested with every required revision in that batch is not
  claimed again.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Collection, Dict, List, Mapping, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class TemplateRevision:
    """A template title pinned to a specific revision."""

    title: str
    revision_id: int


@dataclass(frozen=True)
class DependencyRecord:
    """One template dependency observed while crawling a page.

    Args:
        template: Title of the referenced template.
        revision_id: Revision of the template that was expanded, or ``None``
            when the crawl did not record it. Such a record is incomplete and
            cannot be used to prove the page is up to date.
        expanded: Whether the reference branch was actually expanded. A
            reference present in the wikitext may stay unexpanded because of
            a parameter condition.
    """

    template: str
    revision_id: Optional[int]
    expanded: bool = True


@dataclass(frozen=True)
class ChangePath:
    """A transclusion chain from the edited template to one affected page.

    The first element is the edited template, the last element is the
    harvested page and the elements in between are intermediate templates.
    """

    edges: Tuple[str, ...]

    @property
    def page(self) -> str:
        return self.edges[-1]


@dataclass(frozen=True)
class RefreshJob:
    """A page that must be re-crawled, with every path that reaches it."""

    page: str
    reason: str
    paths: Tuple[ChangePath, ...]
    batch: Tuple[TemplateRevision, ...]


@dataclass(frozen=True)
class SettlementResult:
    """Outcome of settling one re-crawl against the frozen revision batch."""

    page: str
    settled: bool
    stale: Tuple[DependencyRecord, ...]
    missing_revision: Tuple[DependencyRecord, ...]

    @property
    def blocking(self) -> Tuple[DependencyRecord, ...]:
        return self.stale + self.missing_revision


class ReferenceGraph(ABC):
    """Read-only view of the transclusion graph and current revisions."""

    @abstractmethod
    def embedded_in(self, title: str) -> Collection[str]:
        """Return every page or template that statically transcludes *title*."""

    @abstractmethod
    def is_template(self, title: str) -> bool:
        """Return whether *title* is a template rather than an entry page."""

    @abstractmethod
    def current_revision(self, title: str) -> int:
        """Return the current revision id of *title*."""


class CrawlStore(ABC):
    """Storage for the dependency records produced by previous crawls."""

    @abstractmethod
    def get_page_revision(self, page: str) -> Optional[int]:
        """Return the page revision that was harvested, or ``None``."""

    @abstractmethod
    def get_dependencies(
        self, page: str
    ) -> Optional[Sequence[DependencyRecord]]:
        """Return the recorded dependencies.

        ``None`` means the page was harvested without any usable dependency
        record, which is itself an incomplete crawl. An empty sequence means
        the page was fully recorded and simply has no template dependencies.
        """

    @abstractmethod
    def record_crawl(
        self,
        page: str,
        page_revision: int,
        dependencies: Sequence[DependencyRecord],
    ) -> None:
        """Persist the records of a freshly settled crawl."""


class MemoryCrawlStore(CrawlStore):
    """An in-memory :class:`CrawlStore`, useful for tests and batch runs."""

    def __init__(self) -> None:
        self._page_revisions: Dict[str, int] = {}
        self._dependencies: Dict[str, List[DependencyRecord]] = {}

    def get_page_revision(self, page: str) -> Optional[int]:
        return self._page_revisions.get(page)

    def get_dependencies(
        self, page: str
    ) -> Optional[Sequence[DependencyRecord]]:
        if page not in self._dependencies:
            return None
        return tuple(self._dependencies[page])

    def record_crawl(
        self,
        page: str,
        page_revision: int,
        dependencies: Sequence[DependencyRecord],
    ) -> None:
        self._page_revisions[page] = page_revision
        self._dependencies[page] = list(dependencies)


def _is_incomplete(records: Optional[Sequence[DependencyRecord]]) -> bool:
    if records is None:
        return True
    return any(
        record.expanded and record.revision_id is None for record in records
    )


def _collect_paths(
    changed_template: str,
    graph: ReferenceGraph,
    max_depth: int,
) -> Tuple[Dict[str, Set[Tuple[str, ...]]], Dict[str, int]]:
    """Walk the transclusion graph outwards from the edited template.

    Returns a mapping of entry page to the set of change paths reaching it
    and the frozen batch of every template revision visited.
    """
    batch: Dict[str, int] = {
        changed_template: graph.current_revision(changed_template)
    }
    paths_by_page: Dict[str, Set[Tuple[str, ...]]] = {}
    seen_templates: Set[str] = set()
    frontier: List[Tuple[str, Tuple[str, ...]]] = [
        (changed_template, (changed_template,))
    ]

    for _ in range(max_depth):
        next_frontier: List[Tuple[str, Tuple[str, ...]]] = []
        for template, path in frontier:
            if template in seen_templates:
                continue
            seen_templates.add(template)
            if template not in batch:
                batch[template] = graph.current_revision(template)
            for referrer in graph.embedded_in(template):
                new_path = path + (referrer,)
                if graph.is_template(referrer):
                    # The revision is pinned only if the depth bound lets
                    # this template be expanded in a later iteration.
                    next_frontier.append((referrer, new_path))
                else:
                    paths_by_page.setdefault(referrer, set()).add(new_path)
        frontier = next_frontier
        if not frontier:
            break

    return paths_by_page, batch


def _freeze_batch(batch: Mapping[str, int]) -> Tuple[TemplateRevision, ...]:
    return tuple(
        TemplateRevision(title=title, revision_id=revision_id)
        for title, revision_id in sorted(batch.items())
    )


def plan_refresh(
    changed_template: str,
    graph: ReferenceGraph,
    store: CrawlStore,
    *,
    max_depth: int = 2,
    claimed_pages: Collection[str] = (),
) -> List[RefreshJob]:
    """Plan the re-crawl triggered by an edit to *changed_template*.

    Every entry page reachable through at most *max_depth* layers of template
    transclusion is considered. Previously harvested pages fall into three
    groups:

    * pages with incomplete dependency records, including records that cannot
      account for every template on a change path, are always claimed, since
      skipping them cannot be justified;
    * pages whose complete records show an expanded dependency older than the
      frozen batch are claimed as stale;
    * pages already harvested at the batch revisions, or whose complete
      records prove every reference branch never expanded, are skipped.

    Pages never harvested and titles in *claimed_pages* are not returned.

    Returns:
        A deterministic list of :class:`RefreshJob` objects, each carrying
        every change path that reaches the page and the same frozen batch of
        template revisions.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    paths_by_page, batch = _collect_paths(
        changed_template, graph, max_depth
    )
    frozen_batch = _freeze_batch(batch)
    claimed = set(claimed_pages)
    jobs: List[RefreshJob] = []

    for page in sorted(paths_by_page):
        if page in claimed or store.get_page_revision(page) is None:
            continue

        paths = tuple(
            ChangePath(edges=edges)
            for edges in sorted(paths_by_page[page])
        )
        records = store.get_dependencies(page)

        if _is_incomplete(records):
            jobs.append(
                RefreshJob(
                    page=page,
                    reason="incomplete-dependencies",
                    paths=paths,
                    batch=frozen_batch,
                )
            )
            continue

        recorded: Dict[str, List[DependencyRecord]] = {}
        for record in records:
            recorded.setdefault(record.template, []).append(record)

        incomplete = False
        stale = False
        for path in paths:
            for template in path.edges[:-1]:
                entries = recorded.get(template)
                if entries is None:
                    # A complete record set missing a template on the
                    # change path cannot prove the branch was harmless.
                    incomplete = True
                    break
                if any(
                    entry.expanded
                    and (
                        entry.revision_id is None
                        or entry.revision_id < batch[template]
                    )
                    for entry in entries
                ):
                    stale = True
                    break
            if incomplete or stale:
                break

        if incomplete:
            reason = "incomplete-dependencies"
        elif stale:
            reason = "stale-template-revision"
        else:
            continue

        jobs.append(
            RefreshJob(
                page=page,
                reason=reason,
                paths=paths,
                batch=frozen_batch,
            )
        )

    return jobs


def settle_crawl(
    page: str,
    page_revision: int,
    dependencies: Sequence[DependencyRecord],
    batch: Sequence[TemplateRevision],
    store: CrawlStore,
) -> SettlementResult:
    """Settle one re-crawl against the frozen template revision batch.

    The crawl is accepted only when every expanded dependency that belongs to
    *batch* was rendered at a revision no older than the pinned one and every
    expanded record actually carries a revision id. On success the records
    are persisted in *store*, so a subsequent :func:`plan_refresh` call skips
    the page without claiming it again.
    """
    required = {item.title: item.revision_id for item in batch}
    stale: List[DependencyRecord] = []
    missing: List[DependencyRecord] = []

    for record in dependencies:
        if record.template not in required or not record.expanded:
            continue
        if record.revision_id is None:
            missing.append(record)
        elif record.revision_id < required[record.template]:
            stale.append(record)

    result = SettlementResult(
        page=page,
        settled=not stale and not missing,
        stale=tuple(stale),
        missing_revision=tuple(missing),
    )
    if result.settled:
        store.record_crawl(page, page_revision, dependencies)
    return result
