"""Per-key sliding-window rate limiter with bounded memory."""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable


class RateLimiter:
    """Allow at most `max_events` per `window_seconds` for each key (e.g. a Telegram user ID).

    Memory is bounded: once more than `max_keys` keys are tracked, the least recently
    seen are forgotten, so a flood of new accounts can't grow it without limit.
    """

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._events: OrderedDict[Hashable, deque[float]] = OrderedDict()
        self._warned_at: dict[Hashable, float] = {}

    def allow(self, key: Hashable) -> bool:
        now = self._clock()
        events = self._events.pop(key, None) or deque()
        while events and now - events[0] >= self.window:
            events.popleft()
        allowed = len(events) < self.max_events
        if allowed:
            events.append(now)
        self._events[key] = events  # re-insert as most recently seen
        while len(self._events) > self.max_keys:
            old_key, _ = self._events.popitem(last=False)
            self._warned_at.pop(old_key, None)
        return allowed

    def should_warn(self, key: Hashable) -> bool:
        """True at most once per window, so a flooding user gets one notice, not one per message."""
        now = self._clock()
        last = self._warned_at.get(key)
        if last is not None and now - last < self.window:
            return False
        self._warned_at[key] = now
        return True
