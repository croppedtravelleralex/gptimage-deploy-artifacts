from __future__ import annotations

import threading
import time
from collections import deque


class BandwidthTracker:
    """Tracks bandwidth usage with a rolling window of (timestamp, bytes) entries.

    Thread-safe. Retains up to 72 hours of data. The deque is trimmed on every
    mutation and snapshot so callers never need to prune explicitly.
    """

    _MAX_AGE: float = 72 * 3600  # 72 hours in seconds

    def __init__(self) -> None:
        self._entries: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_bytes(self, n: int) -> None:
        """Record *n* bytes transferred at ``time.monotonic()``."""
        with self._lock:
            self._entries.append((time.monotonic(), n))
            self._trim_locked()

    def snapshot(self) -> dict[str, int]:
        """Return cumulative byte counts for the last 5m / 1h / 24h and
        current Mbps (instantaneous, last-60s average)."""
        with self._lock:
            self._trim_locked()
            now = time.monotonic()

            last_24h = self._sum_since(now - 24 * 3600)
            last_1h = self._sum_since(now - 3600)
            last_5m = self._sum_since(now - 300)
            current_mbps = self._current_mbps_locked(now)

        return {
            "last_24h_bytes": last_24h,
            "last_1h_bytes": last_1h,
            "last_5m_bytes": last_5m,
            "current_mbps": current_mbps,
        }

    # ------------------------------------------------------------------
    # Internal helpers (caller must hold _lock)
    # ------------------------------------------------------------------

    def _trim_locked(self) -> None:
        """Remove entries older than ``_MAX_AGE``."""
        cutoff = time.monotonic() - self._MAX_AGE
        while self._entries and self._entries[0][0] < cutoff:
            self._entries.popleft()

    def _sum_since(self, cutoff: float) -> int:
        """Sum bytes for entries with timestamp >= *cutoff*."""
        return sum(b for ts, b in self._entries if ts >= cutoff)

    def _current_mbps_locked(self, now: float) -> int:
        """Instantaneous Mbps computed over the last 60 seconds."""
        cutoff = now - 60
        total_bytes = self._sum_since(cutoff)
        # bytes/s over 60s (fixed window) -> megabits/s
        mbps = (total_bytes * 8) / 60 / 1_000_000
        return round(mbps)


# Singleton
bandwidth_tracker = BandwidthTracker()
