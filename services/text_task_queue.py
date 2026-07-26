"""Lightweight in-memory text task queue (Qtext). Default path stays sync.

A1-5 — lease/ack semantics. `dequeue()` was a bare `popleft`: the caller lost the
item the instant it was taken, and every downstream validation failure (schedule
gate closed, daily cap reached, transient upstream 5xx) destroyed the work item
with no requeue, retry or dead-letter path. `lease()` instead parks the item in an
in-flight table, so the caller must explicitly resolve it:

    lease()  -> commit(item_id)                      # work succeeded, drop it
             -> requeue(item_id, reason=...)         # retryable: backoff, retry later
             -> dead_letter(item_id, reason=...)     # terminal: retire, keep a record

Retryable failures escalate exponentially (`retry_delay_sec`) and terminate in a
bounded dead-letter ring after `MAX_ATTEMPTS`, so a permanently-closed gate can
never livelock the worker and can never silently vanish either. Leases that are
never resolved (caller thread died) are reclaimed after `LEASE_TIMEOUT_SEC`.

`dequeue()` is kept for callers that genuinely want a destructive pop.

KNOWN LIMIT (out of scope here): this queue is still process-local memory with no
persistence and no drain-on-shutdown, so pending / in-flight / dead-letter state
is lost on restart.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Retry policy for leased items that failed for a *retryable* reason.
RETRY_BACKOFF_BASE_SEC = 5.0
RETRY_BACKOFF_FACTOR = 2.0
RETRY_BACKOFF_MAX_SEC = 300.0
RETRY_JITTER_RATIO = 0.15
MAX_ATTEMPTS = 8
LEASE_TIMEOUT_SEC = 900.0
DEAD_LETTER_MAX = 200
DEAD_LETTER_PROMPT_CHARS = 120
REASON_MAX_CHARS = 240


def retry_delay_sec(attempts: int, *, jitter: bool = True) -> float:
    """Exponential backoff for the Nth attempt (1-based), capped and jittered."""
    n = max(1, int(attempts))
    delay = float(RETRY_BACKOFF_BASE_SEC) * (float(RETRY_BACKOFF_FACTOR) ** (n - 1))
    delay = min(delay, float(RETRY_BACKOFF_MAX_SEC))
    if jitter and RETRY_JITTER_RATIO > 0:
        delay += delay * float(RETRY_JITTER_RATIO) * random.random()
    return round(delay, 3)


def _redact_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Dead-letter records are operator-visible: never keep the access token."""
    data = dict(payload or {})
    token = str(data.get("access_token") or "")
    out = {key: value for key, value in data.items() if key != "access_token"}
    out["has_access_token"] = bool(token)
    prompt = out.get("prompt")
    if isinstance(prompt, str) and prompt:
        out["prompt"] = prompt[:DEAD_LETTER_PROMPT_CHARS]
    return out


@dataclass(slots=True)
class TextQueueItem:
    item_id: str
    enqueued_at: float
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    not_before: float = 0.0
    leased_at: float = 0.0
    last_error: str = ""


class TextTaskQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[TextQueueItem] = deque()
        self._inflight: dict[str, TextQueueItem] = {}
        self._dead: deque[dict[str, Any]] = deque(maxlen=DEAD_LETTER_MAX)
        self._requeued_total = 0
        self._dead_total = 0
        self._reclaimed_total = 0

    def depth(self) -> int:
        """Pending items (including ones sitting out a retry backoff)."""
        with self._lock:
            return len(self._items)

    def due_depth(self, *, now: float | None = None) -> int:
        """Pending items eligible to run right now — what a worker loop should poll."""
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._reclaim_expired_locked(ts)
            return sum(1 for item in self._items if item.not_before <= ts)

    def inflight_depth(self) -> int:
        with self._lock:
            return len(self._inflight)

    def dead_letter_depth(self) -> int:
        with self._lock:
            return len(self._dead)

    def dead_letters(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._dead]

    def enqueue(self, payload: dict[str, Any] | None = None) -> TextQueueItem:
        item = TextQueueItem(
            item_id=str(uuid.uuid4()),
            enqueued_at=time.time(),
            payload=dict(payload or {}),
        )
        with self._lock:
            self._items.append(item)
        return item

    def dequeue(self) -> TextQueueItem | None:
        """Destructive pop: the item is gone whatever the caller does with it.

        Kept for callers that want fire-and-forget. Bypasses retry backoff.
        Prefer `lease()` + `commit()` / `requeue()` for work that can fail.
        """
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def lease(self, *, now: float | None = None, lease_timeout_sec: float | None = None) -> TextQueueItem | None:
        """Take the next *due* item and hold it in-flight until it is resolved.

        Returns None when the queue is empty or every pending item is still
        backing off. Exclusive: a leased item is invisible to other leasers.
        """
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._reclaim_expired_locked(ts, lease_timeout_sec)
            index = -1
            for pos, candidate in enumerate(self._items):
                if candidate.not_before <= ts:
                    index = pos
                    break
            if index < 0:
                return None
            # deque has no O(1) pop-at-index; rotate the target to the head and back.
            self._items.rotate(-index)
            item = self._items.popleft()
            self._items.rotate(index)
            item.leased_at = ts
            self._inflight[item.item_id] = item
            return item

    def commit(self, item_id: str) -> bool:
        """Ack a leased item: the work succeeded, drop it for good."""
        key = str(item_id or "")
        with self._lock:
            return self._inflight.pop(key, None) is not None

    def requeue(
        self,
        item_id: str,
        *,
        reason: str = "",
        now: float | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        """Nack a leased item after a retryable failure.

        Applies exponential backoff, or retires the item to the dead-letter ring
        once the attempt budget is exhausted so a permanently-closed gate cannot
        spin forever.
        """
        key = str(item_id or "")
        ts = float(now if now is not None else time.time())
        limit = int(MAX_ATTEMPTS if max_attempts is None else max_attempts)
        with self._lock:
            item = self._inflight.pop(key, None)
            if item is None:
                return {"ok": False, "reason": "unknown_lease", "item_id": key}
            item.attempts += 1
            item.leased_at = 0.0
            item.last_error = str(reason or "")[:REASON_MAX_CHARS]
            if limit > 0 and item.attempts >= limit:
                entry = self._dead_entry_locked(
                    item,
                    reason=f"max_attempts_exhausted({item.attempts}): {item.last_error}".strip(),
                    terminal=False,
                    now=ts,
                )
                return {
                    "ok": True,
                    "dead_lettered": True,
                    "attempts": item.attempts,
                    "item_id": key,
                    "entry": entry,
                }
            delay = retry_delay_sec(item.attempts)
            item.not_before = ts + delay
            self._items.append(item)
            self._requeued_total += 1
            return {
                "ok": True,
                "dead_lettered": False,
                "attempts": item.attempts,
                "retry_in_sec": delay,
                "item_id": key,
            }

    def dead_letter(
        self,
        item_id: str,
        *,
        reason: str = "",
        terminal: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Retire a leased item without retrying (terminal / malformed payload)."""
        key = str(item_id or "")
        ts = float(now if now is not None else time.time())
        with self._lock:
            item = self._inflight.pop(key, None)
            if item is None:
                return {"ok": False, "reason": "unknown_lease", "item_id": key}
            item.attempts += 1
            item.leased_at = 0.0
            item.last_error = str(reason or "")[:REASON_MAX_CHARS]
            entry = self._dead_entry_locked(item, reason=item.last_error, terminal=bool(terminal), now=ts)
            return {
                "ok": True,
                "dead_lettered": True,
                "attempts": item.attempts,
                "item_id": key,
                "entry": entry,
            }

    def reclaim_expired(self, *, now: float | None = None, lease_timeout_sec: float | None = None) -> int:
        """Return leases nobody resolved (dead caller thread) to the pending queue."""
        ts = float(now if now is not None else time.time())
        with self._lock:
            return self._reclaim_expired_locked(ts, lease_timeout_sec)

    def _dead_entry_locked(
        self,
        item: TextQueueItem,
        *,
        reason: str,
        terminal: bool,
        now: float,
    ) -> dict[str, Any]:
        entry = {
            "item_id": item.item_id,
            "payload": _redact_payload(item.payload),
            "attempts": int(item.attempts),
            "enqueued_at": float(item.enqueued_at),
            "dead_at": float(now),
            "terminal": bool(terminal),
            "reason": str(reason or "")[:REASON_MAX_CHARS],
        }
        self._dead.append(entry)
        self._dead_total += 1
        return dict(entry)

    def _reclaim_expired_locked(self, now: float, lease_timeout_sec: float | None = None) -> int:
        timeout = float(LEASE_TIMEOUT_SEC if lease_timeout_sec is None else lease_timeout_sec)
        if timeout <= 0 or not self._inflight:
            return 0
        stale = [
            item
            for item in self._inflight.values()
            if item.leased_at and (now - item.leased_at) >= timeout
        ]
        for item in stale:
            self._inflight.pop(item.item_id, None)
            item.attempts += 1
            item.leased_at = 0.0
            item.last_error = "lease_expired"
            if MAX_ATTEMPTS > 0 and item.attempts >= MAX_ATTEMPTS:
                self._dead_entry_locked(item, reason="lease_expired", terminal=False, now=now)
                continue
            item.not_before = now + retry_delay_sec(item.attempts)
            self._items.append(item)
            self._reclaimed_total += 1
        return len(stale)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                "depth": len(self._items),
                "oldest_age_sec": round(now - self._items[0].enqueued_at, 3) if self._items else 0.0,
                "due_depth": sum(1 for item in self._items if item.not_before <= now),
                "delayed_depth": sum(1 for item in self._items if item.not_before > now),
                "inflight_depth": len(self._inflight),
                "max_attempts": MAX_ATTEMPTS,
                "requeued_total": self._requeued_total,
                "reclaimed_total": self._reclaimed_total,
                "dead_letter_depth": len(self._dead),
                "dead_letter_total": self._dead_total,
                "last_dead_letter": dict(self._dead[-1]) if self._dead else None,
            }


text_task_queue = TextTaskQueue()
