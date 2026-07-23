"""Single-task image poll budget (conversation GET + tasks GET + wall time)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

_EXHAUST_LOCK = threading.Lock()
_EXHAUST_COUNTS: dict[str, int] = {
    "wall_time": 0,
    "conversation_get_budget": 0,
    "tasks_get_budget": 0,
}


def record_poll_exhausted(reason: str) -> None:
    key = str(reason or "").strip() or "unknown"
    if key not in _EXHAUST_COUNTS:
        # normalize known aliases
        if "conversation" in key:
            key = "conversation_get_budget"
        elif "tasks" in key:
            key = "tasks_get_budget"
        elif "wall" in key:
            key = "wall_time"
        else:
            _EXHAUST_COUNTS.setdefault(key, 0)
    with _EXHAUST_LOCK:
        _EXHAUST_COUNTS[key] = int(_EXHAUST_COUNTS.get(key) or 0) + 1


def poll_exhaust_snapshot() -> dict[str, Any]:
    with _EXHAUST_LOCK:
        return {
            "wall": int(_EXHAUST_COUNTS.get("wall_time") or 0),
            "conversation_get": int(_EXHAUST_COUNTS.get("conversation_get_budget") or 0),
            "tasks": int(_EXHAUST_COUNTS.get("tasks_get_budget") or 0),
            "by_reason": dict(_EXHAUST_COUNTS),
        }


@dataclass
class ImagePollBudget:
    """Per logical image task poll budget. One coordinator instance per poll loop."""

    max_conversation_gets: int
    max_tasks_gets: int
    tasks_every_n_attempts: int
    wall_deadline: float
    conversation_gets: int = 0
    tasks_gets: int = 0
    attempt: int = 0
    exhausted_reason: str = ""

    @classmethod
    def create(
        cls,
        *,
        timeout_secs: float,
        max_conversation_gets: int,
        max_tasks_gets: int,
        tasks_every_n_attempts: int,
    ) -> "ImagePollBudget":
        return cls(
            max_conversation_gets=max(1, int(max_conversation_gets)),
            max_tasks_gets=max(0, int(max_tasks_gets)),
            tasks_every_n_attempts=max(1, int(tasks_every_n_attempts)),
            wall_deadline=time.time() + max(0.1, float(timeout_secs)),
        )

    def remaining_wall(self) -> float:
        return self.wall_deadline - time.time()

    def begin_attempt(self) -> bool:
        if self.remaining_wall() <= 0:
            self.exhausted_reason = "wall_time"
            record_poll_exhausted(self.exhausted_reason)
            return False
        if self.conversation_gets >= self.max_conversation_gets:
            self.exhausted_reason = "conversation_get_budget"
            record_poll_exhausted(self.exhausted_reason)
            return False
        self.attempt += 1
        return True

    def record_conversation_get(self) -> None:
        self.conversation_gets += 1

    def should_query_tasks(self) -> bool:
        if self.tasks_gets >= self.max_tasks_gets:
            return False
        if self.remaining_wall() <= 0:
            return False
        # First attempt + every N thereafter (tasks are low-frequency diagnostics).
        return self.attempt == 1 or (self.attempt % self.tasks_every_n_attempts == 0)

    def record_tasks_get(self) -> None:
        self.tasks_gets += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "conversation_gets": self.conversation_gets,
            "tasks_gets": self.tasks_gets,
            "max_conversation_gets": self.max_conversation_gets,
            "max_tasks_gets": self.max_tasks_gets,
            "remaining_wall_secs": round(max(0.0, self.remaining_wall()), 2),
            "exhausted_reason": self.exhausted_reason,
        }
