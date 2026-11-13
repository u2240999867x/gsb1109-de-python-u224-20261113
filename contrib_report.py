"""Monthly contributor statistics with provable bounds and anonymity.

This module extracts revision records with mwclient and produces a monthly
public report on distinct-contributor counts.  The privacy and audit model:

* Records may lack an account name (revision-deleted user, or anonymous
  edits) and may lack an edit summary.  Both are tolerated.
* A shared anonymous display name (e.g. an IP address) never proves that
  two records come from the same person, so anonymous records are never
  auto-merged.
* Named accounts auto-merge: two records carrying the same account name
  are treated as the same contributor.
* Administrators may supply approved "same person" / "different person"
  relations between revisions (referenced by public revision ids only).
* The true contributor count for a clean month is published as a provable
  range [min, max]: every record group linked by "same" evidence must be
  one person (upper bound = number of groups), and the "different person"
  relations force a chromatic-number lower bound on any merging.
* Relations can contradict each other transitively through intermediate
  records (A same as B, B same as C, but A declared different from C).
  For any month touched by such a contradiction no count is published;
  instead the report carries the locatable revision combination (revision
  ids and the link reasons) so an administrator can resolve it.
* Public output never contains account names, only revision ids.
"""

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

SAME = 'same'
DIFFERENT = 'different'

_REASON_ACCOUNT = 'same_account'
_REASON_ADMIN = 'admin_same'

# Step budget for the exact chromatic-number search.  If the search does
# not finish within the budget, the report falls back to a certified
# clique lower bound and marks the minimum as inexact.
_COLOR_STEP_BUDGET = 200000


@dataclass(frozen=True)
class RevisionRecord:
    """One revision, stripped down to what the analysis needs."""

    revid: int
    timestamp: str
    user: Optional[str]
    anon: bool
    comment: Optional[str]

    @property
    def month(self) -> str:
        return self.timestamp[:7]


@dataclass(frozen=True)
class Relation:
    """An administrator-approved relation between two revisions."""

    kind: str  # SAME or DIFFERENT
    revids: Tuple[int, int]


@dataclass
class Conflict:
    """A different-person relation contradicted by a same-person chain."""

    different: Tuple[int, int]
    same_chain: List[int]
    links: List[Dict[str, object]]


@dataclass
class MonthReport:
    month: str
    status: str  # 'ok' or 'contradiction'
    revision_count: int
    records_without_account: int
    records_without_comment: int
    min_contributors: Optional[int] = None
    max_contributors: Optional[int] = None
    min_is_exact: bool = True
    components: Optional[List[List[int]]] = None
    coloring: Optional[List[List[int]]] = None
    conflicts: List[Conflict] = field(default_factory=list)


def record_from_api(entry: Dict[str, object]) -> RevisionRecord:
    """Build a record from a raw API revision dict.

    Revisions with a suppressed user or comment simply lack the
    corresponding key, so missing values are normal, not errors.
    """
    user = entry.get('user')
    anon = 'anon' in entry and entry['anon'] not in (None, False)
    return RevisionRecord(
        revid=int(entry['revid']),  # type: ignore[arg-type]
        timestamp=str(entry['timestamp']),
        user=str(user) if user is not None else None,
        anon=anon,
        comment=(
            str(entry['comment']) if entry.get('comment') is not None else None
        ),
    )


def fetch_records(
    site: 'object',
    pages: Sequence[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_items: Optional[int] = None,
) -> List[RevisionRecord]:
    """Fetch revision records for the given pages via mwclient."""
    records = []
    for title in pages:
        page = site.pages[title]  # type: ignore[attr-defined]
        for entry in page.revisions(
            start=start,
            end=end,
            dir='newer',
            prop='ids|timestamp|flags|comment|user',
            max_items=max_items,
        ):
            records.append(record_from_api(entry))
    return records


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _build_same_links(
    records: Sequence[RevisionRecord],
    relations: Sequence[Relation],
    known_revids: Set[int],
) -> Tuple[_UnionFind, Dict[int, List[Tuple[int, str]]]]:
    """Union same-person evidence and keep a link graph for evidence chains.

    Named accounts merge; anonymous display names deliberately do not.
    """
    uf = _UnionFind()
    links: Dict[int, List[Tuple[int, str]]] = {r.revid: [] for r in records}

    def add_link(a: int, b: int, reason: str) -> None:
        uf.union(a, b)
        links[a].append((b, reason))
        links[b].append((a, reason))

    by_account: Dict[str, List[RevisionRecord]] = {}
    for record in records:
        if record.user is not None and not record.anon:
            by_account.setdefault(record.user, []).append(record)
    for group in by_account.values():
        group.sort(key=lambda r: (r.timestamp, r.revid))
        for earlier, later in zip(group, group[1:]):
            add_link(earlier.revid, later.revid, _REASON_ACCOUNT)

    for relation in relations:
        if relation.kind != SAME:
            continue
        a, b = relation.revids
        if a in known_revids and b in known_revids:
            add_link(a, b, _REASON_ADMIN)

    return uf, links


def _same_chain(
    links: Dict[int, List[Tuple[int, str]]], start: int, goal: int
) -> Tuple[List[int], List[Dict[str, object]]]:
    """Shortest same-person path between two revisions, for evidence."""
    prev: Dict[int, Tuple[int, str]] = {}
    visited = {start}
    queue: deque = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            break
        for neighbor, reason in links.get(node, []):
            if neighbor not in visited:
                visited.add(neighbor)
                prev[neighbor] = (node, reason)
                queue.append(neighbor)
    chain = [goal]
    while chain[-1] != start:
        chain.append(prev[chain[-1]][0])
    chain.reverse()
    path_links = []
    for a, b in zip(chain, chain[1:]):
        reason = next(r for n, r in links[a] if n == b)
        path_links.append({'from': a, 'to': b, 'reason': reason})
    return chain, path_links


def _clique_lower_bound(adj: List[Set[int]]) -> int:
    """Largest greedily-found clique size; a certified lower bound."""
    best = 0
    for start in range(len(adj)):
        clique = {start}
        candidates = set(adj[start])
        while candidates:
            node = max(candidates, key=lambda u: len(adj[u] & candidates))
            clique.add(node)
            candidates &= adj[node]
        best = max(best, len(clique))
    return best


def _chromatic_number(
    adj: List[Set[int]], step_budget: int = _COLOR_STEP_BUDGET
) -> Tuple[int, bool, Optional[List[int]]]:
    """Exact chromatic number via branch and bound.

    Returns (value, exact, coloring).  If the step budget is exhausted the
    returned value is only a certified clique lower bound, exact is False
    and coloring is None.
    """
    n = len(adj)
    if n == 0:
        return 0, True, []

    order = sorted(range(n), key=lambda v: -len(adj[v]))
    greedy_color = [-1] * n
    for node in order:
        used = {greedy_color[u] for u in adj[node] if greedy_color[u] != -1}
        color = 0
        while color in used:
            color += 1
        greedy_color[node] = color
    best = max(greedy_color) + 1
    lower = _clique_lower_bound(adj)

    colors = [-1] * n
    state = {'steps': 0, 'exhausted': False}

    def search(position: int, num_colors: int) -> None:
        nonlocal best
        if num_colors >= best or best == lower:
            return
        if state['steps'] >= step_budget:
            state['exhausted'] = True
            return
        state['steps'] += 1
        if position == n:
            best = num_colors
            for node in range(n):
                greedy_color[node] = colors[node]
            return
        node = order[position]
        used = {colors[u] for u in adj[node] if colors[u] != -1}
        for color in range(num_colors):
            if color not in used:
                colors[node] = color
                search(position + 1, num_colors)
                colors[node] = -1
                if state['exhausted'] or best == lower:
                    return
        colors[node] = num_colors
        search(position + 1, num_colors + 1)
        colors[node] = -1

    search(0, 0)
    if state['exhausted']:
        return lower, False, None
    return best, True, list(greedy_color)


def _find_conflicts(
    relations: Sequence[Relation],
    known_revids: Set[int],
    uf: _UnionFind,
    links: Dict[int, List[Tuple[int, str]]],
) -> List[Conflict]:
    conflicts = []
    for relation in relations:
        if relation.kind != DIFFERENT:
            continue
        a, b = relation.revids
        if a not in known_revids or b not in known_revids:
            continue
        if uf.find(a) == uf.find(b):
            chain, path_links = _same_chain(links, a, b)
            conflicts.append(
                Conflict(different=(a, b), same_chain=chain, links=path_links)
            )
    return conflicts


def build_report(
    records: Sequence[RevisionRecord],
    relations: Sequence[Relation],
) -> Tuple[List[MonthReport], List[Relation]]:
    """Build one report per month, plus the list of ignored relations.

    Identity evidence is global (a person stays the same person across
    months); bounds are computed per month from the contributor groups and
    different-person relations visible in that month.
    """
    known_revids = {r.revid for r in records}
    ignored = [
        rel for rel in relations
        if rel.revids[0] not in known_revids or rel.revids[1] not in known_revids
    ]
    uf, links = _build_same_links(records, relations, known_revids)
    conflicts = _find_conflicts(relations, known_revids, uf, links)

    conflict_months: Set[str] = set()
    by_revid = {r.revid: r for r in records}
    for conflict in conflicts:
        for revid in set(conflict.different) | set(conflict.same_chain):
            if revid in by_revid:
                conflict_months.add(by_revid[revid].month)

    months: Dict[str, List[RevisionRecord]] = {}
    for record in records:
        months.setdefault(record.month, []).append(record)

    reports = []
    for month in sorted(months):
        month_records = months[month]
        base = MonthReport(
            month=month,
            status='ok',
            revision_count=len(month_records),
            records_without_account=sum(
                1 for r in month_records if r.user is None
            ),
            records_without_comment=sum(
                1 for r in month_records if r.comment is None
            ),
        )
        if month in conflict_months:
            base.status = 'contradiction'
            base.conflicts = [
                c for c in conflicts
                if any(
                    revid in by_revid and by_revid[revid].month == month
                    for revid in set(c.different) | set(c.same_chain)
                )
            ]
            reports.append(base)
            continue

        component_members: Dict[int, List[int]] = {}
        for record in month_records:
            component_members.setdefault(uf.find(record.revid), []).append(
                record.revid
            )
        components = sorted(
            (sorted(members) for members in component_members.values()),
            key=lambda members: members[0],
        )
        index_of = {
            revid: idx
            for idx, members in enumerate(components)
            for revid in members
        }
        adj: List[Set[int]] = [set() for _ in components]
        for relation in relations:
            if relation.kind != DIFFERENT:
                continue
            a, b = relation.revids
            if a in index_of and b in index_of:
                ia, ib = index_of[a], index_of[b]
                if ia != ib:
                    adj[ia].add(ib)
                    adj[ib].add(ia)

        minimum, exact, coloring = _chromatic_number(adj)
        base.min_contributors = minimum
        base.max_contributors = len(components)
        base.min_is_exact = exact
        base.components = components
        if exact and coloring is not None:
            groups: Dict[int, List[int]] = {}
            for idx, color in enumerate(coloring):
                groups.setdefault(color, []).append(idx)
            base.coloring = [sorted(g) for g in groups.values()]
        reports.append(base)

    return reports, ignored


def to_public_dict(
    reports: Sequence[MonthReport], ignored: Sequence[Relation]
) -> Dict[str, object]:
    """Serialize reports for publication: revision ids only, no accounts."""
    months = []
    for report in reports:
        entry: Dict[str, object] = {
            'month': report.month,
            'status': report.status,
            'revision_count': report.revision_count,
            'records_without_account': report.records_without_account,
            'records_without_comment': report.records_without_comment,
        }
        if report.status == 'contradiction':
            entry['contributors'] = None
            entry['conflicts'] = [
                {
                    'different': list(conflict.different),
                    'same_chain': conflict.same_chain,
                    'links': conflict.links,
                }
                for conflict in report.conflicts
            ]
        else:
            entry['contributors'] = {
                'min': report.min_contributors,
                'max': report.max_contributors,
                'min_is_exact': report.min_is_exact,
            }
            entry['components'] = report.components
            entry['coloring'] = report.coloring
        months.append(entry)
    return {
        'months': months,
        'ignored_relations': [
            {'type': rel.kind, 'revids': list(rel.revids)} for rel in ignored
        ],
    }


def records_to_json(records: Iterable[RevisionRecord]) -> List[Dict[str, object]]:
    return [
        {
            'revid': r.revid,
            'timestamp': r.timestamp,
            'user': r.user,
            'anon': r.anon,
            'comment': r.comment,
        }
        for r in records
    ]


def records_from_json(data: Iterable[Dict[str, object]]) -> List[RevisionRecord]:
    return [record_from_api(entry) for entry in data]


def relations_from_json(data: Iterable[Dict[str, object]]) -> List[Relation]:
    relations = []
    for entry in data:
        kind = str(entry['type'])
        if kind not in (SAME, DIFFERENT):
            raise ValueError('unknown relation type: %r' % kind)
        revids = entry['revids']  # type: ignore[index]
        if len(revids) != 2:
            raise ValueError('relation needs exactly two revids: %r' % (entry,))
        relations.append(Relation(kind=kind, revids=(int(revids[0]), int(revids[1]))))
    return relations


def _cmd_fetch(args: argparse.Namespace) -> int:
    import mwclient

    site = mwclient.Site(host=args.site, path=args.path, scheme=args.scheme)
    records = fetch_records(
        site, args.page, start=args.start, end=args.end, max_items=args.max_items
    )
    payload = records_to_json(records)
    _write_json(payload, args.output)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    with open(args.input, encoding='utf-8') as handle:
        records = records_from_json(json.load(handle))
    relations: List[Relation] = []
    if args.relations:
        with open(args.relations, encoding='utf-8') as handle:
            relations = relations_from_json(json.load(handle))
    reports, ignored = build_report(records, relations)
    _write_json(to_public_dict(reports, ignored), args.output)
    return 0


def _write_json(payload: object, output: Optional[str]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if output:
        with open(output, 'w', encoding='utf-8') as handle:
            handle.write(text + '\n')
    else:
        print(text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Monthly contributor statistics with provable bounds. '
            'The fetched revision dump is internal; the report is safe '
            'to publish (revision ids only, no account names).'
        )
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    fetch = subparsers.add_parser('fetch', help='dump revisions via mwclient')
    fetch.add_argument('--site', required=True, help='wiki host')
    fetch.add_argument('--path', default='/w/', help='wiki script path')
    fetch.add_argument('--scheme', default='https')
    fetch.add_argument(
        '--page', action='append', required=True, help='page title (repeatable)'
    )
    fetch.add_argument('--start', help='start timestamp (ISO 8601)')
    fetch.add_argument('--end', help='end timestamp (ISO 8601)')
    fetch.add_argument('--max-items', type=int, default=None)
    fetch.add_argument('-o', '--output', help='output file (default: stdout)')
    fetch.set_defaults(func=_cmd_fetch)

    report = subparsers.add_parser('report', help='build the public report')
    report.add_argument('input', help='revision dump JSON from the fetch step')
    report.add_argument(
        '--relations', help='JSON file with approved same/different relations'
    )
    report.add_argument('-o', '--output', help='output file (default: stdout)')
    report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
