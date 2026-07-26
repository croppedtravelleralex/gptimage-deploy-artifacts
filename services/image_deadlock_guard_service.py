from __future__ import annotations

import os
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from services.config import config

# Last-resort vCPU budget: used only when the operator set no override, no cgroup
# quota is readable and os.cpu_count() returns nothing (audit 28 §B8 / fix A4-6).
_FALLBACK_CPU_BUDGET_VCPU = 1.5
# cgroup quota files are static for a container's lifetime; re-reading them once a
# minute keeps `docker update --cpus` working without a restart while avoiding a
# syscall on every 2s sample.
_CPU_BUDGET_CACHE_TTL_SECS = 60.0
# Default recovery dwell time: how long CPU must stay *below* the trip threshold
# before an already-tripped guard clears. Deliberately shorter than
# ``sustain_seconds`` — a guard that stays tripped freezes the whole image queue
# (submission rejected, dispatch stopped, account maintenance paused), so the cost
# of clearing slightly early is much lower than the cost of clearing too late.
_DEFAULT_RECOVER_SUSTAIN_SECS = 30.0

_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_CPU_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_CPU_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"


def _read_text(path: str) -> str:
    """Best-effort text read. Returns "" on Windows / non-container hosts."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def read_cgroup_cpu_budget_vcpu() -> tuple[float | None, str]:
    """Resolve the container CPU quota in vCPU from cgroup, as ``(budget, source)``.

    - cgroup v2 ``cpu.max`` holds ``"<quota_us> <period_us>"``, or ``"max <period>"``
      when the container is unlimited (verified in-container: ``150000 100000`` =
      1.5 vCPU for ``cpus: "1.5"``).
    - cgroup v1 splits the same numbers across ``cpu.cfs_quota_us`` (``-1`` =
      unlimited) and ``cpu.cfs_period_us``.

    Returns ``(None, "")`` when no quota applies — unlimited cgroup, host process,
    or a non-Linux dev host — so the caller can fall back to ``os.cpu_count()``.
    """
    raw = _read_text(_CGROUP_V2_CPU_MAX)
    if raw:
        parts = raw.split()
        if parts and parts[0] != "max":
            try:
                quota = float(parts[0])
                period = float(parts[1]) if len(parts) > 1 else 100000.0
                if quota > 0 and period > 0:
                    return quota / period, "cgroup_v2"
            except (TypeError, ValueError):
                pass
    quota_raw = _read_text(_CGROUP_V1_CPU_QUOTA)
    period_raw = _read_text(_CGROUP_V1_CPU_PERIOD)
    if quota_raw and period_raw:
        try:
            quota = float(quota_raw)
            period = float(period_raw)
            if quota > 0 and period > 0:
                return quota / period, "cgroup_v1"
        except (TypeError, ValueError):
            pass
    return None, ""


class _CpuBudgetResolver:
    """Resolves the vCPU budget used to normalize CPU percentages.

    Fallback chain (``cpu_budget_source`` = ``"auto"``, the default):
    cgroup v2 → cgroup v1 → ``os.cpu_count()`` → ``cpu_budget_vcpu`` →
    ``_FALLBACK_CPU_BUDGET_VCPU``. Setting ``cpu_budget_source: "config"`` pins the
    budget to ``cpu_budget_vcpu`` and skips detection entirely, which is the escape
    hatch for hosts where the quota is not discoverable or intentionally different.

    Detection is cached (``_CPU_BUDGET_CACHE_TTL_SECS``); the config override is
    re-read on every call because it is already an in-memory dict lookup.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._detected: float | None = None
        self._detected_source = ""
        self._detected_at = 0.0

    def invalidate(self) -> None:
        with self._lock:
            self._detected = None
            self._detected_source = ""
            self._detected_at = 0.0

    def _detect(self) -> tuple[float | None, str]:
        now = time.monotonic()
        with self._lock:
            if self._detected_at and now - self._detected_at < _CPU_BUDGET_CACHE_TTL_SECS:
                return self._detected, self._detected_source
        budget, source = read_cgroup_cpu_budget_vcpu()
        if budget is None or budget <= 0:
            try:
                count = int(os.cpu_count() or 0)
            except Exception:
                count = 0
            if count > 0:
                budget, source = float(count), "os_cpu_count"
            else:
                budget, source = None, ""
        with self._lock:
            self._detected = budget
            self._detected_source = source
            self._detected_at = now
        return budget, source

    @staticmethod
    def _configured(settings: dict[str, object]) -> float | None:
        raw = settings.get("cpu_budget_vcpu")
        if raw is None or raw is False or raw == "":
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def resolve(self, settings: dict[str, object]) -> tuple[float, str]:
        configured = self._configured(settings)
        mode = str(settings.get("cpu_budget_source") or "auto").strip().lower()
        if mode == "config":
            if configured is not None:
                return max(0.1, configured), "config"
            return _FALLBACK_CPU_BUDGET_VCPU, "default"
        detected, source = self._detect()
        if detected is not None and detected > 0:
            return max(0.1, detected), source
        if configured is not None:
            return max(0.1, configured), "config_fallback"
        return _FALLBACK_CPU_BUDGET_VCPU, "default"


cpu_budget_resolver = _CpuBudgetResolver()


class ImageDeadlockGuardService:
    """生图 CPU 熔断器。

    该服务不额外启动后台线程，而是在调用方检查时采样当前进程 CPU。
    CPU 使用率按解析出的 vCPU 预算归一化（优先 cgroup v2 `cpu.max`，见
    `_CpuBudgetResolver`）：1.5 vCPU 预算下，1.35 个 CPU 核的进程耗时约等于 90%。

    Hysteresis (audit 28 §B8 / fix A4-6). Each sample's ``current_cpu`` is the
    *average* utilisation over the interval since the previous sample, so
    saturation is integrated rather than inferred from two wall-clock timestamps:

    - ``cpu >= deadlock_cpu_threshold``: add the sample's own interval to the
      above-run; trip once the run reaches ``sustain_seconds``.
    - ``cpu < deadlock_cpu_threshold``: the above-run is reset **immediately**.
      The old code only reset inside the ``cpu <= recover_cpu_threshold`` branch,
      leaving a dead band (65~90%) where a stale ``_above_since`` survived for
      minutes and made two isolated spikes look like sustained saturation.
    - Recovery: instant when ``cpu <= recover_cpu_threshold``, otherwise after
      ``recover_sustain_seconds`` below the trip threshold — so CPU parked in the
      dead band no longer freezes the queue forever.

    Known residual (not fixed here): ``time.process_time()`` is whole-process
    across all threads, so text nurture / account refresh / warmup / proxy scans /
    SQLite writes all count against a guard that only pauses *image* admission.
    Narrowing that to image work needs per-thread accounting in the image workers.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] | None = None,
        process_time: Callable[[], float] | None = None,
    ) -> None:
        # Injectable clocks so tests can drive saturation without burning CPU.
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._process_time: Callable[[], float] = process_time or time.process_time
        self._lock = threading.Lock()
        self._last_wall = self._monotonic()
        self._last_cpu = self._process_time()
        self._samples: deque[float] = deque(maxlen=120)
        self._current_cpu = 0.0
        # Wall clock (monotonic) at which the current above-threshold run started;
        # informational only — trip decisions use _above_run_secs.
        self._above_since: float | None = None
        self._above_run_secs = 0.0
        self._below_run_secs = 0.0
        self._tripped = False
        self._tripped_at: float | None = None
        self._reason = ""
        self._cpu_budget_vcpu = _FALLBACK_CPU_BUDGET_VCPU
        self._cpu_budget_source = ""

    def _settings(self) -> dict[str, object]:
        getter = getattr(config, "get_image_deadlock_guard_settings", None)
        if callable(getter):
            try:
                settings = getter()
            except Exception:
                return {}
            if isinstance(settings, dict):
                return settings
        return {}

    @staticmethod
    def _float_setting(settings: dict[str, object], key: str, default: float) -> float:
        try:
            value = float(settings.get(key) or default)
        except (TypeError, ValueError):
            return default
        return value

    def _measure_locked(self, settings: dict[str, object]) -> float:
        """Take a CPU sample when the sampling window elapsed.

        Returns the measured interval in seconds, or ``0.0`` when the window has
        not elapsed yet (no new information — callers must not grow any run timer).
        """
        sample_interval = self._float_setting(settings, "sample_interval_sec", 2.0)
        now = self._monotonic()
        if now - self._last_wall < sample_interval:
            return 0.0

        cpu_now = self._process_time()
        wall_delta = max(0.001, now - self._last_wall)
        cpu_delta = max(0.0, cpu_now - self._last_cpu)
        budget, budget_source = cpu_budget_resolver.resolve(settings)
        self._cpu_budget_vcpu = budget
        self._cpu_budget_source = budget_source
        self._current_cpu = max(0.0, min(1000.0, (cpu_delta / wall_delta / budget) * 100.0))

        self._last_wall = now
        self._last_cpu = cpu_now
        self._samples.append(self._current_cpu)
        return wall_delta

    def _reset_state_locked(self) -> None:
        self._tripped = False
        self._tripped_at = None
        self._above_since = None
        self._above_run_secs = 0.0
        self._below_run_secs = 0.0
        self._reason = ""

    def _evaluate_locked(self, settings: dict[str, object], elapsed: float) -> None:
        """Apply the hysteresis to the latest sample.

        ``elapsed`` is the duration the latest sample represents; ``0.0`` when no
        new sample was taken. With ``elapsed == 0`` no run timer can grow, so a
        state change can only come from information already measured (config
        reload, or CPU already at/below the recover threshold) — that is what keeps
        ``is_tripped()`` from serving a stale value inside the sampling window.
        """
        threshold = self._float_setting(settings, "deadlock_cpu_threshold", 90.0)
        sustain = max(0.0, self._float_setting(settings, "sustain_seconds", 60.0))
        recover = self._float_setting(settings, "recover_cpu_threshold", 65.0)
        # A recover threshold at/above the trip threshold would collapse the
        # hysteresis into a flapping single threshold.
        recover = min(recover, threshold)
        recover_sustain = max(
            0.0,
            self._float_setting(settings, "recover_sustain_seconds", _DEFAULT_RECOVER_SUSTAIN_SECS),
        )
        step = max(0.0, float(elapsed))
        cpu = self._current_cpu

        if cpu >= threshold:
            self._below_run_secs = 0.0
            if self._above_since is None:
                self._above_since = self._monotonic()
            self._above_run_secs += step
            if self._above_run_secs >= sustain:
                if not self._tripped:
                    self._tripped = True
                    self._tripped_at = time.time()
                self._reason = (
                    f"image CPU {cpu:.1f}% >= {threshold:.1f}% "
                    f"for {self._above_run_secs:.1f}s "
                    f"(budget {self._cpu_budget_vcpu:.2f} vCPU via {self._cpu_budget_source or 'default'})"
                )
            return

        # Below the trip threshold: the sustained-saturation run is broken right
        # here, not only once CPU drops under `recover` (audit 28 §B8 dead band).
        self._above_run_secs = 0.0
        self._above_since = None
        if not self._tripped:
            self._below_run_secs = 0.0
            return

        if cpu <= recover:
            self._reset_state_locked()
            return

        self._below_run_secs += step
        if self._below_run_secs >= recover_sustain:
            self._reset_state_locked()
            return
        self._reason = (
            f"image CPU {cpu:.1f}% in recovery band ({recover:.1f}%~{threshold:.1f}%) "
            f"for {self._below_run_secs:.1f}s / {recover_sustain:.1f}s"
        )

    def _refresh_locked(self) -> None:
        settings = self._settings()
        # Sample first so the CPU accounting window keeps advancing even while the
        # guard is disabled — otherwise re-enabling would charge the whole disabled
        # period to a single sample.
        elapsed = self._measure_locked(settings)
        if not bool(settings.get("enabled", True)):
            # Must take effect on the current call, not at the next sample window.
            self._reset_state_locked()
            return
        self._evaluate_locked(settings, elapsed)

    # Kept as the historical entry point name; callers outside this module use
    # is_tripped()/status().
    def _sample_locked(self) -> None:
        self._refresh_locked()

    def is_tripped(self) -> bool:
        with self._lock:
            self._refresh_locked()
            return self._tripped

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if not self._cpu_budget_source:
                # No sample taken yet (first call inside the sampling window):
                # resolve the budget anyway so /health never advertises the
                # fallback as if it were the detected quota.
                self._cpu_budget_vcpu, self._cpu_budget_source = cpu_budget_resolver.resolve(
                    self._settings()
                )
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
                "cpu_budget_vcpu": round(self._cpu_budget_vcpu, 3),
                "cpu_budget_source": self._cpu_budget_source,
                "above_run_secs": round(self._above_run_secs, 2),
                "below_run_secs": round(self._below_run_secs, 2),
            }


image_deadlock_guard_service = ImageDeadlockGuardService()
