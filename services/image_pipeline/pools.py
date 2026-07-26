from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class PoolSnapshot:
    name: str
    limit: int
    active: int
    queued: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "name": self.name,
            "limit": self.limit,
            "active": self.active,
            "queued": self.queued,
        }


class _WaitTicket:
    __slots__ = ("event", "holder_id", "enqueued_at")

    def __init__(self, holder_id: str) -> None:
        self.event = threading.Event()
        self.holder_id = holder_id
        self.enqueued_at = time.monotonic()


class SlotPool:
    """FIFO worker pool with numbered slots (pS / sS)."""

    def __init__(self, name: str, slots: int) -> None:
        self.name = name
        self.slots = max(1, int(slots or 1))
        self._lock = threading.Lock()
        self._slot_holders: list[str | None] = [None] * self.slots
        self._waiters: deque[_WaitTicket] = deque()

    def acquire(self, holder_id: str, *, timeout: float | None = None) -> tuple[int, int]:
        """Return (slot_index, queue_wait_ms)."""
        ticket = _WaitTicket(holder_id)
        started = time.monotonic()
        with self._lock:
            if self._grant_if_possible_locked(ticket):
                for index, holder in enumerate(self._slot_holders):
                    if holder == holder_id:
                        return index, 0
            self._waiters.append(ticket)
        if timeout is None:
            ticket.event.wait()
        elif not ticket.event.wait(timeout=max(0.0, timeout)):
            with self._lock:
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass
            raise TimeoutError(f"{self.name} pool acquire timeout")
        queue_wait_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            for index, holder in enumerate(self._slot_holders):
                if holder == holder_id:
                    return index, queue_wait_ms
        return -1, queue_wait_ms

    def release(self, slot: int, holder_id: str) -> None:
        with self._lock:
            index = int(slot)
            if 0 <= index < self.slots and self._slot_holders[index] == holder_id:
                self._slot_holders[index] = None
            else:
                for idx, holder in enumerate(self._slot_holders):
                    if holder == holder_id:
                        self._slot_holders[idx] = None
                        break
            while self._waiters:
                next_ticket = self._waiters[0]
                free_slot = next(
                    (idx for idx, holder in enumerate(self._slot_holders) if holder is None),
                    None,
                )
                if free_slot is None:
                    break
                self._waiters.popleft()
                self._slot_holders[free_slot] = next_ticket.holder_id
                next_ticket.event.set()

    def _grant_if_possible_locked(self, ticket: _WaitTicket) -> bool:
        free_slot = next(
            (idx for idx, holder in enumerate(self._slot_holders) if holder is None),
            None,
        )
        if free_slot is None:
            return False
        self._slot_holders[free_slot] = ticket.holder_id
        ticket.event.set()
        return True

    def try_acquire_immediate(self, holder_id: str) -> tuple[int | None, int]:
        ticket = _WaitTicket(holder_id)
        with self._lock:
            if self._grant_if_possible_locked(ticket):
                for index, holder in enumerate(self._slot_holders):
                    if holder == holder_id:
                        return index, 0
            self._waiters.append(ticket)
        started = time.monotonic()
        ticket.event.wait()
        queue_wait_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            for index, holder in enumerate(self._slot_holders):
                if holder == holder_id:
                    return index, queue_wait_ms
        return None, queue_wait_ms

    def snapshot(self) -> PoolSnapshot:
        with self._lock:
            active = sum(1 for holder in self._slot_holders if holder)
            return PoolSnapshot(name=self.name, limit=self.slots, active=active, queued=len(self._waiters))


class SemaphorePool:
    """Counting semaphore pool (upload / download)."""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = max(1, int(limit or 1))
        self._lock = threading.Lock()
        self._active = 0
        self._waiters: deque[_WaitTicket] = deque()

    def acquire(self, holder_id: str, *, timeout: float | None = None) -> int:
        ticket = _WaitTicket(holder_id)
        with self._lock:
            if self._active < self.limit:
                self._active += 1
                return 0
            self._waiters.append(ticket)
        started = time.monotonic()
        if timeout is None:
            ticket.event.wait()
        elif not ticket.event.wait(timeout=max(0.0, timeout)):
            with self._lock:
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass
            raise TimeoutError(f"{self.name} pool acquire timeout")
        return int((time.monotonic() - started) * 1000)

    def release(self, holder_id: str) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1
            while self._waiters and self._active < self.limit:
                next_ticket = self._waiters.popleft()
                self._active += 1
                next_ticket.event.set()

    def snapshot(self) -> PoolSnapshot:
        with self._lock:
            return PoolSnapshot(name=self.name, limit=self.limit, active=self._active, queued=len(self._waiters))


class PipelinePools:
    def __init__(
        self,
        *,
        prompt_slots: int,
        sse_slots: int,
        download_concurrency: int,
        upload_concurrency: int,
    ) -> None:
        self.ps = SlotPool("ps", prompt_slots)
        self.ss = SlotPool("ss", sse_slots)
        self.download = SemaphorePool("download", download_concurrency)
        self.upload = SemaphorePool("upload", upload_concurrency)
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    def admit(self, global_queue_max: int) -> None:
        with self._in_flight_lock:
            if self._in_flight >= max(1, int(global_queue_max or 1)):
                raise RuntimeError("image pipeline global queue is full")
            self._in_flight += 1

    def finish(self) -> None:
        with self._in_flight_lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    def snapshot(self) -> dict[str, Any]:
        with self._in_flight_lock:
            in_flight = self._in_flight
        ps_snap = self.ps.snapshot()
        ss_snap = self.ss.snapshot()
        return {
            "in_flight": in_flight,
            "ps": ps_snap.to_dict(),
            "ss": ss_snap.to_dict(),
            "ps_queue_depth": ps_snap.queued,
            "ss_queue_depth": ss_snap.queued,
            "upload": self.upload.snapshot().to_dict(),
            "download": self.download.snapshot().to_dict(),
        }
