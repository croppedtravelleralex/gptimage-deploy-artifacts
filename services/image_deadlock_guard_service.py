from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Any

from services.config import config


class ImageDeadlockGuardService:
    """生图 CPU 熔断器。

    该服务不额外启动后台线程，而是在调用方检查时采样当前进程 CPU。
    CPU 使用率按配置的 `cpu_budget_vcpu` 归一化：1.5 vCPU 预算下，
    1.35 个 CPU 核的进程耗时约等于 90%。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_wall = time.monotonic()
        self._last_cpu = time.process_time()
        self._samples: deque[float] = deque(maxlen=120)
        self._current_cpu = 0.0
        self._above_since: float | None = None
        self._tripped = False
        self._tripped_at: float | None = None
        self._reason = ""

    def _settings(self) -> dict[str, object]:
        getter = getattr(config, "get_image_deadlock_guard_settings", None)
        if callable(getter):
            return getter()
        return {}

    def _sample_locked(self) -> None:
        settings = self._settings()
        sample_interval = float(settings.get("sample_interval_sec") or 2.0)
        now = time.monotonic()
        if now - self._last_wall < sample_interval:
            return

        cpu_now = time.process_time()
        wall_delta = max(0.001, now - self._last_wall)
        cpu_delta = max(0.0, cpu_now - self._last_cpu)
        budget = max(0.1, float(settings.get("cpu_budget_vcpu") or 1.5))
        current_cpu = max(0.0, min(1000.0, (cpu_delta / wall_delta / budget) * 100.0))

        self._last_wall = now
        self._last_cpu = cpu_now
        self._current_cpu = current_cpu
        self._samples.append(current_cpu)

        if not bool(settings.get("enabled", True)):
            self._tripped = False
            self._above_since = None
            self._reason = ""
            return

        threshold = float(settings.get("deadlock_cpu_threshold") or 90.0)
        sustain = float(settings.get("sustain_seconds") or 60.0)
        recover = float(settings.get("recover_cpu_threshold") or 65.0)

        if current_cpu >= threshold:
            if self._above_since is None:
                self._above_since = now
            if now - self._above_since >= sustain:
                self._tripped = True
                self._tripped_at = self._tripped_at or time.time()
                self._reason = (
                    f"image CPU {current_cpu:.1f}% >= {threshold:.1f}% "
                    f"for {now - self._above_since:.1f}s"
                )
        elif current_cpu <= recover:
            self._above_since = None
            if self._tripped:
                self._tripped = False
                self._reason = ""
                self._tripped_at = None

    def is_tripped(self) -> bool:
        with self._lock:
            self._sample_locked()
            return self._tripped

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._sample_locked()
            samples = list(self._samples)
            p95 = self._current_cpu
            if len(samples) >= 2:
                try:
                    p95 = statistics.quantiles(samples, n=20)[-1]
                except Exception:
                    p95 = max(samples)
            return {
                "enabled": bool(self._settings().get("enabled", True)),
                "tripped": self._tripped,
                "reason": self._reason,
                "current_cpu_pct": round(self._current_cpu, 2),
                "cpu_p95_pct": round(p95, 2),
                "tripped_at": self._tripped_at,
                "sample_count": len(samples),
            }


image_deadlock_guard_service = ImageDeadlockGuardService()
