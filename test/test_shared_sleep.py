import json
import unittest.mock as mock

import pytest
import responses

import mwclient
from mwclient.errors import DeadlineExceeded
from mwclient.sleep import Sleepers


class FakeClock:
    """A controllable clock: time only advances when told to.

    Records every slept second so tests can verify how long each waiter
    actually waited.
    """

    def __init__(self, advance_on_sleep=False):
        self.now = 0.0
        self.sleeps = []
        self.advance_on_sleep = advance_on_sleep

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.advance_on_sleep:
            self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def make_sleepers(clock, recovery_interval=2, max_retries=25, retry_timeout=30):
    return Sleepers(max_retries, retry_timeout,
                    recovery_interval=recovery_interval,
                    sleep_func=clock.sleep, time_func=clock.time)


class TestSharedCooldown:

    def test_waiters_share_cooldown_and_recovery_budget(self):
        # Two list iterators on the same site are both told to back off for
        # 30 seconds at t=0.
        clock = FakeClock()
        sleepers = make_sleepers(clock)

        first = sleepers.make()
        second = sleepers.make()
        first.sleep(30)
        second.sleep(30)

        # The second waiter shares the first waiter's cooldown instead of
        # stacking its own 30 seconds on top (which would be 60), and takes
        # the next slot from the shared recovery budget so the two do not
        # both fire at t=30.
        assert clock.sleeps == [30, 32]

    def test_other_site_keeps_its_own_budget(self):
        clock_a = FakeClock()
        clock_b = FakeClock()
        site_a = make_sleepers(clock_a)
        site_b = make_sleepers(clock_b)

        first = site_a.make()
        second = site_a.make()
        first.sleep(30)
        second.sleep(30)

        # The other site is not affected by the first site's cooldown and
        # waits only according to its own budget.
        other = site_b.make()
        other.sleep(30)

        assert clock_a.sleeps == [30, 32]
        assert clock_b.sleeps == [30]

    def test_recovery_slots_keep_spacing_after_cooldown(self):
        clock = FakeClock()
        sleepers = make_sleepers(clock)

        sleepers.make().sleep(30)
        sleepers.make().sleep(30)
        clock.advance(40)

        # The cooldown has expired, but a late waiter still takes a slot from
        # the shared recovery budget rather than a stale one.
        sleepers.make().sleep(0)
        assert clock.sleeps == [30, 32, 0]

    def test_deadline_exceeded_gives_clear_not_fetched_status(self):
        clock = FakeClock()
        sleepers = make_sleepers(clock)
        sleeper = sleepers.make(deadline=10.0)

        with pytest.raises(DeadlineExceeded) as exc_info:
            sleeper.sleep(30)

        assert exc_info.value.deadline == 10.0
        assert exc_info.value.retry_at == 30.0
        # The wait was not even started: the request cannot make it.
        assert clock.sleeps == []

    def test_deadline_per_sleep_call(self):
        clock = FakeClock()
        sleepers = make_sleepers(clock)
        sleeper = sleepers.make()

        with pytest.raises(DeadlineExceeded):
            sleeper.sleep(30, deadline=10.0)
        assert clock.sleeps == []

    def test_deadline_that_can_be_met(self):
        clock = FakeClock()
        sleepers = make_sleepers(clock)
        sleeper = sleepers.make(deadline=60.0)

        sleeper.sleep(30)
        assert clock.sleeps == [30]

    def test_cancelled_waiter_does_not_release_shared_cooldown(self):
        clock = FakeClock()
        sleepers = make_sleepers(clock)

        cancelled = sleepers.make()
        with mock.patch.object(sleepers, 'sleep_func',
                               side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                cancelled.sleep(30)

        # The cancelled waiter had registered a 30 second shared cooldown.
        # Another waiter must still wait it out (and take the next recovery
        # slot) instead of the site being unlocked early.
        waiter = sleepers.make()
        waiter.sleep(0)
        assert clock.sleeps == [32]


class TestSharedCooldownAcrossSites:
    """Verify the requests actually sent against two offline sites."""

    def meta_response(self):
        return json.dumps({
            "query": {
                "general": {"generator": "MediaWiki 1.39.0"},
                "namespaces": {},
                "userinfo": {
                    "anon": "",
                    "groups": ["*"],
                    "id": 0,
                    "name": "127.0.0.1",
                    "rights": ["read"]
                }
            }
        })

    def make_site(self, host, clock):
        responses.add(responses.GET, f'https://{host}/w/api.php',
                      body=self.meta_response(),
                      content_type='application/json')
        site = mwclient.Site(host)
        responses.reset()
        site.sleepers = make_sleepers(clock)
        return site

    @responses.activate
    def test_cooldown_shared_per_site_and_sites_independent(self):
        clock = FakeClock(advance_on_sleep=True)
        site_a = self.make_site('a.wikipedia.org', clock)
        site_b = self.make_site('b.wikipedia.org', clock)

        request_log = []

        def callback_a(request):
            request_log.append(('a', clock.now))
            if len(request_log) == 1:
                # The wiki is busy: back off for 30 seconds.
                return (200, {'x-database-lag': '3', 'retry-after': '30'}, '')
            return (200, {}, '{"query": {"userinfo": {}}}')

        def callback_b(request):
            request_log.append(('b', clock.now))
            return (200, {}, '{"query": {"userinfo": {}}}')

        responses.add_callback(responses.GET,
                               'https://a.wikipedia.org/w/api.php',
                               callback=callback_a)
        responses.add_callback(responses.GET,
                               'https://b.wikipedia.org/w/api.php',
                               callback=callback_b)

        site_a.get('query', meta='userinfo')
        site_b.get('query', meta='userinfo')

        # Site A waited out the maxlag cooldown and retried at t=30; site B
        # was served right away from its own budget and did not wait.
        assert request_log == [('a', 0), ('a', 30), ('b', 30)]
        assert clock.now == 30
        assert [call.request.url.split('?')[0] for call in responses.calls] == [
            'https://a.wikipedia.org/w/api.php',
            'https://a.wikipedia.org/w/api.php',
            'https://b.wikipedia.org/w/api.php',
        ]

    @responses.activate
    def test_deadline_aborts_request_before_retry_is_sent(self):
        clock = FakeClock(advance_on_sleep=True)
        site = self.make_site('a.wikipedia.org', clock)

        def callback(request):
            # The wiki stays busy longer than the caller's deadline.
            return (200, {'x-database-lag': '3', 'retry-after': '30'}, '')

        responses.add_callback(responses.GET,
                               'https://a.wikipedia.org/w/api.php',
                               callback=callback)

        with pytest.raises(DeadlineExceeded):
            site.get('query', meta='userinfo', deadline=10.0)

        # Only the first request was actually sent; the retry was aborted
        # before being issued, and no time was spent waiting.
        assert len(responses.calls) == 1
        assert clock.now == 0
