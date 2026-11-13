from datetime import datetime, timedelta, timezone

from revision_plan import PageWatch, build_plan

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)
QUERYABLE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def make_page(title, days_since_fetch, rate, retention, queryable=QUERYABLE):
    return PageWatch(
        title=title,
        section="test",
        last_fetched=NOW - timedelta(days=days_since_fetch),
        edit_rate_per_day=rate,
        retention_days=retention,
        earliest_queryable=queryable,
    )


def test_earliest_deadline_first_order():
    pages = [
        make_page("slow", 1, 10, retention=30),
        make_page("urgent", 1, 10, retention=1.5),
    ]
    plan = build_plan(pages, budget=10, batch_size=50, now=NOW)
    assert [p.page.title for p in plan.scheduled] == ["urgent", "slow"]


def test_batching_counts_multiple_revisions_per_request():
    pages = [make_page("busy", 2, 120, retention=5)]
    plan = build_plan(pages, budget=10, batch_size=50, now=NOW)
    assert plan.scheduled[0].pending == 240
    assert plan.scheduled[0].cost == 5  # ceil(240 / 50), not 240


def test_budget_limits_and_defers_pages_that_can_wait():
    pages = [
        make_page("urgent", 1, 10, retention=1.5),   # expires before next run
        make_page("patient", 1, 10, retention=30),   # safely waits
    ]
    plan = build_plan(pages, budget=1, batch_size=50, now=NOW)
    assert [p.page.title for p in plan.scheduled] == ["urgent"]
    assert [p.page.title for p in plan.deferred] == ["patient"]
    assert plan.at_risk == []


def test_unaffordable_urgent_page_is_at_risk():
    pages = [make_page("hot", 1, 600, retention=1.5)]
    plan = build_plan(pages, budget=1, batch_size=50, now=NOW)
    assert plan.scheduled == []
    assert [p.page.title for p in plan.at_risk] == ["hot"]


def test_expired_revisions_reported_as_gap():
    # retention of 1 day, last fetch 3 days ago -> 2 days of edits are gone
    pages = [make_page("stale", 3, 10, retention=1)]
    plan = build_plan(pages, budget=10, batch_size=50, now=NOW)
    assert plan.gaps[0].lost == 20
    assert plan.gaps[0].pending == 10


def test_revisions_before_earliest_queryable_are_lost():
    queryable = NOW - timedelta(days=2)
    pages = [make_page("old", 5, 10, retention=30, queryable=queryable)]
    plan = build_plan(pages, budget=10, batch_size=50, now=NOW)
    assert plan.gaps[0].lost == 30
    assert plan.gaps[0].pending == 20


def test_optimal_selection_maximises_saved_revisions():
    # Greedy EDF would take "urgent" (cost 2, 60 revs); the knapsack should
    # prefer two smaller pages worth 90 revisions within the same budget.
    pages = [
        make_page("urgent", 1, 60, retention=1.5),
        make_page("a", 1, 45, retention=30),
        make_page("b", 1, 45, retention=30),
    ]
    plan = build_plan(pages, budget=2, batch_size=50, now=NOW)
    assert plan.revisions_saved == 90
    assert sorted(p.page.title for p in plan.scheduled) == ["a", "b"]


def test_nothing_pending_means_no_requests():
    pages = [make_page("quiet", 0, 10, retention=5)]
    plan = build_plan(pages, budget=10, batch_size=50, now=NOW)
    assert plan.requests_used == 0
    assert plan.scheduled == []
