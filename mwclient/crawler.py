from typing import (  # noqa: F401
    Any, Dict, Iterable, List, Set, Tuple
)

import mwclient.listing
import mwclient.page

CATEGORY_NAMESPACE = 14

# A path is the chain of page titles, starting at a root category and
# ending at the reached page, e.g. ('Category:A', 'Category:B', 'Page').
Path = Tuple[str, ...]


class CrawlResult:
    """Snapshot of a crawl, delivered by :meth:`CategoryCrawler.crawl`.

    Attributes:
        catalog: Maps every reached page title to a dict of root category
            title to the list of shortest paths from that root. All paths
            of equal (minimal) length are included.
        content: Maps each reached non-category page title to its wikitext.
            Every page is fetched at most once per crawler, no matter how
            many roots or paths reach it.
        empty: Category titles that were expanded and proven to have no
            members.
        unexpanded: Category titles that were reached at the depth limit
            and therefore never expanded. This is always disjoint from
            `empty`: an unexpanded category may or may not be empty.
    """

    def __init__(
        self,
        catalog: Dict[str, Dict[str, List[Path]]],
        namespaces: Dict[str, int],
        content: Dict[str, Any],
        empty: Set[str],
        unexpanded: Set[str]
    ) -> None:
        self.catalog = catalog
        self.content = content
        self.empty = empty
        self.unexpanded = unexpanded
        self._namespaces = namespaces

    @property
    def pages(self) -> Dict[str, Dict[str, List[Path]]]:
        """Catalog entries that are not categories, i.e. actual articles."""
        return {
            title: paths for title, paths in self.catalog.items()
            if self._namespaces.get(title) != CATEGORY_NAMESPACE
        }

    @property
    def categories(self) -> Dict[str, Dict[str, List[Path]]]:
        """Catalog entries in the category namespace."""
        return {
            title: paths for title, paths in self.catalog.items()
            if self._namespaces.get(title) == CATEGORY_NAMESPACE
        }

    def __repr__(self) -> str:
        return (
            f'<{self.__class__.__name__} '
            f'pages={len(self.pages)} categories={len(self.categories)} '
            f'empty={len(self.empty)} unexpanded={len(self.unexpanded)}>'
        )


class CategoryCrawler:
    """Crawl wiki categories from one or more root categories.

    Several roots ("entry points") may share pages and subcategories, and
    subcategories may point back to categories closer to the root. The
    crawler fetches every category member listing and every page text at
    most once, while the resulting catalog still records, for each root,
    the shortest paths by which every page was reached.

    Args:
        site (mwclient.client.Site): The site to crawl.
        roots (Iterable[str]): Root category titles. A leading
            ``Category:`` prefix is added when no namespace is given.
        max_depth (int): Maximum depth of categories to expand. Roots are
            at depth 0, so with ``max_depth=1`` only the roots themselves
            are expanded and their subcategories are reported as
            unexpanded.
        fetch_content (bool): Fetch the wikitext of every reached
            non-category page exactly once (default: `True`).

    Examples:
        >>> crawler = CategoryCrawler(site, ['Category:Agriculture'], max_depth=2)
        >>> result = crawler.crawl()
        >>> crawler.add_root('Category:Gazetteers')
        >>> result = crawler.crawl()  # reuses cached member listings
    """

    def __init__(
        self,
        site: 'mwclient.client.Site',  # noqa: F821
        roots: Iterable[str] = (),
        max_depth: int = 3,
        fetch_content: bool = True
    ) -> None:
        self.site = site
        self.max_depth = max_depth
        self.fetch_content = fetch_content

        self._roots = []  # type: List[str]
        self._crawled = set()  # type: Set[str]
        self._member_cache = {}  # type: Dict[str, List[Tuple[str, int]]]
        self._content = {}  # type: Dict[str, Any]
        self._catalog = {}  # type: Dict[str, Dict[str, List[Path]]]
        self._namespaces = {}  # type: Dict[str, int]
        self._expanded = set()  # type: Set[str]
        self._hit_limit = set()  # type: Set[str]
        self._empty = set()  # type: Set[str]

        for root in roots:
            self.add_root(root)

    @property
    def roots(self) -> List[str]:
        """The registered root category titles, in registration order."""
        return list(self._roots)

    def add_root(self, name: str) -> str:
        """Register an additional root category and return its title.

        The root is only queued; call :meth:`crawl` to traverse it. Roots
        that were already crawled are never traversed again, and member
        listings fetched for earlier roots are reused from the cache, so
        adding a root that overlaps with previous ones saves requests
        without losing the new root's attribution in the catalog.
        """
        name = self._normalize_category(name)
        if name not in self._roots:
            self._roots.append(name)
        return name

    def crawl(self) -> CrawlResult:
        """Traverse all roots that have not been crawled yet.

        Returns:
            CrawlResult: A snapshot of the full catalog, including
            attribution for every root crawled so far.
        """
        for root in self._roots:
            if root in self._crawled:
                continue
            self._crawl_root(root)
            self._crawled.add(root)
        return self._snapshot()

    def _normalize_category(self, name: str) -> str:
        namespaces = getattr(self.site, 'namespaces', None) or {}
        prefix = namespaces.get(CATEGORY_NAMESPACE, 'Category')
        if ':' not in name:
            name = f'{prefix}:{name}'
        return name

    def _crawl_root(self, root: str) -> None:
        depth_of = {root: 0}  # type: Dict[str, int]
        frontier = {root: [(root,)]}  # type: Dict[str, List[Path]]
        self._record(root, CATEGORY_NAMESPACE, root, (root,))

        depth = 0
        while frontier:
            if depth >= self.max_depth:
                self._hit_limit.update(frontier)
                break
            next_frontier = {}  # type: Dict[str, List[Path]]
            for name, paths in frontier.items():
                members = self._members(name)
                if not members:
                    self._empty.add(name)
                for title, namespace in members:
                    for path in paths:
                        self._reach(
                            root, title, namespace, path + (title,),
                            depth + 1, depth_of, next_frontier
                        )
            frontier = next_frontier
            depth += 1

    def _reach(
        self,
        root: str,
        title: str,
        namespace: int,
        path: Path,
        depth: int,
        depth_of: Dict[str, int],
        next_frontier: Dict[str, List[Path]]
    ) -> None:
        known = depth_of.get(title)
        if known is not None and known < depth:
            # Already reached from this root via a shorter path; this also
            # breaks cycles where a subcategory points back to an ancestor.
            return
        depth_of[title] = depth
        self._record(title, namespace, root, path)
        if namespace == CATEGORY_NAMESPACE:
            next_frontier.setdefault(title, [])
            if path not in next_frontier[title]:
                next_frontier[title].append(path)
        elif self.fetch_content:
            self._fetch_content(title)

    def _record(self, title: str, namespace: int, root: str, path: Path) -> None:
        self._namespaces.setdefault(title, namespace)
        paths = self._catalog.setdefault(title, {}).setdefault(root, [])
        if path not in paths:
            paths.append(path)

    def _members(self, category: str) -> List[Tuple[str, int]]:
        if category not in self._member_cache:
            listing = mwclient.listing.List(
                self.site, 'categorymembers', 'cm', cmtitle=category
            )
            self._member_cache[category] = [
                (item['title'], item.get('ns', 0)) for item in listing
            ]
            self._expanded.add(category)
        return self._member_cache[category]

    def _fetch_content(self, title: str) -> None:
        if title not in self._content:
            page = mwclient.page.Page(self.site, title)
            self._content[title] = page.text()

    def _snapshot(self) -> CrawlResult:
        return CrawlResult(
            catalog={
                title: {root: list(paths) for root, paths in by_root.items()}
                for title, by_root in self._catalog.items()
            },
            namespaces=dict(self._namespaces),
            content=dict(self._content),
            empty=set(self._empty),
            unexpanded=self._hit_limit - self._expanded,
        )
