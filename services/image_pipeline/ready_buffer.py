from __future__ import annotations

import threading
import time
from typing import Any

from services.config import config

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ITEMS = 32
DEFAULT_RESUME_BYTES = 256 * 1024 * 1024
DEFAULT_HYSTERESIS_SECS = 5.0


class ReadyBufferTracker:
    """Tracks in-flight READY_BUFFER pressure; pauses sS dequeue when over watermark."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, int] = {}
        self._ss_paused = False
        self._resume_after = 0.0

    def _settings(self) -> dict[str, Any]:
        return config.get_image_pipeline_settings()

    def _max_bytes(self) -> int:
        return int(self._settings().get("ready_buffer_max_bytes") or DEFAULT_MAX_BYTES)

    def _max_items(self) -> int:
        return int(self._settings().get("ready_buffer_max_items") or DEFAULT_MAX_ITEMS)

    def _resume_bytes(self) -> int:
        return int(self._settings().get("ready_buffer_resume_bytes") or DEFAULT_RESUME_BYTES)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(self._entries.values())

    def item_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def admit(self, task_key: str, *, bytes_estimate: int = 2 * 1024 * 1024) -> None:
        with self._lock:
            self._entries[task_key] = max(1, int(bytes_estimate))
            if self.total_bytes_locked() > self._max_bytes() or len(self._entries) > self._max_items():
                self._ss_paused = True

    def release(self, task_key: str) -> None:
        with self._lock:
            self._entries.pop(task_key, None)
            if self._ss_paused and self.total_bytes_locked() <= self._resume_bytes():
                self._ss_paused = False
                self._resume_after = time.monotonic() + float(
                    self._settings().get("ready_buffer_hysteresis_secs") or DEFAULT_HYSTERESIS_SECS
                )

    def total_bytes_locked(self) -> int:
        return sum(self._entries.values())

    def should_pause_ss(self) -> bool:
        with self._lock:
            if not self._ss_paused:
                return False
            if self._resume_after and time.monotonic() < self._resume_after:
                return True
            if self.total_bytes_locked() > self._resume_bytes():
                return True
            self._ss_paused = False
            return False

    def wait_for_ss_slot(self, *, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while self.should_pause_ss():
            if time.monotonic() >= deadline:
                raise TimeoutError("ready buffer backpressure timeout")
            time.sleep(0.25)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "bytes": self.total_bytes_locked(),
                "items": len(self._entries),
                "ss_paused": self._ss_paused,
                "max_bytes": self._max_bytes(),
                "max_items": self._max_items(),
            }


ready_buffer_tracker = ReadyBufferTracker()
