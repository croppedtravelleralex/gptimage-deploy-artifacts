from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from services.account_service import account_service
from services.config import DATA_DIR, config
from services.content_filter import request_text
from services.humanlike_scheduler import compute_resume_delay_seconds, compute_submit_interval_ms
from services.log_service import LOG_TYPE_CALL, log_service
from services.protocol import openai_v1_image_edit, openai_v1_image_generations
from services.image_pipeline import ImagePoolStarvedError, image_pipeline_scheduler
from services.image_pipeline import schedule_trace

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_TIMEOUT_PENDING = "timeout_pending"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_TIMEOUT_PENDING}

_SUCCESS_DURATION_EWMA_INITIAL_SECS = 60.0
_SUCCESS_DURATION_EWMA_MIN_SECS = 30.0
_SUCCESS_DURATION_EWMA_MAX_SECS = 180.0
_SUCCESS_DURATION_EWMA_ALPHA = 0.2

# --- stuck-RUNNING reaper (audit 28 §B10) ---------------------------------------
# A task is set to RUNNING and persisted *before* its worker starts executing it, so a
# worker that dies mid-task used to strand the row in RUNNING forever: startup recovery
# runs once behind a latch and _cleanup_locked() only evicts TERMINAL_STATUSES. Each
# stranded row permanently consumed global / per-user / per-owner dispatch capacity.
# The reaper's bound is derived from _task_hard_timeout_secs() — the legitimate per-mode
# ceiling (~225s generation / ~435s single-ref edit / ~495s multi-ref) — plus a margin,
# rather than a fresh magic number.
_REAP_INTERVAL_SECS = 15.0
_STUCK_RUNNING_MARGIN_RATIO = 0.25
_STUCK_RUNNING_MARGIN_MIN_SECS = 60.0
_STUCK_RUNNING_MARGIN_MAX_SECS = 180.0
# Backoff after a worker-loop escape, so a persistently failing dependency (locked DB,
# full disk, vanished data dir) cannot turn the now-immortal loop into a hot spin.
# Doubles per consecutive crash on the same worker and resets after a clean iteration.
_WORKER_CRASH_BACKOFF_SECS = 0.2
_WORKER_CRASH_BACKOFF_MAX_SECS = 5.0

# --- sync resume-ladder budget (audit 28 §B7) -----------------------------------
# A sync caller waits `newapi_image_sync_wait_timeout_secs` (540s) and then gets an
# ImageTaskWaitTimeoutError; nothing used to cancel the server-side work. The ladder
# (first attempt + up to `timeout_pending_max_attempts` resume polls, each with its own
# hard timeout) summed to ~1395s nominal, and `resume_deadline_ts` was only checked at
# dispatch, so an attempt started at `deadline - ε` still ran its full hard timeout.
# 1–4 background resume polls therefore kept hitting upstream with the same access token
# for a request nobody was waiting for, burning account quota and poll-worker capacity.
#
# Everything below is derived from the *client* budget so the invariant survives a
# retune of newapi_image_sync_wait_timeout_secs:
#   ladder budget L = C - reserve(C)      reserve covers queue wait + delivery + CDN
# and every wait in the ladder is clamped to `sync_ladder_deadline_ts = attach + L`.
# Async (detached) tasks never get that field, so their ladder is untouched.
_SYNC_LADDER_RESERVE_RATIO = 0.10
_SYNC_LADDER_RESERVE_MIN_SECS = 20.0
_SYNC_LADDER_RESERVE_MAX_SECS = 120.0
# A resume attempt shorter than this cannot finish a conversation GET + URL resolve +
# download, so dispatching one only spends quota. Used as the "is another attempt worth
# it" gate for sync-attached tasks.
_SYNC_LADDER_MIN_ATTEMPT_SECS = 20.0
# ``wait_for_result`` is also used for short polling reads, whose documented contract is
# "the task keeps running in the background" (ImageTaskWaitTimeoutError). Only a budget
# large enough to hold the reserve plus one useful attempt counts as *the* client bound;
# anything shorter is a poll, not a deadline, and must not shorten the ladder.
# `_sync_client_budget_secs` clamps the real sync budget to >= 60s, so the sync adapter
# path always qualifies.
_SYNC_LADDER_MIN_BINDING_BUDGET_SECS = _SYNC_LADDER_RESERVE_MIN_SECS + _SYNC_LADDER_MIN_ATTEMPT_SECS
# Reserve may never eat more than this share of a budget, so a small budget still leaves
# a usable ladder instead of a negative one.
_SYNC_LADDER_RESERVE_MAX_SHARE = 0.5
# Margin `_resume_poll_hard_timeout_secs` adds on top of the poll budget for the final
# URL resolve + download. Shared so the dispatch clamp and the wait clamp cannot drift.
_RESUME_POLL_OVERHEAD_SECS = 60.0
# The ladder deadline is re-read in slices while an attempt runs, so a sync waiter that
# attaches *after* dispatch still bounds the attempt already in flight.
_SYNC_LADDER_RECHECK_INTERVAL_SECS = 5.0

# --- per-owner submit fairness (audit 28 §A4-7) ---------------------------------
# `relaxed_per_user_running` used to short-circuit `_effective_per_user_running_max`
# to `sse_slots`, silently dead-configuring per_user_running_max / _base / _burst and
# the burst_min_* keys. It now only applies when the operator has configured none of
# them, so un-tuned deployments keep the old ceiling while explicit config binds.
_PER_USER_RUNNING_CONFIG_KEYS = (
    "per_user_running_max",
    "per_user_running_base",
    "per_user_running_burst",
)
# `_run_task` holds its submit worker for the whole end-to-end task (requirements →
# SSE → poll → download; it is *not* released during I/O wait), so a per-user ceiling
# equal to `submit_workers` let one owner pin every worker for up to ~495s each. When
# other owners have queued work, an owner is additionally capped by a fair share of the
# pool and by a reserve that keeps the pool from ever being fully consumed by one owner.
_OWNER_RESERVE_RATIO = 0.25


class ImageTaskQueueFullError(RuntimeError):
    """异步生图队列已满或被熔断保护暂停。"""


class ImageTaskDuplicatePromptError(RuntimeError):
    """同 owner 短窗内重复 prompt 被拒绝。"""


class ImageTaskWaitTimeoutError(TimeoutError):
    """同步等待图片任务结果超时；任务仍在后台队列中继续执行。"""

    def __init__(self, task_id: str, task: dict[str, Any]):
        self.task_id = task_id
        self.task = task
        status = _clean(task.get("status"), "unknown")
        super().__init__(f"image task wait timeout: {task_id} (status={status})")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_from_ts(value: float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    text = str(value or default).strip()
    return text or default


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _looks_like_timeout(message: str) -> bool:
    lowered = message.lower()
    return "timeout" in lowered or "timed out" in lowered or "超时" in message


def _looks_like_token_invalid(message: str) -> bool:
    lowered = str(message or "").lower()
    return (
        "token invalidated" in lowered
        or "token_revoked" in lowered
        or "invalidated oauth token" in lowered
        or "authentication token has been invalidated" in lowered
    )


def _image_generation_paused() -> bool:
    if bool(config.data.get("image_generation_paused")):
        return True
    try:
        settings = config.get_image_task_queue_settings()
        return not bool(settings.get("enabled", True))
    except Exception:
        return False


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _call_log_usage_fields(usage: object) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    fields: dict[str, Any] = {"usage": dict(usage)}
    prompt_tokens = _positive_int(usage.get("prompt_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _positive_int(usage.get("input_tokens"))
    completion_tokens = _positive_int(usage.get("completion_tokens"))
    if completion_tokens is None:
        completion_tokens = _positive_int(usage.get("output_tokens"))
    if prompt_tokens is not None:
        fields["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        fields["completion_tokens"] = completion_tokens
    total_tokens = _positive_int(usage.get("total_tokens"))
    if total_tokens is not None:
        fields["total_tokens"] = total_tokens
    return fields


def _completion_tokens_from_usage(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("completion_tokens", "output_tokens"):
        value = _positive_int(usage.get(key))
        if value is not None:
            return value
    return 0


def _tokens_per_sec_from_sources(
    usage: object,
    *,
    sse_stream_ms: int = 0,
    total_wall_ms: int = 0,
) -> float | None:
    completion_tokens = _completion_tokens_from_usage(usage)
    if completion_tokens <= 0:
        return None
    denom_ms = sse_stream_ms if sse_stream_ms > 0 else total_wall_ms
    if denom_ms <= 0:
        return None
    return round(completion_tokens / (denom_ms / 1000.0), 2)


def _traffic_field_from_source(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _positive_int(source.get(key))
        if value is not None:
            return value
    return None


def _call_log_traffic_fields(*sources: object) -> dict[str, int]:
    upload: int | None = None
    download: int | None = None
    for source in sources:
        if not isinstance(source, dict):
            continue
        if upload is None:
            upload = _traffic_field_from_source(
                source,
                "upload_bytes",
                "uploaded_bytes",
                "traffic_upload_bytes",
                "traffic_uploaded_bytes",
            )
        if download is None:
            download = _traffic_field_from_source(
                source,
                "download_bytes",
                "downloaded_bytes",
                "traffic_download_bytes",
                "traffic_downloaded_bytes",
            )
    fields: dict[str, int] = {}
    if upload is not None:
        fields["upload_bytes"] = upload
    if download is not None:
        fields["download_bytes"] = download
    if upload is not None and download is not None:
        fields["traffic_bytes"] = upload + download
    return fields


def _encode_for_json(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, bytearray):
        return {"__bytes_b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode_for_json(item) for item in value]}
    if isinstance(value, list):
        return [_encode_for_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode_for_json(item) for key, item in value.items()}
    return value


def _decode_from_json(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {"__bytes_b64__"}:
            try:
                return base64.b64decode(str(value["__bytes_b64__"]).encode("ascii"))
            except Exception:
                return b""
        if set(value.keys()) == {"__tuple__"}:
            items = value.get("__tuple__")
            return tuple(_decode_from_json(item) for item in items) if isinstance(items, list) else tuple()
        return {key: _decode_from_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_from_json(item) for item in value]
    return value


def _compact_task_memory(task: dict[str, Any]) -> None:
    """Drop heavy blobs from in-memory terminal tasks (persisted copy already saved)."""
    payload = task.get("payload")
    if isinstance(payload, dict):
        if payload.get("images"):
            payload["images"] = []
        if payload.get("image"):
            payload.pop("image", None)
    data = task.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("b64_json"):
                item.pop("b64_json", None)
    for heavy_key in ("schedule_trace", "detail", "log_detail"):
        if heavy_key in task:
            task.pop(heavy_key, None)


TERMINAL_MEMORY_RETENTION_SECS = 90.0


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "size": task.get("size"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("conversation_id"):
        item["conversation_id"] = task.get("conversation_id")
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("usage") is not None:
        item["usage"] = task.get("usage")
    if task.get("error"):
        item["error"] = task.get("error")
    if task.get("progress"):
        item["progress"] = task.get("progress")
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    if task.get("total_wall_ms") is not None:
        item["total_wall_ms"] = int(task.get("total_wall_ms") or 0)
    if task.get("task_queue_ms") is not None:
        item["task_queue_ms"] = int(task.get("task_queue_ms") or 0)
    if task.get("worker_duration_ms") is not None:
        item["worker_duration_ms"] = int(task.get("worker_duration_ms") or 0)
    phase_timings = task.get("phase_timings_ms")
    if isinstance(phase_timings, dict) and phase_timings:
        item["phase_timings_ms"] = phase_timings
        wall_clock = phase_timings.get("wall_clock_ms")
        if wall_clock is not None:
            item["wall_clock_ms"] = int(wall_clock)
    if task.get("retry_phase_cursor"):
        item["retry_phase_cursor"] = task.get("retry_phase_cursor")
    if task.get("pipeline_phase"):
        item["pipeline_phase"] = task.get("pipeline_phase")
    if task.get("enhanced_prompt"):
        item["enhanced_prompt"] = task.get("enhanced_prompt")
    sediment_ids = task.get("sediment_ids")
    if isinstance(sediment_ids, list) and sediment_ids:
        item["sediment_ids"] = sediment_ids
    if task.get("resume_attempts") is not None:
        item["resume_attempts"] = int(task.get("resume_attempts") or 0)
    if task.get("next_resume_ts"):
        item["next_resume_at"] = _iso_from_ts(float(task.get("next_resume_ts") or 0.0))
    if task.get("status") in UNFINISHED_STATUSES:
        if task.get("status") == TASK_STATUS_RUNNING:
            base_ts = task.get("started_ts") or task.get("updated_ts")
        else:
            base_ts = task.get("created_ts") or task.get("updated_ts")
        if base_ts:
            item["elapsed_secs"] = round(time.time() - float(base_ts), 1)
    return item


def _result_count(task: dict[str, Any]) -> int:
    data = task.get("data")
    if isinstance(data, list):
        return len(data)
    return 0


def _task_elapsed_secs(task: dict[str, Any]) -> float | None:
    status = task.get("status")
    if status not in UNFINISHED_STATUSES:
        return None
    if status == TASK_STATUS_RUNNING:
        base_ts = task.get("started_ts") or task.get("updated_ts")
    else:
        base_ts = task.get("created_ts") or task.get("updated_ts")
    if not base_ts:
        return None
    try:
        return round(time.time() - float(base_ts), 1)
    except Exception:
        return None


def _status_task(
    task: dict[str, Any],
    *,
    queue_position: int | None = None,
    estimated_start_after_secs: int | None = None,
    running_limit: int | None = None,
    accepted_limit: int | None = None,
) -> dict[str, Any]:
    """轻量任务状态，不返回 data/payload/image/b64 等大字段。"""
    item: dict[str, Any] = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "result_count": _result_count(task),
    }
    if task.get("progress"):
        item["progress"] = task.get("progress")
    elapsed_secs = _task_elapsed_secs(task)
    if elapsed_secs is not None:
        item["elapsed_secs"] = elapsed_secs
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    if task.get("error"):
        item["error"] = task.get("error")
    if task.get("resume_attempts") is not None:
        item["resume_attempts"] = int(task.get("resume_attempts") or 0)
    if task.get("next_resume_ts"):
        item["next_resume_at"] = _iso_from_ts(float(task.get("next_resume_ts") or 0.0))
    if queue_position is not None:
        item["queue_position"] = queue_position
    if estimated_start_after_secs is not None:
        item["estimated_start_after_secs"] = estimated_start_after_secs
    if running_limit is not None:
        item["running_limit"] = running_limit
    if accepted_limit is not None:
        item["accepted_limit"] = accepted_limit
    return item


class ImageTaskService:
    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_generations.handle,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        retention_days_getter: Callable[[], int] | None = None,
        submit_workers_getter: Callable[[], int] | None = None,
        poll_workers_getter: Callable[[], int] | None = None,
        global_queue_max_getter: Callable[[], int] | None = None,
        per_user_running_max_getter: Callable[[], int] | None = None,
        per_user_queue_max_getter: Callable[[], int] | None = None,
        timeout_pending_poll_secs_getter: Callable[[], int] | None = None,
        timeout_pending_max_attempts_getter: Callable[[], int] | None = None,
        deadlock_guard: object | None = None,
    ):
        self.path = path
        self.db_path = path if path.suffix.lower() == ".db" else path.with_suffix(".db")
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self.submit_workers_getter = submit_workers_getter or (lambda: int(self._queue_settings().get("submit_workers") or 6))
        self.poll_workers_getter = poll_workers_getter or (lambda: int(self._queue_settings().get("poll_workers") or 24))
        self.global_queue_max_getter = global_queue_max_getter or (lambda: int(self._queue_settings().get("global_queue_max") or 200))
        self.per_user_running_max_getter = per_user_running_max_getter or (lambda: int(self._queue_settings().get("per_user_running_max") or 2))
        self.per_user_queue_max_getter = per_user_queue_max_getter or (lambda: int(self._queue_settings().get("per_user_queue_max") or 36))
        self.timeout_pending_poll_secs_getter = timeout_pending_poll_secs_getter or (lambda: int(self._queue_settings().get("timeout_pending_poll_secs") or 120))
        self.timeout_pending_max_attempts_getter = timeout_pending_max_attempts_getter or (lambda: int(self._queue_settings().get("timeout_pending_max_attempts") or 3))
        if deadlock_guard is None:
            try:
                from services.image_deadlock_guard_service import image_deadlock_guard_service
                deadlock_guard = image_deadlock_guard_service
            except Exception:
                deadlock_guard = None
        self.deadlock_guard = deadlock_guard
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._submit_threads: list[threading.Thread] = []
        self._poll_threads: list[threading.Thread] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._runtime_recovered = False
        self._recent_prompt_hashes: dict[str, float] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._success_duration_ewma_secs = _SUCCESS_DURATION_EWMA_INITIAL_SECS
        self._last_submit_start_ts = 0.0
        # task_key -> live PipelineRun. The run object is otherwise reachable only from
        # the worker frame that created it, so a dead worker took the only handle on its
        # pipeline accounting with it (audit 28 §B10).
        self._active_pipeline_runs: dict[str, object] = {}
        self._last_reap_ts = 0.0
        # Per-worker consecutive crash counter, for the loop backoff.
        self._worker_crash_state = threading.local()
        # One-shot latch for the sync-ladder budget warning (audit 28 §B7).
        self._sync_ladder_validation_logged = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._init_db_locked()
            self._tasks = self._load_locked()

    def start_background(self) -> None:
        with self._condition:
            if self._stop_event.is_set():
                self._stop_event = threading.Event()
            self._log_sync_ladder_validation_locked()
            self._recover_runtime_tasks_locked()
            self._ensure_workers_locked()
            self._condition.notify_all()

    def _queue_settings(self) -> dict[str, object]:
        getter = getattr(config, "get_image_task_queue_settings", None)
        if callable(getter):
            return getter()
        return {}

    def _effective_submit_interval_ms(self) -> int:
        try:
            base = max(0, int(self._queue_settings().get("submit_start_min_interval_ms") or 0))
        except Exception:
            base = 0
        if self._queue_settings().get("submit_interval_adaptive", True):
            try:
                from services.account_service import account_service
                from services.image_pipeline import schedule_core

                stats = account_service.get_image_candidate_runtime_stats()
                inflight = int(stats.get("image_inflight_count") or 0)
                cap = self._adaptive_submit_cap()
                with self._condition:
                    queued = sum(
                        1 for task in self._tasks.values() if task.get("status") == TASK_STATUS_QUEUED
                    )
                if not schedule_core.dispatch_should_apply_interval(
                    interval_ms=base,
                    inflight=inflight,
                    cap=cap,
                    queued=queued,
                ):
                    return 0
            except Exception:
                pass
        settings = config.get_scheduler_settings()
        if not settings.get("enabled") or base <= 0:
            return base
        return compute_submit_interval_ms(
            base,
            jitter_lo=float(settings.get("submit_interval_jitter_lo") or 0.7),
            jitter_hi=float(settings.get("submit_interval_jitter_hi") or 1.3),
        )

    def _adaptive_submit_cap(self) -> int:
        try:
            pipeline = config.get_image_pipeline_settings()
            sse_slots = int(pipeline.get("sse_slots") or 10)
        except Exception:
            sse_slots = 10
        try:
            from services.account_service import account_service

            stats = account_service.get_image_candidate_runtime_stats()
            global_limit = int(stats.get("image_global_limit") or stats.get("image_global_concurrency_limit") or 0)
        except Exception:
            global_limit = 0
        if global_limit <= 0:
            global_limit = sse_slots
        with self._condition:
            per_user = self._effective_per_user_running_max_locked()
        return max(1, min(sse_slots, global_limit, per_user))

    def _resume_delay_secs(self, attempts: int) -> float:
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return min(300.0, 30.0 * max(1, int(attempts or 1)))
        return compute_resume_delay_seconds(
            int(attempts or 1),
            first_delay_sec=float(settings.get("resume_first_delay_sec") or 5),
            backoff_base_sec=float(settings.get("resume_backoff_base_sec") or 5),
            backoff_cap_sec=float(settings.get("resume_backoff_cap_sec") or 60),
        )

    def _resume_wall_secs(self) -> float:
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return 300.0
        return max(60.0, float(settings.get("resume_wall_sec") or 240.0))

    def _resume_deadline_ts(self, now: float | None = None, *, key: str = "") -> float:
        """Resume wall-clock deadline, never later than the sync ladder deadline.

        The wall alone bounded nothing useful: it was anchored at whatever "now" the
        transition happened at and re-armed on every hop, so the ladder could outlive
        the client by minutes (audit 28 §B7).
        """
        current = float(now if now is not None else time.time())
        deadline = current + self._resume_wall_secs()
        if key:
            ladder = self._sync_ladder_deadline_ts(key)
            if ladder > 0:
                deadline = min(deadline, ladder)
        return deadline

    # ------------------------------------------------------- §B7 sync ladder budget

    def _sync_client_budget_secs(self) -> float:
        """Wall-clock budget a sync caller actually waits (see wait_for_result)."""
        try:
            return max(60.0, min(900.0, float(config.newapi_image_sync_wait_timeout_secs)))
        except Exception:
            return 540.0

    def _sync_ladder_reserve_secs(self, client_budget_secs: float) -> float:
        """Slice of the client budget the ladder must *not* consume.

        Covers the queue wait before the first attempt, the terminal DB write, the
        response marshalling and the NewAPI/Cloudflare hop in front of us.
        """
        try:
            ratio = float(
                self._queue_settings().get("sync_ladder_reserve_ratio")
                or _SYNC_LADDER_RESERVE_RATIO
            )
        except Exception:
            ratio = _SYNC_LADDER_RESERVE_RATIO
        ratio = max(0.0, min(_SYNC_LADDER_RESERVE_MAX_SHARE, ratio))
        budget = max(1.0, float(client_budget_secs))
        reserve = max(
            _SYNC_LADDER_RESERVE_MIN_SECS,
            min(_SYNC_LADDER_RESERVE_MAX_SECS, budget * ratio),
        )
        return min(reserve, budget * _SYNC_LADDER_RESERVE_MAX_SHARE)

    def _sync_ladder_budget_secs(self, client_budget_secs: float | None = None) -> float:
        budget = float(client_budget_secs if client_budget_secs is not None else self._sync_client_budget_secs())
        return max(1.0, budget - self._sync_ladder_reserve_secs(budget))

    def _sync_ladder_deadline_ts(self, key: str) -> float:
        """0.0 for async/detached tasks — they have no client bound to respect.

        Deliberately lock-free: this is read on every ladder wait slice, and both dict
        lookups are atomic under the GIL. Taking ``self._lock`` here would queue a waiting
        worker behind every ``list_tasks`` / ``_cleanup_locked`` pass, for a value that
        only ever moves earlier.
        """
        if not key:
            return 0.0
        task = self._tasks.get(key)
        if not isinstance(task, dict):
            return 0.0
        try:
            return float(task.get("sync_ladder_deadline_ts") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _sync_ladder_remaining_secs(self, key: str, *, now: float | None = None) -> float | None:
        """Seconds of ladder budget left, or None when the task is not sync-attached.

        None means "do not shorten anything": a genuinely detached background task has
        no 540s client bound and keeps the original ladder.
        """
        deadline = self._sync_ladder_deadline_ts(key)
        if deadline <= 0:
            return None
        return deadline - float(now if now is not None else time.time())

    def _sync_ladder_exhausted(self, key: str, *, now: float | None = None) -> bool:
        remaining = self._sync_ladder_remaining_secs(key, now=now)
        return remaining is not None and remaining <= 0.0

    def _attach_sync_waiter_locked(self, key: str, *, wait_timeout_secs: float, client_deadline_ts: float) -> None:
        """Record the client bound this task's ladder has to converge inside.

        Called from ``wait_for_result``, which *is* the definition of a sync caller.
        Several callers may await the same task_id; the ladder has to fit the earliest
        client deadline, otherwise the first one to give up still pays for the rest.
        """
        task = self._tasks.get(key)
        if not isinstance(task, dict):
            return
        if float(wait_timeout_secs) < _SYNC_LADDER_MIN_BINDING_BUDGET_SECS:
            # A short poll-style wait is not a client deadline; bounding the ladder by it
            # would contradict "the task keeps running in the background".
            return
        ladder_deadline = float(client_deadline_ts) - self._sync_ladder_reserve_secs(wait_timeout_secs)
        previous_ladder = 0.0
        try:
            previous_ladder = float(task.get("sync_ladder_deadline_ts") or 0.0)
        except (TypeError, ValueError):
            previous_ladder = 0.0
        if previous_ladder > 0:
            ladder_deadline = min(previous_ladder, ladder_deadline)
        task["sync_waiters"] = int(task.get("sync_waiters") or 0) + 1
        if previous_ladder != ladder_deadline:
            task["sync_ladder_deadline_ts"] = ladder_deadline
            task["sync_client_deadline_ts"] = float(client_deadline_ts)
            # Persist: a restart must not silently hand the ladder an unbounded budget.
            self._save_task_locked(key)

    def _detach_sync_waiter(self, key: str) -> None:
        """The waiter is gone; the recorded deadline deliberately stays.

        Once a sync client has attached, the budget is spent whether or not it is still
        listening — keeping the deadline is what stops the ladder outliving it.
        """
        with self._condition:
            task = self._tasks.get(key)
            if isinstance(task, dict):
                task["sync_waiters"] = max(0, int(task.get("sync_waiters") or 0) - 1)

    def _effective_task_hard_timeout_secs(self, key: str, payload: dict[str, Any]) -> float:
        """Per-mode hard timeout, clamped to what is left of the sync ladder budget."""
        natural = self._task_hard_timeout_secs(payload)
        remaining = self._sync_ladder_remaining_secs(key)
        if remaining is None:
            return natural
        return max(0.1, min(natural, remaining))

    def _effective_resume_poll_hard_timeout_secs(self, key: str, timeout_secs: float) -> float:
        natural = self._resume_poll_hard_timeout_secs(timeout_secs)
        remaining = self._sync_ladder_remaining_secs(key)
        if remaining is None:
            return natural
        return max(0.1, min(natural, remaining))

    def _nominal_resume_backoff_secs(self, attempts: int) -> float:
        """Worst-case (max-jitter) backoff for attempt N, for the budget validation."""
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return min(300.0, 30.0 * max(1, int(attempts or 1)))
        # Pinning both jitter bounds to the high end makes the real scheduler function
        # deterministic instead of re-deriving its formula here.
        return compute_resume_delay_seconds(
            int(attempts or 1),
            first_delay_sec=float(settings.get("resume_first_delay_sec") or 5),
            backoff_base_sec=float(settings.get("resume_backoff_base_sec") or 5),
            backoff_cap_sec=float(settings.get("resume_backoff_cap_sec") or 60),
            jitter_lo=1.25,
            jitter_hi=1.25,
        )

    def validate_sync_ladder_budget(self) -> dict[str, Any]:
        """Surface an inverted timeout configuration instead of letting it rot.

        The 1395s-against-540s inversion arose because no single place compared the
        server-side ladder with the client budget. This does, per mode, and is called
        from ``start_background`` so a future retune shows up in the log.
        """
        client_budget = self._sync_client_budget_secs()
        reserve = self._sync_ladder_reserve_secs(client_budget)
        ladder_budget = self._sync_ladder_budget_secs(client_budget)
        try:
            max_attempts = max(1, int(self.timeout_pending_max_attempts_getter()))
        except Exception:
            max_attempts = 1
        try:
            resume_poll_secs = max(5.0, float(self.timeout_pending_poll_secs_getter()))
        except Exception:
            resume_poll_secs = 180.0
        backoff_total = sum(self._nominal_resume_backoff_secs(index) for index in range(1, max_attempts + 1))
        resume_attempt_secs = self._resume_poll_hard_timeout_secs(resume_poll_secs)
        modes: dict[str, Any] = {}
        for name, poll_timeout in self._nominal_mode_poll_timeouts().items():
            first_attempt = self._task_hard_timeout_secs({"poll_timeout_secs": poll_timeout})
            nominal_total = first_attempt + backoff_total + (resume_attempt_secs * max_attempts)
            overflow = nominal_total - ladder_budget
            modes[name] = {
                "poll_timeout_secs": round(poll_timeout, 1),
                "first_attempt_secs": round(first_attempt, 1),
                "resume_attempt_secs": round(resume_attempt_secs, 1),
                "nominal_total_secs": round(nominal_total, 1),
                "overflow_secs": round(max(0.0, overflow), 1),
                "inverted": overflow > 0,
                # What the ladder is actually allowed to spend after the clamp.
                "enforced_total_secs": round(min(nominal_total, ladder_budget), 1),
            }
        inverted = [name for name, item in modes.items() if item["inverted"]]
        report: dict[str, Any] = {
            "client_budget_secs": round(client_budget, 1),
            "reserve_secs": round(reserve, 1),
            "ladder_budget_secs": round(ladder_budget, 1),
            "resume_max_attempts": max_attempts,
            "nominal_backoff_total_secs": round(backoff_total, 1),
            "modes": modes,
            "inverted": bool(inverted),
            "inverted_modes": inverted,
        }
        if inverted:
            report["detail"] = (
                "server-side resume ladder exceeds the sync client budget for "
                f"{', '.join(inverted)}; attempts will be truncated to "
                f"{report['ladder_budget_secs']}s (client budget {report['client_budget_secs']}s)"
            )
        else:
            report["detail"] = "resume ladder converges inside the sync client budget"
        return report

    def _nominal_mode_poll_timeouts(self) -> dict[str, float]:
        def _read(name: str, fallback: float) -> float:
            try:
                return max(1.0, float(getattr(config, name)))
            except Exception:
                return fallback

        return {
            "generate": _read("image_generation_poll_timeout_secs", 120.0),
            "edit": _read("image_edit_poll_timeout_secs", 300.0),
            "multi_reference": _read("image_multi_reference_poll_timeout_secs", 360.0),
        }

    def _log_sync_ladder_validation_locked(self) -> None:
        if getattr(self, "_sync_ladder_validation_logged", False):
            return
        self._sync_ladder_validation_logged = True
        try:
            report = self.validate_sync_ladder_budget()
        except Exception:
            return
        if not report.get("inverted"):
            return
        try:
            log_service.add(LOG_TYPE_CALL, "同步生图超时阶梯配置倒挂", {"status": "warning", **report})
        except Exception:
            pass

    def _prompt_fingerprint(self, owner: str, prompt: str) -> str:
        digest = hashlib.sha256(str(prompt or "").strip().encode("utf-8")).hexdigest()
        return f"{owner}:{digest}"

    def _count_unfinished_same_prompt_locked(self, owner: str, fingerprint: str) -> int:
        count = 0
        for task in self._tasks.values():
            if task.get("owner_id") != owner:
                continue
            if _clean(task.get("status")) not in UNFINISHED_STATUSES:
                continue
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            prompt = _clean(payload.get("prompt"))
            if not prompt:
                continue
            if self._prompt_fingerprint(owner, prompt) == fingerprint:
                count += 1
        return count

    def _enforce_prompt_dedup_locked(self, owner: str, payload: dict[str, Any]) -> None:
        """同 prompt 允许最多 N 路未完成并行；无未完成且窗口内刚提交过才拒绝。"""
        settings = config.get_scheduler_settings()
        window = float(settings.get("prompt_dedup_window_sec") or 0.0)
        if not settings.get("enabled") or window <= 0:
            return
        prompt = _clean(payload.get("prompt"))
        if not prompt:
            return
        try:
            max_parallel = max(1, int(settings.get("prompt_dedup_max_parallel") or 4))
        except (TypeError, ValueError):
            max_parallel = 4
        now = time.time()
        stale = [key for key, ts in self._recent_prompt_hashes.items() if now - float(ts) > window]
        for key in stale:
            self._recent_prompt_hashes.pop(key, None)
        fingerprint = self._prompt_fingerprint(owner, prompt)
        unfinished = self._count_unfinished_same_prompt_locked(owner, fingerprint)
        if unfinished >= max_parallel:
            raise ImageTaskDuplicatePromptError(
                f"duplicate prompt within {int(window)}s window; please wait or vary the prompt"
            )
        last = float(self._recent_prompt_hashes.get(fingerprint) or 0.0)
        # 仍有未完成同 prompt → 同批兄弟，放行
        if unfinished > 0:
            self._recent_prompt_hashes[fingerprint] = now
            return
        # 无未完成：窗口内刚提交过则视为刷单拒绝
        if last > 0 and (now - last) < window:
            raise ImageTaskDuplicatePromptError(
                f"duplicate prompt within {int(window)}s window; please wait or vary the prompt"
            )
        self._recent_prompt_hashes[fingerprint] = now

    def _recover_runtime_tasks_locked(self) -> None:
        if self._runtime_recovered:
            return
        changed = self._recover_unfinished_locked()
        changed = self._cleanup_locked() or changed
        if changed:
            self._save_locked()
        self._runtime_recovered = True

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
        response_format: str = "url",
        n: int = 1,
        prompt_enhance: bool = False,
        prompt_enhance_locale: str = "en",
        multi_image_mode: str = "fast",
        preferred_account_email: str = "",
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "model": model,
            "n": max(1, min(4, int(n or 1))),
            "size": size,
            "quality": quality,
            "response_format": _clean(response_format, "url"),
            "base_url": base_url,
            "poll_timeout_secs": float(config.image_generation_poll_timeout_secs),
            "resume_timeout_secs": float(self.timeout_pending_poll_secs_getter()),
            "queue_coordinated": True,
            "prompt_enhance": bool(prompt_enhance),
            "prompt_enhance_locale": _clean(prompt_enhance_locale, "en"),
            "multi_image_mode": _clean(multi_image_mode, "fast"),
            "preferred_account_email": _clean(preferred_account_email),
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
        image_asset_ids: list[str] | None = None,
        mask_asset_ids: list[str] | None = None,
        response_format: str = "url",
        n: int = 1,
        prompt_enhance: bool = False,
        prompt_enhance_locale: str = "en",
        multi_image_mode: str = "fast",
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "images": images or [],
            "mask": masks or [],
            "image_asset_ids": image_asset_ids or [],
            "mask_asset_ids": mask_asset_ids or [],
            "model": model,
            "n": max(1, min(4, int(n or 1))),
            "size": size,
            "quality": quality,
            "response_format": _clean(response_format, "url"),
            "base_url": base_url,
        }
        reference_count = len(payload["images"]) + len(payload["image_asset_ids"])
        payload["poll_timeout_secs"] = float(
            config.image_multi_reference_poll_timeout_secs
            if reference_count > 1
            else config.image_edit_poll_timeout_secs
        )
        payload["resume_timeout_secs"] = float(self.timeout_pending_poll_secs_getter())
        payload["queue_coordinated"] = True
        payload["prompt_enhance"] = bool(prompt_enhance)
        payload["prompt_enhance_locale"] = _clean(prompt_enhance_locale, "en")
        payload["multi_image_mode"] = _clean(multi_image_mode, "fast")
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def success_duration_ewma_secs(self) -> float:
        with self._lock:
            return float(self._success_duration_ewma_secs)

    def note_success_duration_ms(self, duration_ms: object) -> None:
        try:
            secs = max(1.0, float(duration_ms) / 1000.0)
        except Exception:
            return
        with self._lock:
            previous = float(self._success_duration_ewma_secs or _SUCCESS_DURATION_EWMA_INITIAL_SECS)
            updated = ((1.0 - _SUCCESS_DURATION_EWMA_ALPHA) * previous) + (_SUCCESS_DURATION_EWMA_ALPHA * secs)
            self._success_duration_ewma_secs = max(
                _SUCCESS_DURATION_EWMA_MIN_SECS,
                min(_SUCCESS_DURATION_EWMA_MAX_SECS, updated),
            )

    def estimate_sync_eta_secs(self, identity: dict[str, object], *, extra_waiters: int = 0) -> int:
        """Estimate wall-clock wait for a new sync request from this owner."""
        owner = _owner_id(identity)
        with self._lock:
            unfinished = [
                task
                for task in self._tasks.values()
                if task.get("owner_id") == owner and self._uses_submit_capacity(task)
            ]
            ahead = len(unfinished) + max(0, int(extra_waiters or 0))
            try:
                global_slots = max(1, int(getattr(config, "image_global_concurrency", 6) or 6))
            except Exception:
                global_slots = 6
            per_user_slots = max(1, int(self._effective_per_user_running_max_locked()))
            try:
                if image_pipeline_scheduler.enabled():
                    pipeline = config.get_image_pipeline_settings()
                    per_user_slots = max(
                        per_user_slots,
                        int(pipeline.get("sse_slots") or global_slots),
                    )
            except Exception:
                pass
            running_slots = max(1, min(global_slots, per_user_slots))
            ewma = float(self._success_duration_ewma_secs or _SUCCESS_DURATION_EWMA_INITIAL_SECS)
        if ahead <= 0:
            return 0
        batches = int(math.ceil(ahead / float(running_slots)))
        return int(max(0, batches * ewma))

    def queue_snapshot_for_task(self, identity: dict[str, object], task_id: str) -> dict[str, Any]:
        result = self.list_task_statuses(identity, [task_id])
        items = result.get("items") if isinstance(result, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return dict(items[0])
        return {}

    def wait_for_result(
        self,
        identity: dict[str, object],
        task_id: str,
        *,
        timeout_secs: float | None = None,
        poll_interval_secs: float | None = None,
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        task_id = _clean(task_id)
        if not task_id:
            raise ValueError("task_id is required")
        key = _task_key(owner, task_id)
        try:
            wait_timeout = max(0.05, min(900.0, float(timeout_secs if timeout_secs is not None else 540.0)))
        except Exception:
            wait_timeout = 540.0
        try:
            poll_interval = max(0.2, min(10.0, float(poll_interval_secs if poll_interval_secs is not None else 1.5)))
        except Exception:
            poll_interval = 1.5
        deadline = time.time() + wait_timeout
        attached = False
        try:
            with self._condition:
                while True:
                    if self._cleanup_locked():
                        self._save_locked()
                    task = self._tasks.get(key) or self._load_task_from_db_locked(key)
                    if task is None:
                        raise KeyError(f"image task not found: {task_id}")
                    if not attached:
                        # Being awaited here is what makes a task "sync": from now on the
                        # whole server-side ladder has to fit inside this caller's budget
                        # (audit 28 §B7). Async submitters never reach this method.
                        self._attach_sync_waiter_locked(
                            key,
                            wait_timeout_secs=wait_timeout,
                            client_deadline_ts=deadline,
                        )
                        attached = True
                    status = _clean(task.get("status"))
                    if status in TERMINAL_STATUSES:
                        return _public_task(task)
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise ImageTaskWaitTimeoutError(task_id, _public_task(task))
                    self._condition.wait(timeout=min(poll_interval, max(0.05, remaining)))
        finally:
            if attached:
                self._detach_sync_waiter(key)

    def compact_task_heavy_fields(self, identity: dict[str, object], task_id: str) -> None:
        """Drop in-memory b64 blobs after sync client has read the result."""
        owner = _owner_id(identity)
        key = _task_key(owner, _clean(task_id))
        with self._lock:
            task = self._tasks.get(key)
            if not isinstance(task, dict):
                return
            _compact_task_memory(task)
            if _clean(task.get("status")) in TERMINAL_STATUSES:
                self._tasks.pop(key, None)

    def queue_depth(self) -> int:
        with self._lock:
            return sum(1 for task in self._tasks.values() if task.get("status") == TASK_STATUS_QUEUED)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id)) or self._load_task_from_db_locked(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [_public_task(task) for task in self._tasks.values() if task.get("owner_id") == owner]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def cancel_task(self, identity: dict[str, object], task_id: str) -> dict[str, Any]:
        """取消未完成任务：queued 立即失败；running/timeout_pending 触发 cancel_event。"""
        owner = _owner_id(identity)
        tid = _clean(task_id)
        if not tid:
            raise ValueError("task_id is required")
        key = _task_key(owner, tid)
        with self._condition:
            self._ensure_workers_locked()
            task = self._tasks.get(key) or self._load_task_from_db_locked(key)
            if task is None:
                raise KeyError(f"image task not found: {tid}")
            status = _clean(task.get("status"))
            if status in TERMINAL_STATUSES:
                return _public_task(task)
            now = time.time()
            now_iso = _now_iso()
            if status == TASK_STATUS_QUEUED:
                task.update(
                    {
                        "status": TASK_STATUS_ERROR,
                        "progress": "cancelled",
                        "error": "cancelled by user",
                        "updated_at": now_iso,
                        "updated_ts": now,
                        "duration_ms": int(max(0.0, now - float(task.get("created_ts") or now)) * 1000),
                    }
                )
                self._cancel_events.pop(key, None)
                self._save_task_locked(key)
                self._condition.notify_all()
                return _public_task(task)
            # running / timeout_pending：发取消信号，并立刻标为 cancelled（避免 UI 挂死）
            event = self._cancel_events.get(key)
            if event is not None:
                event.set()
            task.update(
                {
                    "status": TASK_STATUS_ERROR,
                    "progress": "cancelled",
                    "error": "cancelled by user",
                    "updated_at": now_iso,
                    "updated_ts": now,
                    "duration_ms": int(max(0.0, now - float(task.get("created_ts") or now)) * 1000),
                }
            )
            self._cancel_events.pop(key, None)
            self._save_task_locked(key)
            self._condition.notify_all()
            return _public_task(task)

    def list_task_statuses(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            try:
                # Must be the number dispatch actually enforces, not the raw config key:
                # the two used to be computed independently, so the UI could promise 2
                # while `_next_submit_task_locked` ran 10 (audit 28 §A4-7).
                running_limit = max(1, int(self._owner_running_cap_locked(owner)))
            except Exception:
                try:
                    running_limit = max(1, int(self.per_user_running_max_getter()))
                except Exception:
                    running_limit = 2
            try:
                accepted_limit = max(1, int(self.per_user_queue_max_getter()))
            except Exception:
                accepted_limit = 36
            owner_tasks = [task for task in self._tasks.values() if task.get("owner_id") == owner]
            queued = [
                task for task in owner_tasks
                if task.get("status") == TASK_STATUS_QUEUED
            ]
            queued.sort(
                key=lambda task: (
                    float(task.get("created_ts") or task.get("updated_ts") or 0.0),
                    str(task.get("id") or ""),
                )
            )
            positions = {str(task.get("id") or ""): index for index, task in enumerate(queued, start=1)}

            def public_status(task: dict[str, Any]) -> dict[str, Any]:
                task_id = str(task.get("id") or "")
                position = positions.get(task_id)
                estimated_start_after_secs = None
                if position is not None:
                    # 只做保守体验提示，不作为调度承诺。每批按 running_limit 平滑释放。
                    estimated_start_after_secs = max(0, ((position - 1) // running_limit) * 90)
                return _status_task(
                    task,
                    queue_position=position,
                    estimated_start_after_secs=estimated_start_after_secs,
                    running_limit=running_limit,
                    accepted_limit=accepted_limit,
                )

            items = []
            missing_ids = []
            for task_id in requested_ids:
                key = _task_key(owner, task_id)
                task = self._tasks.get(key)
                if task is None:
                    task = self._load_task_status_from_db_locked(key)
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(public_status(task))
            if not requested_ids:
                items = [public_status(task) for task in owner_tasks]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def stop(self) -> None:
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        for thread in [*self._submit_threads, *self._poll_threads]:
            thread.join(timeout=1.0)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _submit(self, identity: dict[str, object], *, client_task_id: str, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        if _image_generation_paused():
            raise ImageTaskQueueFullError("image generation is paused to preserve the account pool")
        if image_pipeline_scheduler.enabled():
            try:
                from services.image_pipeline.guards import ensure_dispatchable_pool

                ensure_dispatchable_pool(min_count=2)
            except ImagePoolStarvedError as exc:
                raise ImageTaskQueueFullError(str(exc)) from exc
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        now_ts = time.time()
        with self._condition:
            self._ensure_workers_locked()
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            self._enforce_queue_limits_locked(owner)
            self._enforce_prompt_dedup_locked(owner, payload)
            preferred = ""
            if isinstance(payload, dict):
                preferred = str(payload.get("preferred_account_email") or "").strip()
            try:
                from services.image_pipeline.account_lease_pool import account_lease_pool

                if preferred:
                    account_lease_pool.seed_hint(preferred)
                queued = sum(
                    1 for item in self._tasks.values() if item.get("status") == TASK_STATUS_QUEUED
                )
                account_lease_pool.seed_queued_preferences(self._tasks)
                account_lease_pool.maintain(max_acquire=min(16, max(4, queued + 3)))
            except Exception:
                pass
            if schedule_trace.enabled():
                schedule_trace.begin(key, preferred)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), "gpt-image-2"),
                "size": _clean(payload.get("size")),
                "quality": _clean(payload.get("quality"), "auto"),
                "created_at": now,
                "updated_at": now,
                "created_ts": now_ts,
                "updated_ts": now_ts,
                "payload": payload,
                "identity": dict(identity),
                "resume_attempts": 0,
                "progress": "queued",
            }
            self._tasks[key] = task
            self._save_task_locked(key)
            public = _public_task(task)
            self._condition.notify_all()
            return public

    def _enforce_queue_limits_locked(self, owner: str) -> None:
        if self._deadlock_guard_tripped_locked():
            raise ImageTaskQueueFullError("image task queue is temporarily paused by deadlock guard")
        unfinished = [task for task in self._tasks.values() if task.get("status") in UNFINISHED_STATUSES]
        try:
            global_limit = max(1, int(self.global_queue_max_getter()))
        except Exception:
            global_limit = 200
        if len(unfinished) >= global_limit:
            raise ImageTaskQueueFullError(f"image task queue is full ({len(unfinished)}/{global_limit})")
        try:
            owner_limit = max(1, int(self.per_user_queue_max_getter()))
        except Exception:
            owner_limit = 36
        owner_unfinished = [
            task
            for task in unfinished
            if task.get("owner_id") == owner and self._uses_submit_capacity(task)
        ]
        if len(owner_unfinished) >= owner_limit:
            raise ImageTaskQueueFullError(f"image task queue is full for current user ({len(owner_unfinished)}/{owner_limit})")

    def _requeue_task_for_pipeline_backpressure(self, key: str, reason: str) -> None:
        """Put a RUNNING task back on the queue when pipeline slots are saturated."""
        with self._condition:
            task = self._tasks.get(key)
            if not isinstance(task, dict):
                return
            if task.get("status") != TASK_STATUS_RUNNING:
                return
            now_ts = time.time()
            task.update({
                "status": TASK_STATUS_QUEUED,
                "progress": "queued",
                "error": "",
                "pipeline_backpressure": str(reason or "")[:240],
                "updated_at": _now_iso(),
                "updated_ts": now_ts,
            })
            task.pop("started_ts", None)
            task.pop("worker_started_ts", None)
            self._save_task_locked(key)
            self._condition.notify_all()

    def _deadlock_guard_tripped_locked(self) -> bool:
        guard = self.deadlock_guard
        if guard is None:
            return False
        try:
            method = getattr(guard, "is_tripped", None)
            if callable(method):
                return bool(method())
            return bool(getattr(guard, "tripped", False))
        except Exception:
            return False

    def _target_submit_workers(self) -> int:
        try:
            return max(1, int(self.submit_workers_getter()))
        except Exception:
            return 6

    def _target_poll_workers(self) -> int:
        try:
            return max(0, int(self.poll_workers_getter()))
        except Exception:
            return 24

    def _ensure_workers_locked(self) -> None:
        self._recover_runtime_tasks_locked()
        if _image_generation_paused():
            self._submit_threads = [thread for thread in self._submit_threads if thread.is_alive()]
            self._poll_threads = [thread for thread in self._poll_threads if thread.is_alive()]
            return
        self._submit_threads = [thread for thread in self._submit_threads if thread.is_alive()]
        while len(self._submit_threads) < self._target_submit_workers():
            index = len(self._submit_threads) + 1
            thread = threading.Thread(target=self._submit_worker_loop, name=f"image-submit-worker-{index}", daemon=True)
            self._submit_threads.append(thread)
            thread.start()
        self._poll_threads = [thread for thread in self._poll_threads if thread.is_alive()]
        while len(self._poll_threads) < self._target_poll_workers():
            index = len(self._poll_threads) + 1
            thread = threading.Thread(target=self._poll_worker_loop, name=f"image-poll-worker-{index}", daemon=True)
            self._poll_threads.append(thread)
            thread.start()

    def _owner_running_count_locked(self, owner: str) -> int:
        return sum(
            1
            for task in self._tasks.values()
            if task.get("owner_id") == owner
            and task.get("status") == TASK_STATUS_RUNNING
            and not self._is_resume_polling_task(task)
        )

    @staticmethod
    def _is_resume_polling_task(task: dict[str, Any]) -> bool:
        return (
            task.get("status") == TASK_STATUS_RUNNING
            and _clean(task.get("progress")) == "resume_polling"
            and bool(_clean(task.get("conversation_id")))
        )

    @classmethod
    def _uses_submit_capacity(cls, task: dict[str, Any]) -> bool:
        status = task.get("status")
        if status == TASK_STATUS_QUEUED:
            return True
        return status == TASK_STATUS_RUNNING and not cls._is_resume_polling_task(task)

    @staticmethod
    def _explicit_per_user_running_configured() -> bool:
        """True when the operator actually set any per-user running knob.

        ``get_image_task_queue_settings()`` normalises every key in, so presence has to
        be read from the raw block. This is what lets ``relaxed_per_user_running`` keep
        its old meaning for un-tuned deployments while explicit config now binds
        (audit 28 §5 / §A4-7).
        """
        raw = getattr(config, "data", None)
        block = raw.get("image_task_queue") if isinstance(raw, dict) else None
        if not isinstance(block, dict):
            return False
        return any(key in block for key in _PER_USER_RUNNING_CONFIG_KEYS)

    def _effective_per_user_running_max_locked(self) -> int:
        """Config-derived per-user running ceiling (before per-owner fairness).

        ``relaxed_per_user_running`` used to ``return`` here with ``sse_slots``, ahead of
        base/burst, which made per_user_running_max / _base / _burst and every burst_min_*
        key dead configuration. It is now a *floor relaxation* applied only when none of
        those keys are configured, so behaviour is unchanged for deployments that never
        set them and honoured for the ones that did (audit 28 §A4-7).
        """
        value = self._configured_per_user_running_max_locked()
        if not self._explicit_per_user_running_configured():
            try:
                relaxed = image_pipeline_scheduler.relaxed_per_user_running_max()
            except Exception:
                relaxed = None
            if relaxed is not None:
                value = max(value, int(relaxed))
        return max(1, value)

    def _configured_per_user_running_max_locked(self) -> int:
        settings = self._queue_settings()
        try:
            base = max(1, int(settings.get("per_user_running_base") or settings.get("per_user_running_max") or 6))
        except Exception:
            base = 6
        try:
            burst = max(base, int(settings.get("per_user_running_burst") or 8))
        except Exception:
            burst = max(base, 8)
        if not bool(settings.get("burst_enabled")):
            return base
        queued_count = sum(1 for task in self._tasks.values() if task.get("status") == TASK_STATUS_QUEUED)
        try:
            min_queued = max(1, int(settings.get("burst_min_queued") or base))
        except Exception:
            min_queued = base
        try:
            min_dispatchable = max(1, int(settings.get("burst_min_dispatchable_candidates") or 120))
        except Exception:
            min_dispatchable = 120
        try:
            max_preflight_backoff = max(0, int(settings.get("burst_max_preflight_backoff") or 0))
        except Exception:
            max_preflight_backoff = 0
        try:
            stats = account_service.get_image_candidate_runtime_stats()
        except Exception:
            return base
        dispatchable = int(stats.get("dispatchable_candidate_count") or 0)
        preflight_backoff = int(stats.get("preflight_backoff_count") or 0)
        inflight = int(stats.get("image_inflight_count") or 0)
        if self._deadlock_guard_tripped_locked():
            return base
        if (
            queued_count >= min_queued
            and dispatchable >= min_dispatchable
            and preflight_backoff <= max_preflight_backoff
            and inflight < burst
        ):
            return burst
        return base

    # ----------------------------------------------- §A4-7 per-owner submit fairness

    def _contending_owner_count_locked(self, owner: str) -> int:
        """Distinct *other* owners with queued work right now."""
        owners = {
            _clean(task.get("owner_id"))
            for task in self._tasks.values()
            if task.get("status") == TASK_STATUS_QUEUED and _clean(task.get("owner_id")) != owner
        }
        owners.discard("")
        return len(owners)

    def _owner_running_cap_locked(self, owner: str) -> int:
        """Running-task ceiling for one owner — the single source of truth.

        Dispatch used to read ``_effective_per_user_running_max_locked`` (which
        ``relaxed_per_user_running`` pinned to ``sse_slots``) while the UI displayed
        ``per_user_running_max_getter()``, so the two disagreed. Both now call this.

        Because a submit worker is held for the whole end-to-end task, a ceiling equal to
        ``submit_workers`` let one owner pin the entire pool. Under contention the owner is
        additionally capped by a max-min fair share and by a reserve that keeps at least
        one worker out of any single owner's reach. With no other owner waiting the
        configured ceiling applies unchanged, so single-tenant throughput is untouched.
        """
        ceiling = self._effective_per_user_running_max_locked()
        try:
            hard_max = max(1, int(self.per_user_running_max_getter()))
        except Exception:
            hard_max = ceiling
        ceiling = max(1, min(ceiling, hard_max))
        contenders = self._contending_owner_count_locked(owner)
        if contenders <= 0:
            return ceiling
        workers = self._target_submit_workers()
        reserve = max(1, int(round(workers * _OWNER_RESERVE_RATIO)))
        fair_share = max(1, workers // (1 + contenders))
        return max(1, min(ceiling, workers - reserve, fair_share))

    def _warm_account_lease_pool_locked(self) -> None:
        """Warm the account lease pool.

        Despite the ``_locked`` suffix this is also called *without* the lock held (from
        ``_run_task``), and it used to hand ``self._tasks`` itself to
        ``seed_queued_preferences``, which iterates it — a concurrent submit then raised
        ``RuntimeError: dictionary changed size during iteration`` into the bare
        ``except`` below and silently skipped the warm-up (audit 28 §8). Snapshot under
        the (re-entrant) lock and iterate the copy instead.
        """
        if not image_pipeline_scheduler.enabled():
            return
        try:
            from services.image_pipeline.account_lease_pool import account_lease_pool

            with self._lock:
                tasks_snapshot = dict(self._tasks)
            queued = sum(1 for item in tasks_snapshot.values() if item.get("status") == TASK_STATUS_QUEUED)
            account_lease_pool.seed_queued_preferences(tasks_snapshot)
            account_lease_pool.maintain(max_acquire=min(16, max(4, queued + 3)))
        except Exception:
            pass

    def _next_submit_task_locked(self) -> tuple[str, str, dict[str, Any], dict[str, object], str] | None:
        if self._deadlock_guard_tripped_locked():
            return None
        self._warm_account_lease_pool_locked()
        try:
            interval_ms = self._effective_submit_interval_ms()
        except Exception:
            interval_ms = 0
        if interval_ms > 0 and self._last_submit_start_ts > 0:
            elapsed = time.time() - float(self._last_submit_start_ts)
            if elapsed < (interval_ms / 1000.0):
                return None
        queued = [
            (key, task) for key, task in self._tasks.items() if task.get("status") == TASK_STATUS_QUEUED
        ]
        if not queued:
            # Idle loops run this every 0.5s per worker; there is nothing to rank.
            return None
        running_by_owner: dict[str, int] = {}
        for item in self._tasks.values():
            if item.get("status") != TASK_STATUS_RUNNING or self._is_resume_polling_task(item):
                continue
            owner_id = _clean(item.get("owner_id"))
            running_by_owner[owner_id] = running_by_owner.get(owner_id, 0) + 1
        # Dispatch was global FIFO by created_ts with no notion of the owner, so an owner
        # with a long queue held the head of the line for everyone. Ordering by the
        # owner's current occupancy first keeps FIFO *within* an owner while giving the
        # least-served owner the next free worker (audit 28 §A4-7).
        candidates = sorted(
            queued,
            key=lambda item: (
                running_by_owner.get(_clean(item[1].get("owner_id")), 0),
                float(item[1].get("created_ts") or 0.0),
            ),
        )
        owner_caps: dict[str, int] = {}
        for key, task in candidates:
            owner = _clean(task.get("owner_id"))
            if owner not in owner_caps:
                owner_caps[owner] = self._owner_running_cap_locked(owner)
            if running_by_owner.get(owner, 0) >= owner_caps[owner]:
                continue
            payload = task.get("payload")
            identity = task.get("identity")
            if not isinstance(payload, dict) or not isinstance(identity, dict):
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "服务已重启，未完成的图片任务已中断"
                task["updated_at"] = _now_iso()
                task["updated_ts"] = time.time()
                self._save_task_locked(key)
                continue
            if self._sync_ladder_exhausted(key):
                # Sync caller already gave up while this was queued; starting it would
                # spend an account slot and quota on a response nobody can receive.
                task["status"] = TASK_STATUS_ERROR
                task["progress"] = "failed"
                task["error"] = "同步调用方等待预算已耗尽，排队任务不再启动"
                task["updated_at"] = _now_iso()
                task["updated_ts"] = time.time()
                self._save_task_locked(key)
                continue
            now_ts = time.time()
            task.update({
                "status": TASK_STATUS_RUNNING,
                "progress": "submitting",
                "error": "",
                "updated_at": _now_iso(),
                "updated_ts": now_ts,
                "started_ts": now_ts,
                "worker_started_ts": now_ts,
            })
            self._save_task_locked(key)
            self._last_submit_start_ts = now_ts
            return key, _clean(task.get("mode"), "generate"), dict(payload), dict(identity), _clean(task.get("model"), "gpt-image-2")
        return None

    def _submit_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task_key = ""
            try:
                self._maybe_reap_stuck_running_tasks()
                with self._condition:
                    run_args = self._next_submit_task_locked()
                    if run_args is None:
                        wait_secs = 0.5
                        try:
                            interval_ms = self._effective_submit_interval_ms()
                        except Exception:
                            interval_ms = 0
                        if interval_ms > 0 and self._last_submit_start_ts > 0:
                            remaining = (interval_ms / 1000.0) - (time.time() - float(self._last_submit_start_ts))
                            if 0 < remaining < wait_secs:
                                wait_secs = remaining
                        if image_pipeline_scheduler.enabled():
                            self._warm_account_lease_pool_locked()
                        self._condition.wait(timeout=max(0.05, wait_secs))
                        continue
                task_key, mode, payload, identity, model = run_args
                self._run_task(task_key, mode, payload, identity, model)
            except Exception as exc:
                # _next_submit_task_locked() already flipped this task to RUNNING and
                # persisted it, so an escape here used to kill the worker thread and
                # strand the task in RUNNING forever. Known escape routes: the explicit
                # re-raise for a non-"queue is full" RuntimeError from begin_run, a
                # non-RuntimeError from begin_run (ValueError from int(payload["n"]),
                # TypeError from normalize_multi_image_mode), "can't start new thread",
                # and sqlite3.OperationalError from _update_task/_log_call inside an
                # except block. Terminalise the *task*; keep the worker (audit 28 §B10).
                self._handle_worker_loop_crash(task_key, exc, worker="submit")
            else:
                self._note_worker_loop_ok()
            try:
                with self._condition:
                    self._condition.notify_all()
            except Exception:
                pass

    # ------------------------------------------------------------------ §B10 fixes

    def _handle_worker_loop_crash(self, key: str, exc: BaseException, *, worker: str) -> None:
        """Turn a worker-loop escape into a terminal task instead of a dead thread."""
        detail = _clean(str(exc)) or exc.__class__.__name__
        message = f"image {worker} worker aborted: {exc.__class__.__name__}: {detail}"[:500]
        if key:
            try:
                self._release_task_runtime_resources(key)
            except Exception:
                pass
            try:
                self._update_task(key, status=TASK_STATUS_ERROR, progress="failed", error=message)
            except Exception:
                # The persistence layer is what failed (locked DB / full disk). Keep the
                # in-memory row terminal anyway, otherwise it keeps consuming global,
                # per-user and per-owner dispatch capacity until restart.
                try:
                    with self._condition:
                        task = self._tasks.get(key)
                        if isinstance(task, dict):
                            task["status"] = TASK_STATUS_ERROR
                            task["progress"] = "failed"
                            task["error"] = message
                            task["updated_at"] = _now_iso()
                            task["updated_ts"] = time.time()
                        self._cancel_events.pop(key, None)
                        self._condition.notify_all()
                except Exception:
                    pass
        streak = max(1, int(getattr(self._worker_crash_state, "streak", 0)) + 1)
        self._worker_crash_state.streak = streak
        try:
            log_service.add(
                LOG_TYPE_CALL,
                f"图片任务 worker 异常兜底（{worker}）",
                {"task_key": key or "", "status": "failed", "error": message, "crash_streak": streak},
            )
        except Exception:
            pass
        # Never spin: an immortal loop against a permanently broken dependency would
        # otherwise burn the CPU and hammer the failing resource.
        backoff = min(_WORKER_CRASH_BACKOFF_MAX_SECS, _WORKER_CRASH_BACKOFF_SECS * (2 ** (streak - 1)))
        self._stop_event.wait(backoff)

    def _note_worker_loop_ok(self) -> None:
        self._worker_crash_state.streak = 0

    def _pop_active_pipeline_run(self, key: str) -> object | None:
        with self._lock:
            return self._active_pipeline_runs.pop(key, None)

    def _release_task_runtime_resources(self, key: str) -> None:
        """Give back everything a task holds outside its own status field.

        Status alone is not enough: the account in-flight slot, the pipeline admission
        permit and the sS/upload/download slots all live elsewhere, and the watchdog
        counts a stuck RUNNING task as *expected* in-flight, so the leak looks
        legitimate (audit 28 §B10).
        """
        with self._lock:
            task = self._tasks.get(key)
            task_snapshot = dict(task) if isinstance(task, dict) else {}
            cancel_event = self._cancel_events.get(key)
        if cancel_event is not None:
            try:
                cancel_event.set()
            except Exception:
                pass
        pipeline_run = self._pop_active_pipeline_run(key)
        tokens: set[str] = set()
        for field in ("resume_access_token", "access_token"):
            token = _clean(task_snapshot.get(field))
            if token:
                tokens.add(token)
        if pipeline_run is not None:
            bound = _clean(getattr(pipeline_run, "_account_access_token", ""))
            if bound:
                tokens.add(bound)
            try:
                # Idempotent: decrements the pipeline admission counter exactly once and
                # sweeps any sS / upload / download slot the dead worker left behind.
                pipeline_run.finish()
            except Exception:
                pass
        if tokens:
            self._force_release_image_slots(tokens)

    def _stuck_running_bound_secs(self, task: dict[str, Any]) -> float:
        """Wall-clock ceiling past which a RUNNING task can only be stranded.

        Built from the legitimate per-mode hard timeout (`_task_hard_timeout_secs`, i.e.
        ~225s generation / ~435s single-ref edit / ~495s multi-ref) plus a proportional
        margin covering the cancel grace, the terminal DB write and the resume ladder —
        deliberately not an independent magic number.
        """
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        try:
            bound = float(self._task_hard_timeout_secs(payload))
        except Exception:
            bound = 900.0
        if self._is_resume_polling_task(task):
            try:
                resume_bound = self._resume_poll_hard_timeout_secs(
                    float(task.get("resume_timeout_secs") or self.timeout_pending_poll_secs_getter())
                )
            except Exception:
                resume_bound = 0.0
            bound = max(bound, float(resume_bound))
        margin = max(
            _STUCK_RUNNING_MARGIN_MIN_SECS,
            min(_STUCK_RUNNING_MARGIN_MAX_SECS, bound * _STUCK_RUNNING_MARGIN_RATIO),
        )
        return bound + margin

    def _stuck_running_since_ts(self, task: dict[str, Any]) -> float:
        """Latest sign of life for a RUNNING task (0.0 when unknown → never reaped).

        ``_update_task`` bumps ``updated_ts`` on every progress callback, so a healthy
        long-running task keeps pushing its own deadline out and a genuinely dead worker
        stops doing so.
        """
        candidates = [
            float(task.get("updated_ts") or 0.0),
            float(task.get("worker_started_ts") or 0.0),
            float(task.get("started_ts") or 0.0),
        ]
        newest = max(candidates)
        if newest > 0:
            return newest
        return float(task.get("created_ts") or 0.0)

    def _maybe_reap_stuck_running_tasks(self) -> list[str]:
        # Called from every iteration of every worker loop, so the throttle check stays
        # lock-free: reading a float is atomic under the GIL, and losing the race only
        # costs a duplicate scan, which is idempotent.
        now = time.time()
        if now - float(self._last_reap_ts or 0.0) < _REAP_INTERVAL_SECS:
            return []
        with self._lock:
            if now - float(self._last_reap_ts or 0.0) < _REAP_INTERVAL_SECS:
                return []
            self._last_reap_ts = now
        return self.reap_stuck_running_tasks()

    def reap_stuck_running_tasks(self, *, now: float | None = None) -> list[str]:
        """Terminalise RUNNING tasks whose worker can no longer be alive.

        Nothing else reclaims them: ``_recover_unfinished_locked`` runs once behind the
        ``_runtime_recovered`` latch and ``_cleanup_locked`` only evicts
        ``TERMINAL_STATUSES`` (audit 28 §B10).
        """
        current = float(now if now is not None else time.time())
        stuck: list[tuple[str, float]] = []
        with self._lock:
            for key, task in list(self._tasks.items()):
                if _clean(task.get("status")) != TASK_STATUS_RUNNING:
                    continue
                since = self._stuck_running_since_ts(task)
                if since <= 0:
                    continue
                bound = self._stuck_running_bound_secs(task)
                stalled_for = current - since
                if stalled_for > bound:
                    stuck.append((key, stalled_for))
        reaped: list[str] = []
        for key, stalled_for in stuck:
            message = (
                f"image task reaped: stuck in {TASK_STATUS_RUNNING} for {stalled_for:.0f}s "
                "with no live worker"
            )
            try:
                self._release_task_runtime_resources(key)
            except Exception:
                pass
            try:
                self._update_task(
                    key,
                    status=TASK_STATUS_ERROR,
                    progress="failed",
                    error=message,
                    reaped_stuck_running=True,
                )
            except Exception:
                continue
            reaped.append(key)
            try:
                log_service.add(
                    LOG_TYPE_CALL,
                    "图片任务卡死回收",
                    {"task_key": key, "status": "failed", "error": message, "stalled_secs": int(stalled_for)},
                )
            except Exception:
                pass
        return reaped

    def _task_hard_timeout_secs(self, payload: dict[str, Any]) -> float:
        explicit = payload.get("task_hard_timeout_secs")
        if explicit is not None:
            try:
                return max(0.1, min(900.0, float(explicit)))
            except Exception:
                pass
        try:
            pre_conversation = max(0.0, float(getattr(config, "image_pre_conversation_timeout_secs", 60.0)))
        except Exception:
            pre_conversation = 60.0
        try:
            poll_timeout = max(0.0, float(payload.get("poll_timeout_secs") or config.image_generation_poll_timeout_secs))
        except Exception:
            poll_timeout = 180.0
        # 真实生图需要给上游收尾、下载和日志回写留余量；测试可用极小 timeout 快速覆盖。
        overhead = max(0.5, min(90.0, poll_timeout * 0.5))
        return max(1.0, min(900.0, pre_conversation + poll_timeout + overhead))

    def _run_task(self, key: str, mode: str, payload: dict[str, Any], identity: dict[str, object], model: str) -> None:
        started = time.time()
        trace = schedule_trace.get(key)
        trace_token = schedule_trace.bind(trace)
        if trace is not None:
            trace.emit("task_worker_start")
        cancelled = threading.Event()
        finished = threading.Event()
        state_lock = threading.Lock()
        outcome: dict[str, Any] = {"payload_for_run": dict(payload)}
        leased_access_tokens: set[str] = set()
        released_access_tokens: set[str] = set()
        pipeline_run = None
        if image_pipeline_scheduler.enabled():
            self._warm_account_lease_pool_locked()
            try:
                pipeline_run = image_pipeline_scheduler.begin_run(task_key=key, mode=mode, payload=payload)
            except ImagePoolStarvedError as exc:
                self._requeue_task_for_pipeline_backpressure(key, str(exc))
                return
            except RuntimeError as exc:
                if "queue is full" in str(exc).lower():
                    self._requeue_task_for_pipeline_backpressure(key, str(exc))
                    return
                raise
        if pipeline_run is not None:
            pipeline_run.persist_hook = lambda **fields: self._update_task(key, **fields)
            # Publish the run so the stuck-RUNNING reaper can still hand back its
            # pipeline accounting if this worker never reaches the finally below.
            with self._lock:
                self._active_pipeline_runs[key] = pipeline_run
        try:
            self._run_task_body(
                key,
                mode,
                payload,
                identity,
                model,
                started,
                cancelled,
                finished,
                state_lock,
                outcome,
                leased_access_tokens,
                released_access_tokens,
                pipeline_run,
            )
        finally:
            phase_timings = self._finalize_pipeline_run(key, pipeline_run)
            trace_payload = self._finalize_schedule_trace(key)
            schedule_trace.unbind(trace_token)
            pending = outcome.get("pending_call_log")
            if isinstance(pending, dict):
                if isinstance(phase_timings, dict) and phase_timings:
                    pending["phase_timings_ms"] = phase_timings
                if isinstance(trace_payload, dict) and trace_payload:
                    pending["schedule_trace"] = trace_payload
        self._emit_pending_call_log(key, outcome, identity, mode, model, started)

    def _finalize_schedule_trace(self, key: str) -> dict[str, Any] | None:
        trace = schedule_trace.pop(key)
        if trace is None:
            return None
        try:
            trace.emit("task_terminal")
            payload = trace.finish()
            self._update_task(key, schedule_trace=payload)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _emit_pending_call_log(
        self,
        key: str,
        outcome: dict[str, Any],
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
    ) -> None:
        pending = outcome.get("pending_call_log")
        if not isinstance(pending, dict):
            return
        suffix = _clean(pending.get("suffix"), "调用完成")
        pending_usage = pending.get("usage")
        usage = pending_usage if isinstance(pending_usage, dict) else None
        self._log_call(
            identity,
            mode,
            model,
            started,
            suffix,
            request_preview=_clean(pending.get("request_preview")),
            status=_clean(pending.get("status"), "success"),
            error=_clean(pending.get("error")),
            urls=pending.get("urls") if isinstance(pending.get("urls"), list) else None,
            account_email=_clean(pending.get("account_email")),
            task_key=key,
            usage=usage,
            traffic_fields=_call_log_traffic_fields(pending),
            phase_timings_ms=pending.get("phase_timings_ms")
            if isinstance(pending.get("phase_timings_ms"), dict)
            else None,
            schedule_trace_payload=pending.get("schedule_trace")
            if isinstance(pending.get("schedule_trace"), dict)
            else None,
        )

    def _finalize_pipeline_run(self, key: str, pipeline_run: object | None) -> dict[str, int] | None:
        if pipeline_run is None:
            # Nothing was ever published for this key (pipeline disabled, or begin_run
            # failed), so there is no registry entry to reclaim.
            return None
        with self._lock:
            self._active_pipeline_runs.pop(key, None)
        try:
            # finish() is idempotent, so a run the reaper already reclaimed is a no-op
            # here rather than a double-decrement of the global admission counter.
            timings = pipeline_run.finish().to_dict()
            self._update_task(key, phase_timings_ms=timings, pipeline_phase="delivered")
            return timings if isinstance(timings, dict) else None
        except Exception:
            return None

    def _task_call_log_urls(self, task: dict[str, Any]) -> list[str]:
        data = task.get("data")
        if not isinstance(data, list):
            return []
        urls: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            url = _clean(item.get("url"))
            if url:
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _task_call_log_request_preview(self, task: dict[str, Any]) -> str:
        payload = task.get("payload")
        if isinstance(payload, dict):
            return request_text(payload.get("prompt"))
        return ""

    def _leased_tokens_for_release(
        self,
        *,
        leased_access_tokens: set[str],
        outcome: dict[str, Any],
        pipeline_run: object | None,
    ) -> set[str]:
        tokens = {str(t).strip() for t in leased_access_tokens if str(t).strip()}
        resume = _clean(outcome.get("access_token"))
        if resume:
            tokens.add(resume)
        if pipeline_run is not None:
            bound = _clean(getattr(pipeline_run, "_account_access_token", ""))
            if bound:
                tokens.add(bound)
        return tokens

    def _wait_for_runner(
        self,
        key: str,
        finished: threading.Event,
        granted_secs: float,
    ) -> tuple[bool, float]:
        """Wait for the runner, bounded by the hard timeout *and* the sync ladder.

        The wall used to be checked only when an attempt was dispatched, so an attempt
        that started just inside the deadline still ran its full hard timeout (audit 28
        §B7). Waiting in slices also lets a sync waiter that attaches after dispatch
        bound the attempt already in flight.

        Returns ``(finished, granted_secs)`` where ``granted_secs`` is the budget this
        attempt actually got — that is what the timeout message reports.
        """
        start = time.time()
        granted = max(0.1, float(granted_secs))
        while True:
            now = time.time()
            elapsed = max(0.0, now - start)
            remaining = granted - elapsed
            ladder = self._sync_ladder_remaining_secs(key, now=now)
            if ladder is not None and ladder < remaining:
                remaining = ladder
                granted = elapsed + max(0.0, ladder)
            if remaining <= 0:
                return finished.is_set(), max(0.1, granted)
            if finished.wait(timeout=min(remaining, _SYNC_LADDER_RECHECK_INTERVAL_SECS)):
                return True, max(0.1, granted)

    def _run_task_body(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
        started: float,
        cancelled: threading.Event,
        finished: threading.Event,
        state_lock: threading.Lock,
        outcome: dict[str, Any],
        leased_access_tokens: set[str],
        released_access_tokens: set[str],
        pipeline_run: object | None,
    ) -> None:
        with self._lock:
            self._cancel_events[key] = cancelled
            # 若用户在入队后、开跑前已取消，任务可能已是 error
            existing = self._tasks.get(key)
            if existing is not None and _clean(existing.get("status")) in TERMINAL_STATUSES:
                self._cancel_events.pop(key, None)
                return
            if existing is not None and _clean(existing.get("error")) == "cancelled by user":
                self._cancel_events.pop(key, None)
                return

        def claim_release(access_token: str) -> bool:
            token = _clean(access_token)
            if not token:
                return False
            with state_lock:
                if token in released_access_tokens:
                    return False
                released_access_tokens.add(token)
                leased_access_tokens.discard(token)
                return True

        def release_slot_once(access_token: str) -> bool:
            token = _clean(access_token)
            if not claim_release(token):
                return False
            try:
                account_service.release_image_slot(token)
                return True
            except Exception:
                with state_lock:
                    released_access_tokens.discard(token)
                return False

        def progress_callback(step: object) -> None:
            progress = ""
            access_token = ""
            conversation_id = ""
            if isinstance(step, dict):
                progress = _clean(step.get("step") or step.get("progress"))
                access_token = _clean(step.get("access_token"))
                conversation_id = _clean(step.get("conversation_id"))
            else:
                progress = _clean(step)

            late_cancelled_token = ""
            with state_lock:
                if access_token:
                    leased_access_tokens.add(access_token)
                    outcome["access_token"] = access_token
                if cancelled.is_set():
                    late_cancelled_token = access_token
                else:
                    if conversation_id:
                        outcome["conversation_id"] = conversation_id

                    updates: dict[str, Any] = {}
                    if progress:
                        updates["progress"] = progress
                    if progress == "image_stream_resolve_start":
                        updates["resolve_started_ts"] = time.time()
                    if conversation_id:
                        updates["conversation_id"] = conversation_id
                    resume_access_token = _clean(outcome.get("access_token"))
                    if conversation_id and resume_access_token:
                        updates["resume_access_token"] = resume_access_token
                    if updates:
                        # 与 hard-timeout 分支共用 state_lock，防止迟到的进度回调
                        # 在 timeout_pending/error 终态之后把 progress 覆盖回去。
                        self._update_task(key, **updates)

            # get_available_access_token 可能在 hard-timeout 之后才返回。
            # 迟到 token 属于已经结束的任务，需立即归还其并发槽位。
            if late_cancelled_token:
                release_slot_once(late_cancelled_token)
            if cancelled.is_set():
                return

        def run_handler() -> None:
            child_trace = schedule_trace.get(key)
            child_token = schedule_trace.bind(child_trace)
            try:
                payload_for_run = self._resolve_payload_assets(mode, payload, identity, pipeline_run=pipeline_run)
                with state_lock:
                    outcome["payload_for_run"] = payload_for_run
                prefer = _clean(payload_for_run.get("preferred_account_email") or payload.get("preferred_account_email"))
                if prefer:
                    from services.request_account_context import set_preferred_account_email

                    set_preferred_account_email(prefer)
                payload_with_progress = {
                    **payload_for_run,
                    "progress_callback": progress_callback,
                    "cancel_event": cancelled,
                    "pipeline_run": pipeline_run,
                }
                handler = self.edit_handler if mode == "edit" else self.generation_handler
                result = handler(payload_with_progress)
                with state_lock:
                    outcome["result"] = result
            except Exception as exc:
                with state_lock:
                    outcome["exception"] = exc
            finally:
                schedule_trace.unbind(child_token)
                finished.set()

        thread = threading.Thread(target=run_handler, name=f"image-task-runner-{key.replace(':', '-')}", daemon=True)
        thread.start()
        completed, hard_timeout_secs = self._wait_for_runner(
            key,
            finished,
            self._effective_task_hard_timeout_secs(key, payload),
        )
        if not completed:
            cancelled.set()
            try:
                cancel_grace_secs = max(0.0, min(5.0, float(payload.get("cancel_grace_secs") or 1.0)))
            except Exception:
                cancel_grace_secs = 1.0
            finished.wait(timeout=cancel_grace_secs)
            if finished.is_set():
                thread.join(timeout=0.1)
            runner_alive_after_cancel = thread.is_alive()
            duration_ms = int((time.time() - started) * 1000)
            with state_lock:
                payload_for_run = outcome.get("payload_for_run") if isinstance(outcome.get("payload_for_run"), dict) else payload
                conversation_id = _clean(outcome.get("conversation_id"))
                resume_access_token = _clean(outcome.get("access_token"))
                leased_tokens = self._leased_tokens_for_release(
                    leased_access_tokens=leased_access_tokens,
                    outcome=outcome,
                    pipeline_run=pipeline_run,
                )

            resume_worth_it = True
            resume_remaining = self._sync_ladder_remaining_secs(key)
            if resume_remaining is not None and resume_remaining < _SYNC_LADDER_MIN_ATTEMPT_SECS:
                # The sync caller's budget is gone: a resume poll could only hit upstream
                # with the same access token for a response nobody can receive, so this
                # is where the ladder stops instead of queueing 1–4 more attempts.
                resume_worth_it = False
            if conversation_id and resume_worth_it:
                error_message = (
                    f"image task hard timeout after upstream conversation capture ({hard_timeout_secs:.1f}s); "
                    "background resume polling scheduled"
                )
                for leased_token in leased_tokens:
                    try:
                        account_service.record_image_transient_backoff(leased_token, error_message)
                    except Exception:
                        pass
                force_released = sum(1 for token in leased_tokens if release_slot_once(token))
                resume_timeout_secs = max(
                    float(payload.get("resume_timeout_secs") or 0.0),
                    float(payload.get("poll_timeout_secs") or 0.0),
                    float(self.timeout_pending_poll_secs_getter()),
                )
                with state_lock:
                    self._update_task(
                        key,
                        status=TASK_STATUS_TIMEOUT_PENDING,
                        progress="timeout_pending",
                        error=error_message,
                        data=[],
                        duration_ms=duration_ms,
                        hard_timeout_secs=hard_timeout_secs,
                        cancel_grace_secs=cancel_grace_secs,
                        runner_alive_after_cancel=runner_alive_after_cancel,
                        force_released_inflight_count=force_released,
                        conversation_id=conversation_id,
                        resume_timeout_secs=resume_timeout_secs,
                        **({"resume_access_token": resume_access_token} if resume_access_token else {}),
                        next_resume_ts=time.time() + self._resume_delay_secs(1),
                    )
                self._log_call(
                    identity,
                    mode,
                    model,
                    started,
                    "调用硬超时待续轮询",
                    request_preview=request_text(payload_for_run.get("prompt")),
                    status="timeout_pending",
                    error=error_message,
                )
                return

            if conversation_id and not resume_worth_it:
                error_message = (
                    f"image task hard timeout after upstream conversation capture ({hard_timeout_secs:.1f}s); "
                    "sync client budget exhausted, resume polling skipped"
                )
            else:
                error_message = f"image task hard timeout before upstream completion ({hard_timeout_secs:.1f}s); no conversation_id captured"
            released_count = 0
            leased_tokens = self._leased_tokens_for_release(
                leased_access_tokens=leased_access_tokens,
                outcome=outcome,
                pipeline_run=pipeline_run,
            )
            for leased_token in leased_tokens:
                if not claim_release(leased_token):
                    continue
                try:
                    account_service.record_image_transient_backoff(leased_token, error_message)
                except Exception:
                    pass
                try:
                    # mark_image_result() 已负责释放账号在途槽位。只有它抛错时，
                    # 才交给后面的兜底强释，避免同一 token 被释放两次。
                    account_service.mark_image_result(leased_token, False)
                    released_count += 1
                except Exception:
                    try:
                        account_service.release_image_slot(leased_token)
                        released_count += 1
                    except Exception:
                        with state_lock:
                            released_access_tokens.discard(leased_token)
            force_released = released_count
            with state_lock:
                self._update_task(
                    key,
                    status=TASK_STATUS_ERROR,
                    progress="failed",
                    error=error_message,
                    data=[],
                    duration_ms=duration_ms,
                    hard_timeout_secs=hard_timeout_secs,
                    cancel_grace_secs=cancel_grace_secs,
                    runner_alive_after_cancel=runner_alive_after_cancel,
                    force_released_inflight_count=force_released,
                    # Kept so the operator-triggered resume_poll() escape hatch still has
                    # something to resume, even though the automatic ladder stopped here.
                    **({"conversation_id": conversation_id} if conversation_id else {}),
                )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用硬超时",
                request_preview=request_text(payload_for_run.get("prompt")),
                status="failed",
                error=error_message,
            )
            return

        with state_lock:
            payload_for_run = outcome.get("payload_for_run") if isinstance(outcome.get("payload_for_run"), dict) else payload
            result = outcome.get("result")
            exception = outcome.get("exception")
        try:
            if exception is not None:
                raise exception
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            account_email = _clean(result.get("_account_email") or result.get("account_email"))
            if not isinstance(data, list) or not data:
                upstream = _clean(result.get("message"))
                message = upstream or "号池中没有可用账号或所有账号均被限流，请检查号池状态（账号额度、是否被封禁、是否到达生图上限）"
                error = RuntimeError(message)
                if account_email:
                    setattr(error, "account_email", account_email)
                raise error
            usage = result.get("usage")
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_SUCCESS, progress="success", data=data, usage=usage, error="", duration_ms=duration_ms)
            self.note_success_duration_ms(duration_ms)
            outcome["pending_call_log"] = {
                "suffix": "调用完成",
                "request_preview": request_text(payload_for_run.get("prompt")),
                "urls": _collect_image_urls(data),
                "account_email": account_email,
                "status": "success",
                **({"usage": usage} if isinstance(usage, dict) else {}),
                **_call_log_traffic_fields(result),
            }
        except Exception as exc:
            error_message = str(exc) or "image task failed"
            account_email = _clean(getattr(exc, "account_email", ""))
            conversation_id = _clean(getattr(exc, "conversation_id", ""))
            code = _clean(getattr(exc, "code", ""))
            duration_ms = int((time.time() - started) * 1000)
            ladder_left = self._sync_ladder_remaining_secs(key)
            resume_affordable = ladder_left is None or ladder_left >= _SYNC_LADDER_MIN_ATTEMPT_SECS
            if (
                conversation_id
                and resume_affordable
                and (code == "image_timeout_pending" or _looks_like_timeout(error_message))
            ):
                resume_timeout_secs = max(
                    float(payload.get("resume_timeout_secs") or 0.0),
                    float(payload.get("poll_timeout_secs") or 0.0),
                    float(self.timeout_pending_poll_secs_getter()),
                )
                resume_access_token = _clean(getattr(exc, "access_token", ""))
                self._update_task(
                    key,
                    status=TASK_STATUS_TIMEOUT_PENDING,
                    progress="timeout_pending",
                    error=error_message,
                    data=[],
                    duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    resume_timeout_secs=resume_timeout_secs,
                    **({"resume_access_token": resume_access_token} if resume_access_token else {}),
                    next_resume_ts=time.time() + self._resume_delay_secs(1),
                )
                self._log_call(
                    identity,
                    mode,
                    model,
                    started,
                    "调用超时待续轮询",
                    request_preview=request_text(payload_for_run.get("prompt")),
                    status="timeout_pending",
                    error=error_message,
                    account_email=account_email,
                )
                return
            self._update_task(
                key,
                status=TASK_STATUS_ERROR,
                progress="failed",
                error=error_message,
                data=[],
                duration_ms=duration_ms,
                **({"conversation_id": conversation_id} if conversation_id else {}),
            )
            self._log_call(identity, mode, model, started, "调用失败", request_preview=request_text(payload_for_run.get("prompt")), status="failed", error=error_message, account_email=account_email, task_key=key)

    def _force_release_image_slots(self, access_tokens: set[str] | list[str] | tuple[str, ...]) -> int:
        released = 0
        seen: set[str] = set()
        for token in access_tokens:
            access_token = _clean(token)
            if not access_token or access_token in seen:
                continue
            seen.add(access_token)
            try:
                account_service.release_image_slot(access_token)
                released += 1
            except Exception:
                pass
        return released

    def _resolve_payload_assets(
        self,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        *,
        pipeline_run: object | None = None,
    ) -> dict[str, Any]:
        if mode != "edit":
            return dict(payload)
        upload_acquired = False
        if pipeline_run is not None:
            try:
                getattr(pipeline_run, "acquire_upload")()
                upload_acquired = True
            except Exception:
                pass
        try:
            resolved = dict(payload)
            image_asset_ids = [str(item).strip() for item in (resolved.get("image_asset_ids") or []) if str(item).strip()]
            mask_asset_ids = [str(item).strip() for item in (resolved.get("mask_asset_ids") or []) if str(item).strip()]
            if not image_asset_ids and not mask_asset_ids:
                return resolved
            from services.image_asset_service import image_asset_service

            if image_asset_ids:
                existing_images = resolved.get("images") if isinstance(resolved.get("images"), list) else []
                resolved["images"] = [*existing_images, *image_asset_service.read_assets(identity, image_asset_ids)]
            if mask_asset_ids:
                existing_masks = resolved.get("mask") if isinstance(resolved.get("mask"), list) else []
                resolved["mask"] = [*existing_masks, *image_asset_service.read_assets(identity, mask_asset_ids)]
            resolved.pop("image_asset_ids", None)
            resolved.pop("mask_asset_ids", None)
            return resolved
        finally:
            if upload_acquired and pipeline_run is not None:
                try:
                    getattr(pipeline_run, "release_upload")()
                except Exception:
                    pass

    def _next_poll_task_locked(self) -> tuple[str, str, float, dict[str, object], str, str, str] | None:
        now = time.time()
        try:
            max_attempts = max(1, int(self.timeout_pending_max_attempts_getter()))
        except Exception:
            max_attempts = 3
        candidates = sorted(
            ((key, task) for key, task in self._tasks.items() if task.get("status") == TASK_STATUS_TIMEOUT_PENDING),
            key=lambda item: float(item[1].get("next_resume_ts") or item[1].get("updated_ts") or 0.0),
        )
        for key, task in candidates:
            conversation_id = _clean(task.get("conversation_id"))
            if not conversation_id:
                task.update(status=TASK_STATUS_ERROR, error="timeout_pending task has no conversation_id", updated_at=_now_iso(), updated_ts=now)
                self._save_task_locked(key)
                continue
            ladder_deadline = 0.0
            try:
                ladder_deadline = float(task.get("sync_ladder_deadline_ts") or 0.0)
            except (TypeError, ValueError):
                ladder_deadline = 0.0
            deadline = float(task.get("resume_deadline_ts") or 0.0)
            if deadline <= 0:
                anchor = float(task.get("updated_ts") or task.get("created_ts") or now)
                deadline = anchor + self._resume_wall_secs()
            if ladder_deadline > 0:
                # The client budget is the outer bound; the resume wall can only make it
                # tighter, never looser (audit 28 §B7).
                deadline = min(deadline, ladder_deadline)
            task["resume_deadline_ts"] = deadline
            # A backoff that lands past the wall is dead on arrival: waiting for it only
            # keeps the task non-terminal until some later pass notices.
            next_resume_ts = float(task.get("next_resume_ts") or 0.0)
            if now > deadline or (next_resume_ts > 0 and next_resume_ts > deadline):
                task.update(
                    status=TASK_STATUS_ERROR,
                    progress="failed",
                    error=f"续轮询总墙钟已超时取消（>{int(self._resume_wall_secs())}s）",
                    updated_at=_now_iso(),
                    updated_ts=now,
                )
                self._save_task_locked(key)
                continue
            if next_resume_ts > now:
                continue
            attempts = int(task.get("resume_attempts") or 0)
            if attempts >= max_attempts:
                task.update(status=TASK_STATUS_ERROR, error="续轮询次数已耗尽", updated_at=_now_iso(), updated_ts=now)
                self._save_task_locked(key)
                continue
            timeout_secs = float(task.get("resume_timeout_secs") or self.timeout_pending_poll_secs_getter())
            if ladder_deadline > 0:
                # Clamp this attempt's poll budget to what the client can still receive,
                # leaving the resolve/download margin `_resume_poll_hard_timeout_secs`
                # adds on top, so attempt end == deadline rather than deadline + 60s.
                affordable = (deadline - now) - _RESUME_POLL_OVERHEAD_SECS
                if (deadline - now) < _SYNC_LADDER_MIN_ATTEMPT_SECS:
                    task.update(
                        status=TASK_STATUS_ERROR,
                        progress="failed",
                        error="续轮询预算不足（同步调用方等待预算已耗尽）",
                        updated_at=_now_iso(),
                        updated_ts=now,
                    )
                    self._save_task_locked(key)
                    continue
                timeout_secs = max(5.0, min(timeout_secs, affordable))
            task.update(status=TASK_STATUS_RUNNING, progress="resume_polling", resume_attempts=attempts + 1, updated_at=_now_iso(), updated_ts=now)
            self._save_task_locked(key)
            access_token = _clean(task.get("resume_access_token"))
            identity = task.get("identity") if isinstance(task.get("identity"), dict) else {}
            return key, conversation_id, timeout_secs, dict(identity), _clean(task.get("mode"), "generate"), _clean(task.get("model"), "gpt-image-2"), access_token
        return None

    def _poll_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task_key = ""
            try:
                self._maybe_reap_stuck_running_tasks()
                with self._condition:
                    run_args = self._next_poll_task_locked()
                    if run_args is None:
                        self._condition.wait(timeout=1.0)
                        continue
                key, conversation_id, timeout_secs, identity, mode, model, access_token = run_args
                task_key = key
                self._run_resume_poll_with_hard_timeout(
                    key,
                    conversation_id,
                    timeout_secs,
                    identity,
                    mode,
                    model,
                    access_token=access_token,
                )
            except Exception as exc:
                # Same stranding hazard as the submit loop: _next_poll_task_locked()
                # already flipped the task to RUNNING/resume_polling (audit 28 §B10).
                self._handle_worker_loop_crash(task_key, exc, worker="poll")
            else:
                self._note_worker_loop_ok()
            try:
                with self._condition:
                    self._condition.notify_all()
            except Exception:
                pass

    def _resume_poll_hard_timeout_secs(self, timeout_secs: float) -> float:
        try:
            base_timeout = max(5.0, float(timeout_secs))
        except Exception:
            base_timeout = float(self.timeout_pending_poll_secs_getter())
        # backend._poll_image_results already has its own timeout.  The extra
        # margin covers final URL resolving/download, but prevents a resume
        # worker from staying "running" indefinitely after a public sync caller
        # has already hit Cloudflare/NewAPI timeout.
        return max(10.0, min(900.0, base_timeout + 60.0))

    def _run_resume_poll_with_hard_timeout(
        self,
        key: str,
        conversation_id: str,
        timeout_secs: float,
        identity: dict[str, object],
        mode: str,
        model: str,
        *,
        access_token: str = "",
    ) -> None:
        started = time.time()
        finished = threading.Event()

        def runner() -> None:
            try:
                self._run_resume_poll(
                    key,
                    conversation_id,
                    timeout_secs,
                    identity,
                    mode,
                    model,
                    access_token=access_token,
                )
            finally:
                finished.set()

        thread = threading.Thread(target=runner, name=f"image-resume-poll-{key.replace(':', '-')}", daemon=True)
        thread.start()
        completed, hard_timeout_secs = self._wait_for_runner(
            key,
            finished,
            self._effective_resume_poll_hard_timeout_secs(key, timeout_secs),
        )
        if completed:
            return

        error_message = f"image resume poll hard timeout ({hard_timeout_secs:.1f}s); conversation_id={conversation_id}"
        with self._condition:
            task = self._tasks.get(key)
            attempts = int(task.get("resume_attempts") or 0) if task else 0
            try:
                max_attempts = max(1, int(self.timeout_pending_max_attempts_getter()))
            except Exception:
                max_attempts = 3
            # Mid-attempt wall: this attempt was cut short *because* the client budget ran
            # out, so re-arming another one would be exactly the leak §B7 describes.
            budget_left = self._sync_ladder_remaining_secs(key)
            ladder_spent = budget_left is not None and budget_left < _SYNC_LADDER_MIN_ATTEMPT_SECS
            if ladder_spent:
                error_message = (
                    f"image resume poll cancelled at sync client deadline ({hard_timeout_secs:.1f}s); "
                    f"conversation_id={conversation_id}"
                )
            if task and attempts < max_attempts and not ladder_spent:
                now_ts = time.time()
                updates = {
                    "status": TASK_STATUS_TIMEOUT_PENDING,
                    "progress": "timeout_pending",
                    "error": error_message,
                    "data": [],
                    "duration_ms": int((now_ts - started) * 1000),
                    "next_resume_ts": now_ts + self._resume_delay_secs(attempts),
                    "updated_at": _now_iso(),
                    "updated_ts": now_ts,
                }
                if not float(task.get("resume_deadline_ts") or 0.0):
                    updates["resume_deadline_ts"] = self._resume_deadline_ts(now_ts, key=key)
                task.update(updates)
                self._save_task_locked(key)
                self._condition.notify_all()
                self._log_call(identity, mode, model, started, "续轮询硬超时待重试", status="timeout_pending", error=error_message)
                return
        self._update_task(
            key,
            status=TASK_STATUS_ERROR,
            progress="failed",
            error=error_message,
            data=[],
            duration_ms=int((time.time() - started) * 1000),
        )
        self._log_call(identity, mode, model, started, "续轮询硬超时", status="failed", error=error_message)

    def resume_poll(self, identity: dict[str, object], task_id: str, extra_timeout_secs: float = 30.0) -> dict[str, Any]:
        """把超时任务放回 timeout_pending 队列，后台继续轮询原 conversation_id。"""
        owner = _owner_id(identity)
        key = _task_key(owner, _clean(task_id))
        with self._condition:
            task = self._tasks.get(key)
            if task is None:
                raise ValueError("task not found")
            status = _clean(task.get("status"))
            if status not in {TASK_STATUS_ERROR, TASK_STATUS_TIMEOUT_PENDING}:
                raise ValueError("task is not resumable")
            error_text = _clean(task.get("error"))
            if status == TASK_STATUS_ERROR and "超时" not in error_text and "timeout" not in error_text.lower():
                raise ValueError("task error is not a timeout error")
            conversation_id = _clean(task.get("conversation_id"))
            if not conversation_id:
                raise ValueError("task has no conversation_id")
            task.update(
                status=TASK_STATUS_TIMEOUT_PENDING,
                progress="timeout_pending",
                error="",
                identity=dict(identity),
                resume_timeout_secs=max(5.0, min(600.0, float(extra_timeout_secs))),
                next_resume_ts=time.time(),
                resume_deadline_ts=self._resume_deadline_ts(key=key),
                updated_at=_now_iso(),
                updated_ts=time.time(),
            )
            self._save_task_locked(key)
            self._ensure_workers_locked()
            self._condition.notify_all()
            return _public_task(task)

    def _run_resume_poll(
        self,
        key: str,
        conversation_id: str,
        extra_timeout_secs: float,
        identity: dict[str, object],
        mode: str,
        model: str,
        *,
        access_token: str = "",
    ) -> None:
        started = time.time()
        backend = None
        try:
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol.conversation import format_image_result

            if not access_token:
                with self._condition:
                    task = self._tasks.get(key)
                    access_token = _clean(task.get("resume_access_token")) if task else ""
            backend = OpenAIBackendAPI(access_token=access_token) if access_token else OpenAIBackendAPI()
            file_ids, sediment_ids = backend._poll_image_results(conversation_id, extra_timeout_secs)
            if not file_ids and not sediment_ids:
                raise RuntimeError(f"继续等待 {extra_timeout_secs} 秒后仍未找到图片结果。")
            image_urls = backend.resolve_conversation_image_urls(conversation_id, file_ids, sediment_ids, poll=False)
            if not image_urls:
                raise RuntimeError("图片 URL 解析失败")
            image_items = [
                {"b64_json": base64.b64encode(image_data).decode("ascii")}
                for image_data in backend.download_image_bytes(image_urls[:1])
            ]
            data = format_image_result(image_items, "", "b64_json", "", int(time.time()))["data"]
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_SUCCESS, progress="success", data=data, error="", duration_ms=duration_ms)
            self.note_success_duration_ms(duration_ms)
            with self._condition:
                task = self._tasks.get(key) or {}
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成（续轮询）",
                status="success",
                urls=_collect_image_urls(data),
                task_key=key,
                usage=task.get("usage") if isinstance(task.get("usage"), dict) else None,
                traffic_fields=_call_log_traffic_fields(task),
            )
        except Exception as exc:
            error_message = str(exc) or "resume poll failed"
            with self._condition:
                task = self._tasks.get(key)
                attempts = int(task.get("resume_attempts") or 0) if task else 0
                try:
                    max_attempts = max(1, int(self.timeout_pending_max_attempts_getter()))
                except Exception:
                    max_attempts = 3
                should_retry = _looks_like_timeout(error_message) or _looks_like_token_invalid(error_message)
                budget_left = self._sync_ladder_remaining_secs(key)
                if budget_left is not None and budget_left < _SYNC_LADDER_MIN_ATTEMPT_SECS:
                    # Sync caller is gone; another attempt would spend account quota on a
                    # response that can no longer be delivered (audit 28 §B7).
                    should_retry = False
                if task and attempts < max_attempts and should_retry:
                    if _looks_like_token_invalid(error_message):
                        current_token = _clean(task.get("resume_access_token") or access_token)
                        if current_token:
                            refreshed_token = account_service.refresh_access_token(current_token, force=True, event="image_resume_poll")
                            if refreshed_token and refreshed_token != current_token:
                                task["resume_access_token"] = refreshed_token
                    task.update(
                        status=TASK_STATUS_TIMEOUT_PENDING,
                        progress="timeout_pending",
                        error=error_message,
                        data=[],
                        duration_ms=int((time.time() - started) * 1000),
                        next_resume_ts=time.time() + self._resume_delay_secs(attempts),
                        updated_at=_now_iso(),
                        updated_ts=time.time(),
                    )
                    if not float(task.get("resume_deadline_ts") or 0.0):
                        task["resume_deadline_ts"] = self._resume_deadline_ts(key=key)
                    self._save_task_locked(key)
                    self._condition.notify_all()
                    return
            self._update_task(key, status=TASK_STATUS_ERROR, progress="failed", error=error_message, data=[], duration_ms=int((time.time() - started) * 1000))
            self._log_call(identity, mode, model, started, "调用失败（续轮询）", status="failed", error=error_message)
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        request_preview: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
        account_email: str = "",
        task_key: str = "",
        usage: dict[str, Any] | None = None,
        traffic_fields: dict[str, int] | None = None,
        phase_timings_ms: dict[str, int] | None = None,
        schedule_trace_payload: dict[str, Any] | None = None,
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail: dict[str, Any] = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if error:
            detail["error"] = error
        if account_email:
            detail["account_email"] = account_email
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        task: dict[str, Any] = {}
        if task_key:
            with self._condition:
                task = dict(self._tasks.get(task_key) or {})
            detail["task_id"] = task.get("id")
            detail["worker_duration_ms"] = task.get("duration_ms")
            for timing_field in (
                "total_wall_ms",
                "task_queue_ms",
                "created_at",
                "worker_started_ts",
                "finished_ts",
            ):
                value = task.get(timing_field)
                if value is not None and value != "":
                    detail[timing_field] = value
            resolved_phase_timings = (
                phase_timings_ms
                if isinstance(phase_timings_ms, dict) and phase_timings_ms
                else task.get("phase_timings_ms")
            )
            if isinstance(resolved_phase_timings, dict) and resolved_phase_timings:
                detail["phase_timings_ms"] = dict(resolved_phase_timings)
                for phase_key, phase_ms in resolved_phase_timings.items():
                    if not str(phase_key).endswith("_ms"):
                        continue
                    try:
                        value = int(phase_ms or 0)
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        detail[f"phase_{phase_key}"] = value
            progress = _clean(task.get("progress"))
            if progress:
                detail["progress"] = progress
            pipeline_phase = _clean(task.get("pipeline_phase"))
            if pipeline_phase:
                detail["pipeline_phase"] = pipeline_phase
            resolved_schedule_trace = (
                schedule_trace_payload
                if isinstance(schedule_trace_payload, dict) and schedule_trace_payload
                else task.get("schedule_trace")
            )
            if isinstance(resolved_schedule_trace, dict) and resolved_schedule_trace:
                detail["schedule_trace"] = resolved_schedule_trace
        resolved_usage: dict[str, Any] | None = None
        if isinstance(usage, dict) and usage:
            resolved_usage = usage
        elif isinstance(task.get("usage"), dict) and task.get("usage"):
            resolved_usage = dict(task["usage"])
        usage_fields = _call_log_usage_fields(resolved_usage)
        if usage_fields:
            detail.update(usage_fields)
        phase_timings = detail.get("phase_timings_ms")
        sse_stream_ms = 0
        if isinstance(phase_timings, dict):
            try:
                sse_stream_ms = int(phase_timings.get("sse_stream_ms") or 0)
            except (TypeError, ValueError):
                sse_stream_ms = 0
        total_wall_ms = 0
        for source in (detail, task):
            value = _positive_int(source.get("total_wall_ms"))
            if value is not None:
                total_wall_ms = value
                break
        tokens_per_sec = _tokens_per_sec_from_sources(
            resolved_usage,
            sse_stream_ms=sse_stream_ms,
            total_wall_ms=total_wall_ms,
        )
        if tokens_per_sec is not None:
            detail["tokens_per_sec"] = tokens_per_sec
        traffic = dict(traffic_fields or {})
        for key, value in _call_log_traffic_fields(task).items():
            traffic.setdefault(key, value)
        if traffic:
            detail.update(traffic)
        try:
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass
        if task_key:
            with self._lock:
                mem_task = self._tasks.get(task_key)
                if isinstance(mem_task, dict) and _clean(mem_task.get("status")) in TERMINAL_STATUSES:
                    _compact_task_memory(mem_task)
                    self._tasks.pop(task_key, None)

    def _apply_terminal_timing_fields(self, task: dict[str, Any], *, finished_ts: float | None = None) -> None:
        """任务进入终态时补齐墙钟/排队/执行分段，供 API 与 call log 排查。"""
        now = float(finished_ts if finished_ts is not None else time.time())
        task["finished_ts"] = now
        created_ts = float(task.get("created_ts") or 0.0)
        worker_started = float(
            task.get("worker_started_ts")
            or task.get("started_ts")
            or task.get("updated_ts")
            or created_ts
            or now
        )
        if created_ts > 0:
            task["total_wall_ms"] = int(max(0.0, (now - created_ts) * 1000))
            task["task_queue_ms"] = int(max(0.0, (worker_started - created_ts) * 1000))
        worker_duration = task.get("duration_ms")
        if worker_duration is None and worker_started > 0:
            task["worker_duration_ms"] = int(max(0.0, (now - worker_started) * 1000))
        elif worker_duration is not None:
            task["worker_duration_ms"] = int(worker_duration)

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._condition:
            task = self._tasks.get(key)
            if task is None:
                return
            prev_status = _clean(task.get("status"))
            prev_error = _clean(task.get("error"))
            # 用户取消后禁止迟到的 success/timeout_pending 覆盖
            if prev_status == TASK_STATUS_ERROR and prev_error == "cancelled by user":
                next_status = _clean(updates.get("status"), prev_status)
                if next_status != TASK_STATUS_ERROR or _clean(updates.get("error"), prev_error) != "cancelled by user":
                    return
            task.update(updates)
            now = time.time()
            task["updated_at"] = _now_iso()
            task["updated_ts"] = now
            if (
                _clean(task.get("status")) == TASK_STATUS_TIMEOUT_PENDING
                and prev_status != TASK_STATUS_TIMEOUT_PENDING
                and not float(task.get("resume_deadline_ts") or 0.0)
            ):
                task["resume_deadline_ts"] = self._resume_deadline_ts(now, key=key)
            if _clean(task.get("status")) in TERMINAL_STATUSES:
                self._cancel_events.pop(key, None)
                self._apply_terminal_timing_fields(task)
            self._save_task_locked(key)
            self._condition.notify_all()

    def _init_db_locked(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_tasks (
                    key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_ts REAL,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_image_tasks_owner_status ON image_tasks(owner_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_image_tasks_updated ON image_tasks(updated_ts)")
            conn.commit()

    def _task_from_row(self, row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            task = _decode_from_json(json.loads(row["data"]))
        except Exception:
            return None
        if not isinstance(task, dict):
            return None
        task_id = _clean(task.get("id"))
        owner = _clean(task.get("owner_id"))
        if not task_id or not owner:
            return None
        return task

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT key, data FROM image_tasks
                WHERE status IN (?, ?, ?)
                """,
                (TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_TIMEOUT_PENDING),
            ).fetchall()
        tasks: dict[str, dict[str, Any]] = {}
        for row in rows:
            task = self._task_from_row(row)
            if task is None:
                continue
            task_id = _clean(task.get("id"))
            owner = _clean(task.get("owner_id"))
            if task_id and owner:
                tasks[_task_key(owner, task_id)] = task
        if rows:
            return tasks
        if not self.path.exists():
            return tasks
        legacy_tasks = self._load_legacy_json_locked()
        for key, task in legacy_tasks.items():
            self._tasks[key] = task
            self._save_task_locked(key)
        return legacy_tasks

    def _load_task_from_db_locked(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT key, data FROM image_tasks WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        task = self._task_from_row(row)
        if task is None:
            return None
        if task.get("status") in UNFINISHED_STATUSES:
            self._tasks[key] = task
        return task

    def _load_task_status_from_db_locked(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT owner_id, task_id, status, updated_ts
                FROM image_tasks
                WHERE key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        updated_ts = float(row["updated_ts"] or 0.0)
        updated_at = _iso_from_ts(updated_ts) if updated_ts > 0 else None
        return {
            "id": row["task_id"],
            "owner_id": row["owner_id"],
            "status": row["status"],
            "updated_ts": updated_ts,
            "updated_at": updated_at,
        }

    def _load_legacy_json_locked(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            status = _clean(item.get("status"))
            if status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_TIMEOUT_PENDING, TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}:
                status = TASK_STATUS_ERROR
            created_at = _clean(item.get("created_at"), _now_iso())
            updated_at = _clean(item.get("updated_at"), created_at)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": status,
                "mode": "edit" if item.get("mode") == "edit" else "generate",
                "model": _clean(item.get("model"), "gpt-image-2"),
                "size": _clean(item.get("size")),
                "quality": _clean(item.get("quality"), "auto"),
                "created_at": created_at,
                "updated_at": updated_at,
                "created_ts": item.get("created_ts") or _timestamp(created_at),
                "updated_ts": item.get("updated_ts") or _timestamp(updated_at),
                "started_ts": item.get("started_ts"),
                "duration_ms": item.get("duration_ms"),
                "progress": _clean(item.get("progress")),
                "resume_attempts": int(item.get("resume_attempts") or 0),
            }
            for field in ("data", "usage", "error", "conversation_id", "next_resume_ts", "resume_timeout_secs", "resume_access_token"):
                if item.get(field) is not None:
                    task[field] = item.get(field)
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_task_locked(self, key: str) -> None:
        task = self._tasks.get(key)
        if task is None:
            with self._connect() as conn:
                conn.execute("DELETE FROM image_tasks WHERE key = ?", (key,))
                conn.commit()
            return
        payload = json.dumps(_encode_for_json(task), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                INSERT INTO image_tasks(key, owner_id, task_id, status, updated_ts, data)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    task_id=excluded.task_id,
                    status=excluded.status,
                    updated_ts=excluded.updated_ts,
                    data=excluded.data
                """,
                (key, _clean(task.get("owner_id")), _clean(task.get("id")), _clean(task.get("status"), TASK_STATUS_ERROR), float(task.get("updated_ts") or time.time()), payload),
            )
            conn.commit()

    def _save_locked(self) -> None:
        for key in list(self._tasks.keys()):
            self._save_task_locked(key)

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for key, task in list(self._tasks.items()):
            status = task.get("status")
            task_changed = False
            if status == TASK_STATUS_QUEUED:
                if not isinstance(task.get("payload"), dict) or not isinstance(task.get("identity"), dict):
                    task["status"] = TASK_STATUS_ERROR
                    task["error"] = "服务已重启，未完成的图片任务已中断"
                    task_changed = True
            elif status == TASK_STATUS_RUNNING:
                cursor = _clean(task.get("retry_phase_cursor"))
                sediment_ids = task.get("sediment_ids") if isinstance(task.get("sediment_ids"), list) else []
                if cursor == "SS_DONE" and sediment_ids and _clean(task.get("conversation_id")):
                    task["status"] = TASK_STATUS_TIMEOUT_PENDING
                    task["progress"] = "timeout_pending"
                    task["next_resume_ts"] = time.time() + self._resume_delay_secs(1)
                    task["resume_deadline_ts"] = self._resume_deadline_ts(key=key)
                    task["error"] = task.get("error") or "服务已重启，从 SS_DONE 续下载"
                elif _clean(task.get("conversation_id")):
                    task["status"] = TASK_STATUS_TIMEOUT_PENDING
                    task["progress"] = "timeout_pending"
                    task["next_resume_ts"] = time.time() + self._resume_delay_secs(1)
                    task["resume_deadline_ts"] = self._resume_deadline_ts(key=key)
                    task["error"] = task.get("error") or "服务已重启，任务进入续轮询"
                elif isinstance(task.get("payload"), dict) and isinstance(task.get("identity"), dict):
                    task["status"] = TASK_STATUS_QUEUED
                    task["progress"] = "queued"
                    task["error"] = "服务已重启，任务已重新排队"
                else:
                    task["status"] = TASK_STATUS_ERROR
                    task["error"] = "服务已重启，未完成的图片任务已中断"
                task_changed = True
            elif status == TASK_STATUS_TIMEOUT_PENDING and not _clean(task.get("conversation_id")):
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "timeout_pending task has no conversation_id"
                task_changed = True
            if task_changed:
                task["updated_at"] = _now_iso()
                task["updated_ts"] = time.time()
                self._save_task_locked(key)
                changed = True
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        memory_cutoff = time.time() - TERMINAL_MEMORY_RETENTION_SECS
        removed_keys: list[str] = []
        memory_evict_keys: list[str] = []
        for key, task in list(self._tasks.items()):
            status = task.get("status")
            updated_ts = float(task.get("updated_ts") or _timestamp(task.get("updated_at")) or 0.0)
            if status in TERMINAL_STATUSES and updated_ts < cutoff:
                removed_keys.append(key)
            elif status in TERMINAL_STATUSES and updated_ts < memory_cutoff:
                memory_evict_keys.append(key)
        for key in memory_evict_keys:
            task = self._tasks.pop(key, None)
            if task is not None:
                _compact_task_memory(task)
        for key in removed_keys:
            self._tasks.pop(key, None)
            self._save_task_locked(key)
        removed_db_rows = 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM image_tasks
                WHERE status IN (?, ?)
                  AND updated_ts IS NOT NULL
                  AND updated_ts < ?
                """,
                (TASK_STATUS_SUCCESS, TASK_STATUS_ERROR, cutoff),
            )
            removed_db_rows = max(0, int(cursor.rowcount or 0))
            conn.commit()
        return bool(removed_keys or removed_db_rows)


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
