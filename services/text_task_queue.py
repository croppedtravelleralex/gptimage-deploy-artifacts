"""Lightweight in-memory text task queue (Qtext). Default path stays sync."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TextQueueItem:
    item_id: str
    enqueued_at: float
    payload: dict[str, Any] = field(default_factory=dict)


class TextTaskQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[TextQueueItem] = deque()

    def depth(self) -> int:
        with self._lock:
            return len(self._items)

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
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "depth": len(self._items),
                "oldest_age_sec": round(time.time() - self._items[0].enqueued_at, 3) if self._items else 0.0,
            }


text_task_queue = TextTaskQueue()
