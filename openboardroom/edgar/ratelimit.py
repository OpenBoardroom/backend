"""A blocking rate limiter that spaces calls evenly."""

import time
from collections.abc import Callable


class RateLimiter:
    """Spaces calls at least ``1 / max_per_second`` seconds apart.

    Even spacing (rather than a burst-then-wait window) keeps every one-second
    window at or under the limit, which is what SEC fair access measures.

    Attributes:
        interval: Minimum seconds between two calls.
    """

    def __init__(
        self,
        max_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a limiter.

        Args:
            max_per_second: Maximum calls per second; must be greater than 0.
            clock: Monotonic clock returning seconds.
            sleep: Function that blocks for the given number of seconds.

        Raises:
            ValueError: If ``max_per_second`` is not greater than 0.
        """
        if not max_per_second > 0:
            raise ValueError(f"max_per_second must be greater than 0, got {max_per_second!r}")
        self.interval = 1.0 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_allowed: float | None = None

    def wait(self) -> None:
        """Block until the next call is allowed, then reserve that slot."""
        now = self._clock()
        if self._next_allowed is not None and now < self._next_allowed:
            self._sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + self.interval
