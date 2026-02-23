"""Per-sender sliding window rate limiter."""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """Sliding window rate limiter using per-sender timestamp deques.

    Args:
        max_messages: Maximum messages allowed within the window.
        window_seconds: Sliding window size in seconds.
    """

    def __init__(self, max_messages: int = 20, window_seconds: int = 60) -> None:
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._last_cleanup = time.monotonic()

    def is_allowed(self, sender_id: str) -> bool:
        """Return True if *sender_id* has not exceeded the rate limit."""
        now = time.monotonic()

        # Periodic cleanup of stale senders
        if now - self._last_cleanup > self.window_seconds * 2:
            self._cleanup(now)

        bucket = self._buckets.get(sender_id)
        if bucket is None:
            bucket = deque()
            self._buckets[sender_id] = bucket

        # Evict timestamps outside the window
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self.max_messages:
            return False

        bucket.append(now)
        return True

    def _cleanup(self, now: float) -> None:
        """Remove senders with no activity within 2× the window."""
        cutoff = now - self.window_seconds * 2
        stale = [
            sid for sid, dq in self._buckets.items()
            if not dq or dq[-1] <= cutoff
        ]
        for sid in stale:
            del self._buckets[sid]
        self._last_cleanup = now
