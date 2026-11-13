"""Plan a nightly revision-collection round under a fixed read-request budget.

Each watched page belongs to a section with its own retention window and a
maximum edit rate.  Given the newest revision already collected per page, the
section's maximum edit speed and the earliest queryable time, this module
decides which pages to fetch tonight, in which order, so that revisions close
to expiring out of the retention window are saved first.

One API read request can return up to ``batch_size`` revisions, so a page with
many pending revisions still costs only ``ceil(pending / batch_size)``
requests.  Revisions that have already aged out of the retention window (or
predate the earliest queryable time) are unrecoverable and reported
separately as gaps.

Usage:
    python revision_plan.py plan.json
    python revision_plan.py --demo

Input JSON format:
    {
      "now": "2026-11-14T02:00:00Z",        # optional, defaults to utcnow
      "budget": 20,                          # read requests available tonight
      "batch_size": 50,                      # revisions per request
      "pages": [
        {"title": "...", "section": "...", "last_fetched": "2026-11-10T00:00:00Z",
         "edit_rate_per_day": 30, "retention_days": 30,
         "earliest_queryable": "2026-10-01T00:00:00Z"}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SECONDS_PER_DAY = 86400.0
# Exact knapsack optimisation is only used for small offline lists.
DP_MAX_PAGES = 64
DP_MAX_BUDGET = 10000


@dataclass
class PageWatch:
    title: str
    section: str
    last_fetched: datetime          # timestamp of newest revision already saved
    edit_rate_per_day: float        # section's maximum edit speed
    retention_days: float           # section's retention window
    earliest_queryable: datetime    # earliest time revisions can be queried


@dataclass
class PagePlan:
    page: PageWatch
    recoverable_from: datetime      # oldest revision still reachable tonight
    deadline: datetime              # when the oldest reachable revision expires
    pending: int                    # recoverable revisions awaiting collection
    lost: int                       # revisions already gone for good
    cost: int                       # read requests needed tonight


@dataclass
class CollectionPlan:
    now: datetime
    next_run: datetime
    budget: int
    batch_size: int
    scheduled: list                 # PagePlan, in fetch order
    deferred: list                  # PagePlan, safely waits until next run
    at_risk: list                   # PagePlan, unaffordable and expires soon
    gaps: list                      # PagePlan with lost > 0

    @property
    def requests_used(self):
        return sum(p.cost for p in self.scheduled)

    @property
    def revisions_saved(self):
        return sum(p.pending for p in self.scheduled)


def _parse_ts(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def assess(page, now):
    """Work out what is still reachable for one page at time ``now``."""
    elapsed_days = (now - page.last_fetched).total_seconds() / SECONDS_PER_DAY
    estimate = max(0, int(page.edit_rate_per_day * elapsed_days))

    # A revision made at time t disappears at t + retention, and nothing
    # before earliest_queryable can be read at all.
    recoverable_from = max(
        page.last_fetched,
        page.earliest_queryable,
        now - timedelta(days=page.retention_days),
    )
    lost_days = (recoverable_from - page.last_fetched).total_seconds() / SECONDS_PER_DAY
    lost = min(estimate, max(0, int(page.edit_rate_per_day * lost_days)))
    pending = estimate - lost

    deadline = recoverable_from + timedelta(days=page.retention_days)
    return PagePlan(page, recoverable_from, deadline, pending, lost, cost=0)


def _with_costs(plans, batch_size):
    for plan in plans:
        plan.cost = math.ceil(plan.pending / batch_size) if plan.pending else 0
    return plans


def _select_greedy(candidates, budget):
    """Earliest-deadline-first; take each page if its cost still fits."""
    chosen, rest = [], []
    remaining = budget
    for plan in candidates:
        if plan.cost <= remaining:
            chosen.append(plan)
            remaining -= plan.cost
        else:
            rest.append(plan)
    return chosen, rest


def _select_optimal(candidates, budget):
    """Exact knapsack for small lists: maximise saved revisions within budget.

    Candidates are processed in deadline order; on ties the earlier-deadline
    page is preferred, so the optimal set stays urgency-biased.
    """
    # dp[b] = (saved, bitmask) best achievable with b requests
    dp = [(0, 0)] * (budget + 1)
    for index, plan in enumerate(candidates):
        cost, value = plan.cost, plan.pending
        if cost > budget or value == 0:
            continue
        bit = 1 << index
        for b in range(budget, cost - 1, -1):
            saved, mask = dp[b - cost]
            candidate = (saved + value, mask | bit)
            if candidate[0] > dp[b][0]:
                dp[b] = candidate
    best_saved, best_mask = max(dp, key=lambda entry: entry[0])
    chosen, rest = [], []
    for index, plan in enumerate(candidates):
        (chosen if best_mask >> index & 1 else rest).append(plan)
    return chosen, rest


def build_plan(pages, budget, batch_size, now=None, next_run=None):
    now = now or datetime.now(timezone.utc)
    next_run = next_run or now + timedelta(days=1)

    plans = _with_costs([assess(p, now) for p in pages], batch_size)
    gaps = [p for p in plans if p.lost > 0]
    candidates = sorted(
        (p for p in plans if p.pending > 0), key=lambda p: p.deadline
    )

    if len(candidates) <= DP_MAX_PAGES and budget <= DP_MAX_BUDGET:
        chosen, rest = _select_optimal(candidates, budget)
        chosen.sort(key=lambda p: p.deadline)  # fetch order stays EDF
    else:
        chosen, rest = _select_greedy(candidates, budget)

    deferred = [p for p in rest if p.deadline > next_run]
    at_risk = [p for p in rest if p.deadline <= next_run]

    return CollectionPlan(
        now=now, next_run=next_run, budget=budget, batch_size=batch_size,
        scheduled=chosen, deferred=deferred, at_risk=at_risk, gaps=gaps,
    )


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%MZ")


def render(plan):
    lines = []
    lines.append(
        "Budget: {0.requests_used}/{0.budget} requests, "
        "saving {0.revisions_saved} revisions tonight "
        "(batch size {0.batch_size}).".format(plan)
    )
    lines.append("")
    lines.append("Fetch order (earliest deadline first):")
    if not plan.scheduled:
        lines.append("  (nothing to fetch)")
    for i, p in enumerate(plan.scheduled, 1):
        lines.append(
            "  {0:>2}. {1} [{2}]  {3} revs / {4} req  deadline {5}".format(
                i, p.page.title, p.page.section, p.pending, p.cost,
                _fmt(p.deadline),
            )
        )
    if plan.deferred:
        lines.append("")
        lines.append("Can wait until tomorrow:")
        for p in plan.deferred:
            lines.append(
                "  - {0} [{1}]  {2} revs, deadline {3}".format(
                    p.page.title, p.page.section, p.pending, _fmt(p.deadline),
                )
            )
    if plan.at_risk:
        lines.append("")
        lines.append("AT RISK (unaffordable tonight, expires before next run):")
        for p in plan.at_risk:
            lines.append(
                "  - {0} [{1}]  {2} revs, deadline {3}".format(
                    p.page.title, p.page.section, p.pending, _fmt(p.deadline),
                )
            )
    if plan.gaps:
        lines.append("")
        lines.append("Unrecoverable gaps (already gone):")
        for p in plan.gaps:
            lines.append(
                "  - {0} [{1}]  ~{2} revisions lost (oldest reachable: {3})".format(
                    p.page.title, p.page.section, p.lost,
                    _fmt(p.recoverable_from),
                )
            )
    return "\n".join(lines)


def load_pages(data):
    pages = []
    for entry in data["pages"]:
        pages.append(PageWatch(
            title=entry["title"],
            section=entry.get("section", ""),
            last_fetched=_parse_ts(entry["last_fetched"]),
            edit_rate_per_day=float(entry["edit_rate_per_day"]),
            retention_days=float(entry["retention_days"]),
            earliest_queryable=_parse_ts(entry["earliest_queryable"]),
        ))
    return pages


DEMO = {
    "now": "2026-11-14T02:00:00Z",
    "budget": 6,
    "batch_size": 50,
    "pages": [
        {"title": "Portal:News", "section": "news",
         "last_fetched": "2026-11-12T02:00:00Z", "edit_rate_per_day": 120,
         "retention_days": 2, "earliest_queryable": "2026-09-01T00:00:00Z"},
        {"title": "Annex:Minutes", "section": "minutes",
         "last_fetched": "2026-11-13T02:00:00Z", "edit_rate_per_day": 40,
         "retention_days": 3, "earliest_queryable": "2026-09-01T00:00:00Z"},
        {"title": "Index:Archive", "section": "archive",
         "last_fetched": "2026-11-07T02:00:00Z", "edit_rate_per_day": 10,
         "retention_days": 30, "earliest_queryable": "2026-09-01T00:00:00Z"},
        {"title": "Board:Notice", "section": "notice",
         "last_fetched": "2026-11-13T14:00:00Z", "edit_rate_per_day": 90,
         "retention_days": 1.5, "earliest_queryable": "2026-09-01T00:00:00Z"},
        {"title": "Draft:Old", "section": "draft",
         "last_fetched": "2026-10-20T02:00:00Z", "edit_rate_per_day": 5,
         "retention_days": 20, "earliest_queryable": "2026-11-01T00:00:00Z"},
    ],
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", nargs="?", help="JSON watch list")
    parser.add_argument("--demo", action="store_true", help="run built-in sample")
    args = parser.parse_args(argv)

    if args.demo or not args.input:
        data = DEMO
    else:
        with open(args.input, encoding="utf-8") as fh:
            data = json.load(fh)

    now = _parse_ts(data["now"]) if data.get("now") else None
    plan = build_plan(
        load_pages(data),
        budget=int(data["budget"]),
        batch_size=int(data.get("batch_size", 50)),
        now=now,
    )
    print(render(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
