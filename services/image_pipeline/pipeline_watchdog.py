"""Pipeline watchdog: slot ledger reconcile + inflight drift detection."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from services.image_pipeline.slot_ledger import slot_ledger

logger = logging.getLogger(__name__)


class PipelineWatchdogService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_tick_mono = 0.0
        self._last_report: dict[str, Any] = {}

    def _running_token_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        try:
            from services.image_task_service import (
                TASK_STATUS_RUNNING,
                TASK_STATUS_TIMEOUT_PENDING,
                image_task_service,
            )

            with image_task_service._lock:
                for task in image_task_service._tasks.values():
                    status = task.get("status")
                    if status not in {TASK_STATUS_RUNNING, TASK_STATUS_TIMEOUT_PENDING}:
                        continue
                    token = str(task.get("access_token") or task.get("account_token") or "").strip()
                    if not token:
                        identity = task.get("identity")
                        if isinstance(identity, dict):
                            token = str(identity.get("access_token") or "").strip()
                    if token:
                        counts[token] = int(counts.get(token, 0)) + 1
        except Exception as exc:
            logger.debug({"event": "watchdog_running_tokens_error", "error": str(exc)})
        return counts

    def tick(self, *, force_release_expired: bool = False) -> dict[str, Any]:
        from services.account_service import account_service
        from services.image_pipeline import image_pipeline_scheduler

        ledger_report = slot_ledger.watchdog_tick(force_release_expired=force_release_expired)
        expected = self._running_token_counts()
        inflight_drift = account_service.reconcile_inflight(expected_by_token=expected, force=False)
        pipeline = image_pipeline_scheduler.snapshot()
        pools = pipeline.get("pools") if isinstance(pipeline, dict) else {}
        ss_pool = (pools or {}).get("ss") if isinstance(pools, dict) else {}
        report = {
            "ts": time.time(),
            "slot_ledger": slot_ledger.snapshot(),
            "ledger_watchdog": ledger_report,
            "inflight_drift": inflight_drift,
            "pipeline_pools": pools,
            "ss_active": int((ss_pool or {}).get("active") or 0) if isinstance(ss_pool, dict) else 0,
            "ss_queued": int((ss_pool or {}).get("queued") or 0) if isinstance(ss_pool, dict) else 0,
            "segments_recent": len(pipeline.get("segments") or []) if isinstance(pipeline, dict) else 0,
        }
        with self._lock:
            self._last_tick_mono = time.monotonic()
            self._last_report = report
        if inflight_drift.get("drift_count"):
            logger.warning({"event": "pipeline_inflight_drift", **inflight_drift})
        return report

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_tick_mono": self._last_tick_mono,
                "last_report": dict(self._last_report),
            }


pipeline_watchdog_service = PipelineWatchdogService()
