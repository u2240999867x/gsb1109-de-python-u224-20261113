import logging
import threading
import time
from typing import Callable, Optional, Any

from mwclient.errors import DeadlineExceeded, MaximumRetriesExceeded

log = logging.getLogger(__name__)


class Sleepers:
    """
    A class that allows for the creation of multiple `Sleeper` objects with shared
    arguments.

    All `Sleeper` objects created from the same `Sleepers` instance share a common
    cooldown and recovery budget. When one of them is asked to wait (for example
    because the site asked clients to back off via `maxlag`), the shared cooldown
    is extended rather than stacked, so concurrent waiters recover together
    instead of summing their individual waits. Once the cooldown expires, each
    waiter takes a slot from the shared recovery budget, spaced
    `recovery_interval` seconds apart, so that they do not all issue their
    requests at the same instant. Since every `Site` creates its own `Sleepers`
    instance, cooldowns are shared per site and never leak across sites.

    Examples:
        Firstly a `Sleepers` object containing the shared attributes has to be created.
        >>> max_retries, retry_timeout = 5, 5
        >>> sleepers = Sleepers(max_retries, retry_timeout)
        From this `Sleepers` object multiple individual `Sleeper` objects can be created
        using the `make` method.
        >>> sleeper = sleepers.make()
    Args:
        max_retries: The maximum number of retries to perform.
        retry_timeout: The time to sleep for each past retry.
        callback: A callable to be called on each retry.
        recovery_interval: The minimum number of seconds between two waiters
            recovering from a shared cooldown. This spaces out concurrent
            waiters so they do not all retry at the same instant.
        sleep_func: The function used to sleep. Defaults to `time.sleep`.
            Mainly useful to inject a controllable clock in tests.
        time_func: The function used to read the current time. Defaults to
            `time.monotonic`. Mainly useful to inject a controllable clock
            in tests.
    Attributes:
        max_retries: The maximum number of retries to perform.
        retry_timeout: The time to sleep for each past retry.
        callback: A callable to be called on each retry.
        recovery_interval: The minimum number of seconds between two waiters
            recovering from a shared cooldown.
        sleep_func: The function used to sleep, or `None` for `time.sleep`.
        time_func: The function used to read the current time, or `None` for
            `time.monotonic`.
    """

    def __init__(
        self,
        max_retries: int,
        retry_timeout: int,
        callback: Callable[['Sleeper', int, Optional[Any]], Any] = lambda *x: None,
        recovery_interval: float = 1.0,
        sleep_func: Optional[Callable[[float], Any]] = None,
        time_func: Optional[Callable[[], float]] = None
    ) -> None:
        self.max_retries = max_retries
        self.retry_timeout = retry_timeout
        self.callback = callback
        self.recovery_interval = recovery_interval
        self.sleep_func = sleep_func
        self.time_func = time_func
        self._lock = threading.Lock()
        # Shared cooldown: no waiter may proceed before this point in time.
        self._cooldown_until = 0.0
        # Shared recovery budget: the next slot a waiter may take.
        self._next_slot = 0.0

    def make(self, args: Optional[Any] = None,
             deadline: Optional[float] = None) -> 'Sleeper':
        """
        Creates a new `Sleeper` object.
        Args:
            args: Arguments to be passed to the `callback` callable.
            deadline: An absolute point in time, in the same clock as
                `time_func`, by which the operation must be able to proceed.
                See `wait`.
        Returns:
            Sleeper: A `Sleeper` object.
        """
        return Sleeper(args, self.max_retries, self.retry_timeout, self.callback,
                       sleepers=self, deadline=deadline)

    def wait(self, timeout: float, deadline: Optional[float] = None) -> None:
        """
        Wait out the shared cooldown and take a slot from the shared recovery
        budget.

        The cooldown is shared between all waiters created from this `Sleepers`
        instance: `timeout` only extends the shared cooldown if it reaches
        further into the future than what other waiters have already
        registered, so concurrent waiters recover together instead of stacking
        their individual waits. Once the cooldown has expired, each waiter
        takes a slot spaced `recovery_interval` seconds apart, so they do not
        all issue their requests at the same instant.

        Cancelling a waiter (for example by interrupting its sleep) does not
        release the shared cooldown: the remaining waiters still wait until it
        expires.

        Args:
            timeout: The number of seconds to wait.
            deadline: An absolute point in time, in the same clock as
                `time_func`, by which the waiter must be able to proceed.

        Raises:
            DeadlineExceeded: If the wait could not finish before `deadline`.
                The waiter is aborted before its request is (re-)issued.
        """
        now = self._now()
        with self._lock:
            if now + timeout > self._cooldown_until:
                self._cooldown_until = now + timeout
            slot = max(self._next_slot, self._cooldown_until)
            self._next_slot = slot + self.recovery_interval
        if deadline is not None and slot > deadline:
            raise DeadlineExceeded(deadline, slot)
        delay = max(0.0, slot - now)
        log.debug('Sleeping for %d seconds', delay)
        self._sleep(delay)

    def _sleep(self, seconds: float) -> None:
        sleep_func = self.sleep_func if self.sleep_func is not None else time.sleep
        sleep_func(seconds)

    def _now(self) -> float:
        time_func = self.time_func if self.time_func is not None else time.monotonic
        return time_func()


class Sleeper:
    """
    For any given operation, a `Sleeper` object keeps count of the number of retries.
    For each retry, the sleep time increases until the max number of retries is reached
    and a `MaximumRetriesExceeded` is raised. The sleeper object should be discarded
    once the operation is successful.

    Sleepers created from the same `Sleepers` instance share a cooldown and
    recovery budget, see `Sleepers` for details.

    Args:
        args: Arguments to be passed to the `callback` callable.
        max_retries: The maximum number of retries to perform.
        retry_timeout: The time to sleep for each past retry.
        callback: A callable to be called on each retry.
        sleepers: The `Sleepers` instance holding the shared cooldown and
            recovery budget. If not given, a private one is created.
        deadline: An absolute point in time by which the operation must be able
            to proceed. See `Sleepers.wait`.
    Attributes:
        args: Arguments to be passed to the `callback` callable.
        retries: The number of retries that have been performed.
        max_retries: The maximum number of retries to perform.
        retry_timeout: The time to sleep for each past retry.
        callback: A callable to be called on each retry.
        sleepers: The `Sleepers` instance holding the shared cooldown and
            recovery budget.
        deadline: An absolute point in time by which the operation must be able
            to proceed, or `None` for no deadline.
    """

    def __init__(
        self,
        args: Any,
        max_retries: int,
        retry_timeout: int,
        callback: Callable[['Sleeper', int, Optional[Any]], Any],
        sleepers: Optional[Sleepers] = None,
        deadline: Optional[float] = None
    ) -> None:
        self.args = args
        self.retries = 0
        self.max_retries = max_retries
        self.retry_timeout = retry_timeout
        self.callback = callback
        if sleepers is None:
            sleepers = Sleepers(max_retries, retry_timeout, callback)
        self.sleepers = sleepers
        self.deadline = deadline

    def sleep(self, min_time: int = 0, deadline: Optional[float] = None) -> None:
        """
        Sleeps for a minimum of `min_time` seconds. The actual sleeping time will
        increase with the number of retries, and may be adjusted so that
        concurrent sleepers created from the same `Sleepers` instance share the
        cooldown and do not all wake up at the same instant.
        Args:
            min_time (int): The minimum sleeping time.
            deadline: An absolute point in time by which the sleeper must be
                able to proceed. Overrides the deadline given on creation.
        Raises:
            MaximumRetriesExceeded: If the number of retries exceeds the maximum.
            DeadlineExceeded: If the wait could not finish before the deadline.
        """
        self.retries += 1
        if self.retries > self.max_retries:
            raise MaximumRetriesExceeded(self, self.args)

        self.callback(self, self.retries, self.args)

        timeout = self.retry_timeout * (self.retries - 1)
        if timeout < min_time:
            timeout = min_time
        if deadline is None:
            deadline = self.deadline
        self.sleepers.wait(timeout, deadline=deadline)
