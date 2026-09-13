"""Authentication failures are limited on their own, apart from work
admission: a wrong secret ten times in a minute locks that client id and
that source address out for a minute, and says for how long."""

from __future__ import annotations

import threading
from collections import deque


class FailureLimiter:
    def __init__(self, *, limit: int = 10, window_s: float = 60.0, lockout_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, now: float) -> float | None:
        """Seconds until ``key`` may try again, or ``None`` when it may now."""
        with self._lock:
            until = self._locked_until.get(key)
            if until is not None:
                if until > now:
                    return until - now
                del self._locked_until[key]
            return None

    def record_failure(self, key: str, now: float) -> float | None:
        """Note a failure; returns the lockout it triggered, if any."""
        with self._lock:
            failures = self._failures.setdefault(key, deque())
            failures.append(now)
            while failures and failures[0] < now - self.window_s:
                failures.popleft()
            if len(failures) >= self.limit:
                self._locked_until[key] = now + self.lockout_s
                failures.clear()
                return self.lockout_s
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
