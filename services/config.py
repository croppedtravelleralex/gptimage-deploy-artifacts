from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
import time

from services.storage.base import StorageBackend

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"
BACKUP_STATE_FILE = DATA_DIR / "backup_state.json"

DEFAULT_BACKUP_INCLUDE = {
    "config": True,
    "register": True,
    "cpa": True,
    "sub2api": True,
    "logs": True,
    "image_tasks": True,
    "accounts_snapshot": True,
    "auth_keys_snapshot": True,
    "images": False,
}

DEFAULT_IMAGE_STORAGE = {
    "enabled": False,
    "mode": "local",
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    "webdav_root_path": "chatgpt2api/images",
    "public_base_url": "",
}

DEFAULT_CHAT_COMPLETION_CACHE = {
    "enabled": True,
    "ttl_seconds": 60,
    "max_entries": 256,
    "dedupe_inflight": True,
    "stream_cache": True,
    "normalize_messages": True,
    "drop_adjacent_duplicates": True,
    "drop_assistant_history": False,
}

DEFAULT_IMAGE_TASK_QUEUE = {
    "enabled": True,
    "submit_workers": 10,
    "submit_workers_max": 12,
    "poll_workers": 24,
    "download_workers": 4,
    "global_queue_max": 200,
    "per_user_running_max": 10,
    "per_user_running_base": 10,
    "per_user_running_burst": 12,
    "burst_enabled": False,
    "burst_min_queued": 6,
    "burst_min_dispatchable_candidates": 8,
    "burst_max_preflight_backoff": 0,
    "per_user_queue_max": 36,
    # 错峰启动，降低同 egress 突发 CF
    "submit_start_min_interval_ms": 1500,
    # inflight < cap 且 queued 可立即开跑时 interval=0（conc≤sse_slots 时 task_queue≈0）
    "submit_interval_adaptive": True,
    "timeout_pending_poll_secs": 180,
    "timeout_pending_max_attempts": 0,
    # 同步等待预算中保留给排队/投递/CDN 的比例。
    "sync_ladder_reserve_ratio": 0.10,
    "generation_poll_timeout_secs": 60,
    "edit_poll_timeout_secs": 480,
    "multi_reference_poll_timeout_secs": 360,
    "pre_conversation_timeout_secs": 45,
    "pre_conversation_max_attempts": 4,
    "pre_conversation_retry_backoff_secs": 1,
}

DEFAULT_IMAGE_PIPELINE = {
    "enabled": True,
    "prompt_slots": 10,
    "sse_slots": 10,
    "download_concurrency": 8,
    "asset_upload_concurrency": 8,
    "global_queue_max": 200,
    "relaxed_per_user_running": True,
    "aci_epsilon": 0.05,
    "ready_buffer_max_bytes": 512 * 1024 * 1024,
    "ready_buffer_max_items": 32,
    "ready_buffer_resume_bytes": 256 * 1024 * 1024,
    "ready_buffer_hysteresis_secs": 5,
    "require_quota_freshness": False,
    "schedule_trace_enabled": True,
    "account_lease_prewarm_enabled": True,
    "release_account_after_sse": True,
    "ss_stage_wall_timeout_secs": 120,
    # Watchdog forced-release deadline for a whole sS/account lease (SSE + poll +
    # resolve + download + return window). Must stay well above the largest poll
    # budget (multi_reference 360s) plus download, otherwise the watchdog would yank
    # slots out from under legitimate long polls.
    "ss_slot_deadline_secs": 900,
    # Max queue wait when acquiring an upload/pS/sS/download slot. Bounds the wait so a
    # leaked slot degrades into a clean error instead of an unbounded Event.wait().
    "pool_acquire_timeout_secs": 300,
    "pre_ticket_ttl_secs": 120,
    "pre_ticket_pool_enabled": True,
}

DEFAULT_IMAGE_REFERENCE_ASSETS = {
    "asset_ttl_seconds": 6 * 3600,
    "upload_global_concurrency": 6,
    "upload_per_user_concurrency": 3,
    "upload_max_bytes_inflight": 96 * 1024 * 1024,
    "upload_retry_after_seconds": 5,
}

DEFAULT_IMAGE_DEADLOCK_GUARD = {
    "enabled": True,
    # Fallback only. With cpu_budget_source="auto" the guard reads the real quota
    # from cgroup v2 /sys/fs/cgroup/cpu.max (v1 and os.cpu_count() next), so this
    # hand-typed number can no longer silently drift from compose `cpus:`.
    # Set cpu_budget_source="config" to pin the budget to this value instead
    # (audit 28 §B8 / fix A4-6).
    "cpu_budget_vcpu": 1.5,
    "cpu_budget_source": "auto",
    "normal_cpu_p95": 70.0,
    "warning_cpu_p95": 80.0,
    "deadlock_cpu_threshold": 90.0,
    "sustain_seconds": 60.0,
    "recover_cpu_threshold": 65.0,
    # Dwell time below deadlock_cpu_threshold that clears an already-tripped guard
    # when CPU never drops to recover_cpu_threshold. Without it, CPU parked in the
    # 65~90% band froze the image queue indefinitely (audit 28 §B8).
    "recover_sustain_seconds": 30.0,
    "sample_interval_sec": 2.0,
}

# Pipeline watchdog (audit 28 §A4 / fixes A3-1, A3-2).
#   enabled                 -- run the background loop at all
#   interval_secs           -- loop period; /health still ticks on demand
#   startup_delay_secs      -- let the app finish booting before the first tick
#   min_tick_interval_secs  -- coalescing window shared by loop and /health,
#                              capped internally at interval_secs / 2
#   force_release_expired   -- drop expired *ledger* leases (near-cosmetic: the
#                              ledger does not own _image_inflight)
#   reconcile_force         -- THE TEETH. Lets reconcile_inflight() rewrite the
#                              live _image_inflight map. Set false to make the
#                              watchdog report-only without a code change.
#   reconcile_confirm_ticks -- consecutive ticks a token must stay over-counted
#                              before any correction is applied
DEFAULT_IMAGE_PIPELINE_WATCHDOG = {
    "enabled": True,
    "interval_secs": 30.0,
    "startup_delay_secs": 15.0,
    "min_tick_interval_secs": 5.0,
    "force_release_expired": True,
    "reconcile_force": True,
    "reconcile_confirm_ticks": 3,
}

DEFAULT_PROXY_RUNTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
    },
}

DEFAULT_THIRD_PARTY_APPS = {
    "infinite_canvas": {
        "enabled": False,
        "url": "https://canvas.best",
    },
}

DEFAULT_ACCOUNT_REFRESH_ALL = {
    "concurrency": 2,
    "max_concurrency": 8,
    "batch_size": 25,
    "delay_between_accounts_sec": 0.2,
    "delay_between_batches_sec": 5.0,
    "stale_after_hours": 0,
    "include_recent": True,
    "min_available_memory_mb": 512,
    "max_load_1m": 1.5,
    "resource_pause_enabled": False,
    "resource_check_interval_sec": 10.0,
    "delete_invalid": True,
    "delete_after_failures": 1,
    "expired_grace_hours": 1,
}

DEFAULT_PANDA_SYNC = {
    "enabled": False,
    "base_url": "",
    "auth_key": "",
    "batch_size": 20,
    "timeout_seconds": 60,
    "remove_local_on_success": True,
    "queue_on_failure": False,
    "cooldown_seconds": 2.0,
    # 本地注册号先进入 staging，按多档三次探活成熟后再上传 Panda。
    "staging_enabled": True,
    "probe_before_upload": True,
    # 兼容旧配置；若未显式配置 probe_schedule_minutes，则由 hours * 60 派生。
    "probe_schedule_hours": [1, 3, 6],
    "probe_schedule_minutes": [30, 120, 360],
    "low_probe_schedule_minutes": [10, 30, 90],
    "emergency_probe_schedule_minutes": [5, 15, 45],
    "probe_batch_limit": 100,
    "low_probe_batch_limit": 150,
    "emergency_probe_batch_limit": 200,
    "probe_concurrency": 4,
    "low_probe_concurrency": 6,
    "emergency_probe_concurrency": 8,
    "probe_cooldown_sec": 120.0,
    "low_probe_cooldown_sec": 60.0,
    "emergency_probe_cooldown_sec": 30.0,
    "probe_transient_backoff_sec": 1800.0,
    # Panda 号池水位：高水位以上不上传，低水位以下按批次补到高水位。
    "watermark_enabled": True,
    "high_watermark": 1500,
    "low_watermark": 500,
    "emergency_watermark": 200,
    "upload_min_batch": 10,
    "upload_max_batch": 20,
    "low_upload_max_batch": 20,
    "emergency_upload_max_batch": 20,
    "sync_interval_minutes": 30,
    "low_sync_interval_sec": 60,
    "emergency_sync_interval_sec": 30,
    "remote_stats_ttl_sec": 60,
    # 远端公网导入入口保护，避免频繁全量写/大批量写拖垮 Panda。
    "public_import_min_interval_sec": 30,
    "public_import_max_batch_size": 20,
}

DEFAULT_ACCOUNT_MAINTENANCE_LOOP = {
    "enabled": False,
    "batch_limit": 80,
    "concurrency": 1,
    "batch_size": 20,
    "delay_between_accounts_sec": 1.5,
    "delay_between_batches_sec": 5.0,
    "cooldown_sec": 10.0,
    "stale_after_hours": 0,
    "include_recent": True,
    "min_available_memory_mb": 512,
    "slow_min_available_memory_mb": 512,
    "max_load_1m": 1.5,
    "resource_pause_enabled": False,
    "resource_check_interval_sec": 10.0,
    "slow_when_image_inflight": 8,
    "pause_when_image_inflight": 0,
    "slow_batch_limit": 20,
    "slow_delay_between_accounts_sec": 3.0,
    "slow_cooldown_sec": 10.0,
    "startup_delay_sec": 5.0,
    "delete_invalid": True,
    "delete_after_failures": 1,
    "expired_grace_hours": 1,
}

DEFAULT_OUTLOOK_AUTO_RECOVERY = {
    "enabled": False,
    "interval_sec": 1800,
    "max_per_cycle": 1,
    "startup_delay_sec": 15.0,
    "progress_poll_sec": 2.0,
}


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _normalize_ratio(value: object, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        normalized = float(default)
    if normalized != normalized:  # NaN
        normalized = float(default)
    return max(float(minimum), min(float(maximum), normalized))


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


def _normalize_backup_include(value: object) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    normalized = dict(DEFAULT_BACKUP_INCLUDE)
    for key in normalized:
        normalized[key] = _normalize_bool(source.get(key), normalized[key])
    return normalized


def _normalize_backup_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), False),
        "provider": "cloudflare_r2",
        "account_id": str(source.get("account_id") or "").strip(),
        "access_key_id": str(source.get("access_key_id") or "").strip(),
        "secret_access_key": str(source.get("secret_access_key") or "").strip(),
        "bucket": str(source.get("bucket") or "").strip(),
        "prefix": str(source.get("prefix") or "backups").strip().strip("/") or "backups",
        "interval_minutes": _normalize_positive_int(source.get("interval_minutes"), 360, 1),
        "rotation_keep": _normalize_positive_int(source.get("rotation_keep"), 10, 0),
        "encrypt": _normalize_bool(source.get("encrypt"), False),
        "passphrase": str(source.get("passphrase") or "").strip(),
        "include": _normalize_backup_include(source.get("include")),
    }


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "last_started_at": str(source.get("last_started_at") or "").strip() or None,
        "last_finished_at": str(source.get("last_finished_at") or "").strip() or None,
        "last_status": str(source.get("last_status") or "idle").strip() or "idle",
        "last_error": str(source.get("last_error") or "").strip() or None,
        "last_object_key": str(source.get("last_object_key") or "").strip() or None,
    }


def _normalize_image_storage_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    mode = str(source.get("mode") or "local").strip().lower()
    if mode not in {"local", "webdav", "both"}:
        mode = "local"
    enabled = _normalize_bool(source.get("enabled"), False)
    if not enabled:
        mode = "local"
    root_path = str(source.get("webdav_root_path") or DEFAULT_IMAGE_STORAGE["webdav_root_path"]).strip().strip("/")
    return {
        "enabled": enabled,
        "mode": mode,
        "webdav_url": str(source.get("webdav_url") or "").strip().rstrip("/"),
        "webdav_username": str(source.get("webdav_username") or "").strip(),
        "webdav_password": str(source.get("webdav_password") or "").strip(),
        "webdav_root_path": root_path or str(DEFAULT_IMAGE_STORAGE["webdav_root_path"]),
        "public_base_url": str(source.get("public_base_url") or "").strip().rstrip("/"),
    }


def _normalize_chat_completion_cache_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), DEFAULT_CHAT_COMPLETION_CACHE["enabled"]),
        "ttl_seconds": _normalize_positive_int(
            source.get("ttl_seconds"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["ttl_seconds"]),
            0,
        ),
        "max_entries": _normalize_positive_int(
            source.get("max_entries"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["max_entries"]),
            1,
        ),
        "dedupe_inflight": _normalize_bool(
            source.get("dedupe_inflight"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["dedupe_inflight"]),
        ),
        "stream_cache": _normalize_bool(
            source.get("stream_cache"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["stream_cache"]),
        ),
        "normalize_messages": _normalize_bool(
            source.get("normalize_messages"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["normalize_messages"]),
        ),
        "drop_adjacent_duplicates": _normalize_bool(
            source.get("drop_adjacent_duplicates"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_adjacent_duplicates"]),
        ),
        "drop_assistant_history": _normalize_bool(
            source.get("drop_assistant_history"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_assistant_history"]),
        ),
    }


def _normalize_image_task_queue_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    submit_max = _normalize_positive_int(source.get("submit_workers_max"), int(DEFAULT_IMAGE_TASK_QUEUE["submit_workers_max"]), 1)
    submit_workers = _normalize_positive_int(source.get("submit_workers"), int(DEFAULT_IMAGE_TASK_QUEUE["submit_workers"]), 1)
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_IMAGE_TASK_QUEUE["enabled"])),
        "submit_workers": min(submit_workers, submit_max),
        "submit_workers_max": submit_max,
        "poll_workers": _normalize_positive_int(source.get("poll_workers"), int(DEFAULT_IMAGE_TASK_QUEUE["poll_workers"]), 0),
        "download_workers": _normalize_positive_int(source.get("download_workers"), int(DEFAULT_IMAGE_TASK_QUEUE["download_workers"]), 1),
        "global_queue_max": _normalize_positive_int(source.get("global_queue_max"), int(DEFAULT_IMAGE_TASK_QUEUE["global_queue_max"]), 1),
        "per_user_running_max": _normalize_positive_int(source.get("per_user_running_max"), int(DEFAULT_IMAGE_TASK_QUEUE["per_user_running_max"]), 1),
        "per_user_running_base": _normalize_positive_int(source.get("per_user_running_base"), int(DEFAULT_IMAGE_TASK_QUEUE["per_user_running_base"]), 1),
        "per_user_running_burst": _normalize_positive_int(source.get("per_user_running_burst"), int(DEFAULT_IMAGE_TASK_QUEUE["per_user_running_burst"]), 1),
        "burst_enabled": _normalize_bool(source.get("burst_enabled"), bool(DEFAULT_IMAGE_TASK_QUEUE["burst_enabled"])),
        "burst_min_queued": _normalize_positive_int(source.get("burst_min_queued"), int(DEFAULT_IMAGE_TASK_QUEUE["burst_min_queued"]), 1),
        "burst_min_dispatchable_candidates": _normalize_positive_int(
            source.get("burst_min_dispatchable_candidates"),
            int(DEFAULT_IMAGE_TASK_QUEUE["burst_min_dispatchable_candidates"]),
            1,
        ),
        "burst_max_preflight_backoff": _normalize_positive_int(
            source.get("burst_max_preflight_backoff"),
            int(DEFAULT_IMAGE_TASK_QUEUE["burst_max_preflight_backoff"]),
            0,
        ),
        "per_user_queue_max": _normalize_positive_int(source.get("per_user_queue_max"), int(DEFAULT_IMAGE_TASK_QUEUE["per_user_queue_max"]), 1),
        "submit_start_min_interval_ms": _normalize_positive_int(
            source.get("submit_start_min_interval_ms"),
            int(DEFAULT_IMAGE_TASK_QUEUE["submit_start_min_interval_ms"]),
            0,
        ),
        "submit_interval_adaptive": _normalize_bool(
            source.get("submit_interval_adaptive"),
            bool(DEFAULT_IMAGE_TASK_QUEUE["submit_interval_adaptive"]),
        ),
        "timeout_pending_poll_secs": _normalize_positive_int(source.get("timeout_pending_poll_secs"), int(DEFAULT_IMAGE_TASK_QUEUE["timeout_pending_poll_secs"]), 5),
        "timeout_pending_max_attempts": _normalize_positive_int(source.get("timeout_pending_max_attempts"), int(DEFAULT_IMAGE_TASK_QUEUE["timeout_pending_max_attempts"]), 1),
        "sync_ladder_reserve_ratio": _normalize_ratio(
            source.get("sync_ladder_reserve_ratio"),
            float(DEFAULT_IMAGE_TASK_QUEUE["sync_ladder_reserve_ratio"]),
            minimum=0.0,
            maximum=0.5,
        ),
        "generation_poll_timeout_secs": _normalize_positive_int(source.get("generation_poll_timeout_secs"), int(DEFAULT_IMAGE_TASK_QUEUE["generation_poll_timeout_secs"]), 30),
        "edit_poll_timeout_secs": _normalize_positive_int(source.get("edit_poll_timeout_secs"), int(DEFAULT_IMAGE_TASK_QUEUE["edit_poll_timeout_secs"]), 30),
        "multi_reference_poll_timeout_secs": _normalize_positive_int(source.get("multi_reference_poll_timeout_secs"), int(DEFAULT_IMAGE_TASK_QUEUE["multi_reference_poll_timeout_secs"]), 30),
        "pre_conversation_timeout_secs": _normalize_positive_int(source.get("pre_conversation_timeout_secs"), int(DEFAULT_IMAGE_TASK_QUEUE["pre_conversation_timeout_secs"]), 30),
        "pre_conversation_max_attempts": _normalize_positive_int(source.get("pre_conversation_max_attempts"), int(DEFAULT_IMAGE_TASK_QUEUE["pre_conversation_max_attempts"]), 1),
        "pre_conversation_retry_backoff_secs": _normalize_positive_int(source.get("pre_conversation_retry_backoff_secs"), int(DEFAULT_IMAGE_TASK_QUEUE["pre_conversation_retry_backoff_secs"]), 0),
    }


def _normalize_image_pipeline_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_IMAGE_PIPELINE["enabled"])),
        "prompt_slots": _normalize_positive_int(source.get("prompt_slots"), int(DEFAULT_IMAGE_PIPELINE["prompt_slots"]), 1),
        "sse_slots": _normalize_positive_int(source.get("sse_slots"), int(DEFAULT_IMAGE_PIPELINE["sse_slots"]), 1),
        "download_concurrency": _normalize_positive_int(
            source.get("download_concurrency"),
            int(DEFAULT_IMAGE_PIPELINE["download_concurrency"]),
            1,
        ),
        "asset_upload_concurrency": _normalize_positive_int(
            source.get("asset_upload_concurrency"),
            int(DEFAULT_IMAGE_PIPELINE["asset_upload_concurrency"]),
            1,
        ),
        "global_queue_max": _normalize_positive_int(
            source.get("global_queue_max"),
            int(DEFAULT_IMAGE_PIPELINE["global_queue_max"]),
            1,
        ),
        "relaxed_per_user_running": _normalize_bool(
            source.get("relaxed_per_user_running"),
            bool(DEFAULT_IMAGE_PIPELINE["relaxed_per_user_running"]),
        ),
        "aci_epsilon": float(source.get("aci_epsilon") or DEFAULT_IMAGE_PIPELINE["aci_epsilon"]),
        "ready_buffer_max_bytes": _normalize_positive_int(
            source.get("ready_buffer_max_bytes"),
            int(DEFAULT_IMAGE_PIPELINE["ready_buffer_max_bytes"]),
            1,
        ),
        "ready_buffer_max_items": _normalize_positive_int(
            source.get("ready_buffer_max_items"),
            int(DEFAULT_IMAGE_PIPELINE["ready_buffer_max_items"]),
            1,
        ),
        "ready_buffer_resume_bytes": _normalize_positive_int(
            source.get("ready_buffer_resume_bytes"),
            int(DEFAULT_IMAGE_PIPELINE["ready_buffer_resume_bytes"]),
            1,
        ),
        "ready_buffer_hysteresis_secs": _normalize_positive_int(
            source.get("ready_buffer_hysteresis_secs"),
            int(DEFAULT_IMAGE_PIPELINE["ready_buffer_hysteresis_secs"]),
            0,
        ),
        "require_quota_freshness": _normalize_bool(
            source.get("require_quota_freshness"),
            bool(DEFAULT_IMAGE_PIPELINE["require_quota_freshness"]),
        ),
        "schedule_trace_enabled": _normalize_bool(
            source.get("schedule_trace_enabled"),
            bool(DEFAULT_IMAGE_PIPELINE["schedule_trace_enabled"]),
        ),
        "account_lease_prewarm_enabled": _normalize_bool(
            source.get("account_lease_prewarm_enabled"),
            bool(DEFAULT_IMAGE_PIPELINE["account_lease_prewarm_enabled"]),
        ),
        "release_account_after_sse": _normalize_bool(
            source.get("release_account_after_sse"),
            bool(DEFAULT_IMAGE_PIPELINE["release_account_after_sse"]),
        ),
        "ss_stage_wall_timeout_secs": _normalize_positive_int(
            source.get("ss_stage_wall_timeout_secs"),
            int(DEFAULT_IMAGE_PIPELINE["ss_stage_wall_timeout_secs"]),
            5,
        ),
        "ss_slot_deadline_secs": _normalize_positive_int(
            source.get("ss_slot_deadline_secs"),
            int(DEFAULT_IMAGE_PIPELINE["ss_slot_deadline_secs"]),
            30,
        ),
        "pool_acquire_timeout_secs": _normalize_positive_int(
            source.get("pool_acquire_timeout_secs"),
            int(DEFAULT_IMAGE_PIPELINE["pool_acquire_timeout_secs"]),
            5,
        ),
        "pre_ticket_ttl_secs": _normalize_positive_int(
            source.get("pre_ticket_ttl_secs"),
            int(DEFAULT_IMAGE_PIPELINE["pre_ticket_ttl_secs"]),
            30,
        ),
        "pre_ticket_pool_enabled": _normalize_bool(
            source.get("pre_ticket_pool_enabled"),
            bool(DEFAULT_IMAGE_PIPELINE["pre_ticket_pool_enabled"]),
        ),
    }


def _normalize_image_reference_assets_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "asset_ttl_seconds": _normalize_positive_int(
            source.get("asset_ttl_seconds"),
            int(DEFAULT_IMAGE_REFERENCE_ASSETS["asset_ttl_seconds"]),
            60,
        ),
        "upload_global_concurrency": _normalize_positive_int(
            source.get("upload_global_concurrency"),
            int(DEFAULT_IMAGE_REFERENCE_ASSETS["upload_global_concurrency"]),
            1,
        ),
        "upload_per_user_concurrency": _normalize_positive_int(
            source.get("upload_per_user_concurrency"),
            int(DEFAULT_IMAGE_REFERENCE_ASSETS["upload_per_user_concurrency"]),
            1,
        ),
        "upload_max_bytes_inflight": _normalize_positive_int(
            source.get("upload_max_bytes_inflight"),
            int(DEFAULT_IMAGE_REFERENCE_ASSETS["upload_max_bytes_inflight"]),
            1024 * 1024,
        ),
        "upload_retry_after_seconds": _normalize_positive_int(
            source.get("upload_retry_after_seconds"),
            int(DEFAULT_IMAGE_REFERENCE_ASSETS["upload_retry_after_seconds"]),
            1,
        ),
    }


def _normalize_image_deadlock_guard_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    def _float_value(key: str, default: float, minimum: float = 0.0) -> float:
        try:
            return max(minimum, float(source.get(key, default) or default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_IMAGE_DEADLOCK_GUARD["enabled"])),
        "cpu_budget_vcpu": _float_value("cpu_budget_vcpu", float(DEFAULT_IMAGE_DEADLOCK_GUARD["cpu_budget_vcpu"]), 0.1),
        "cpu_budget_source": (
            "config"
            if str(source.get("cpu_budget_source") or "").strip().lower() == "config"
            else str(DEFAULT_IMAGE_DEADLOCK_GUARD["cpu_budget_source"])
        ),
        "normal_cpu_p95": _float_value("normal_cpu_p95", float(DEFAULT_IMAGE_DEADLOCK_GUARD["normal_cpu_p95"]), 1.0),
        "warning_cpu_p95": _float_value("warning_cpu_p95", float(DEFAULT_IMAGE_DEADLOCK_GUARD["warning_cpu_p95"]), 1.0),
        "deadlock_cpu_threshold": _float_value("deadlock_cpu_threshold", float(DEFAULT_IMAGE_DEADLOCK_GUARD["deadlock_cpu_threshold"]), 1.0),
        "sustain_seconds": _float_value("sustain_seconds", float(DEFAULT_IMAGE_DEADLOCK_GUARD["sustain_seconds"]), 1.0),
        "recover_cpu_threshold": _float_value("recover_cpu_threshold", float(DEFAULT_IMAGE_DEADLOCK_GUARD["recover_cpu_threshold"]), 1.0),
        "recover_sustain_seconds": _float_value("recover_sustain_seconds", float(DEFAULT_IMAGE_DEADLOCK_GUARD["recover_sustain_seconds"]), 0.0),
        "sample_interval_sec": _float_value("sample_interval_sec", float(DEFAULT_IMAGE_DEADLOCK_GUARD["sample_interval_sec"]), 0.5),
    }


def _normalize_image_pipeline_watchdog_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}

    def _float_value(key: str, minimum: float) -> float:
        default = float(DEFAULT_IMAGE_PIPELINE_WATCHDOG[key])
        try:
            return max(minimum, float(source.get(key, default) if source.get(key) is not None else default))
        except (TypeError, ValueError):
            return default

    def _bool_value(key: str) -> bool:
        return _normalize_bool(source.get(key), bool(DEFAULT_IMAGE_PIPELINE_WATCHDOG[key]))

    try:
        confirm_ticks = int(source.get("reconcile_confirm_ticks") or DEFAULT_IMAGE_PIPELINE_WATCHDOG["reconcile_confirm_ticks"])
    except (TypeError, ValueError):
        confirm_ticks = int(DEFAULT_IMAGE_PIPELINE_WATCHDOG["reconcile_confirm_ticks"])

    return {
        "enabled": _bool_value("enabled"),
        "interval_secs": _float_value("interval_secs", 1.0),
        "startup_delay_secs": _float_value("startup_delay_secs", 0.0),
        "min_tick_interval_secs": _float_value("min_tick_interval_secs", 0.0),
        "force_release_expired": _bool_value("force_release_expired"),
        "reconcile_force": _bool_value("reconcile_force"),
        # At least 2 samples: a single-tick over-count is indistinguishable from
        # a task that is between acquiring its slot and being marked RUNNING.
        "reconcile_confirm_ticks": max(2, min(100, confirm_ticks)),
    }


def _normalize_status_codes(value: object) -> list[int]:
    items = value if isinstance(value, list) else DEFAULT_PROXY_RUNTIME["reset_session_status_codes"]
    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        try:
            status = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= status <= 599 and status not in normalized:
            normalized.append(status)
    if not normalized:
        return list(DEFAULT_PROXY_RUNTIME["reset_session_status_codes"])
    return normalized


def _normalize_proxy_runtime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    default_clearance = DEFAULT_PROXY_RUNTIME["clearance"]
    clearance_source = source.get("clearance") if isinstance(source.get("clearance"), dict) else {}

    egress_mode = str(source.get("egress_mode") or DEFAULT_PROXY_RUNTIME["egress_mode"]).strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = str(DEFAULT_PROXY_RUNTIME["egress_mode"])

    clearance_mode = str(clearance_source.get("mode") or default_clearance["mode"]).strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = str(default_clearance["mode"])

    user_agent = str(clearance_source.get("user_agent") or default_clearance["user_agent"]).strip()
    browser = str(clearance_source.get("browser") or default_clearance["browser"]).strip()

    existing_clearance_cookies = str(source.get("_existing_cf_cookies") or "").strip()
    existing_cf_clearance = str(source.get("_existing_cf_clearance") or "").strip()
    cf_cookies = str(clearance_source.get("cf_cookies") or "").strip()
    cf_clearance = str(clearance_source.get("cf_clearance") or "").strip()
    if not cf_cookies and _normalize_bool(clearance_source.get("has_cf_cookies"), False):
        cf_cookies = existing_clearance_cookies
    if not cf_clearance and _normalize_bool(clearance_source.get("has_cf_clearance"), False):
        cf_clearance = existing_cf_clearance

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROXY_RUNTIME["enabled"])),
        "egress_mode": egress_mode,
        "proxy_url": str(source.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(source.get("resource_proxy_url") or "").strip(),
        "skip_ssl_verify": _normalize_bool(
            source.get("skip_ssl_verify"),
            bool(DEFAULT_PROXY_RUNTIME["skip_ssl_verify"]),
        ),
        "reset_session_status_codes": _normalize_status_codes(source.get("reset_session_status_codes")),
        "clearance": {
            "enabled": _normalize_bool(clearance_source.get("enabled"), bool(default_clearance["enabled"])),
            "mode": clearance_mode,
            "cf_cookies": cf_cookies,
            "cf_clearance": cf_clearance,
            "user_agent": user_agent or str(default_clearance["user_agent"]),
            "browser": browser or str(default_clearance["browser"]),
            "flaresolverr_url": str(clearance_source.get("flaresolverr_url") or "").strip(),
            "timeout_sec": _normalize_positive_int(
                clearance_source.get("timeout_sec"),
                int(default_clearance["timeout_sec"]),
                1,
            ),
            "refresh_interval": _normalize_positive_int(
                clearance_source.get("refresh_interval"),
                int(default_clearance["refresh_interval"]),
                60,
            ),
            "warm_up_on_start": _normalize_bool(
                clearance_source.get("warm_up_on_start"),
                bool(default_clearance["warm_up_on_start"]),
            ),
        },
    }


def _normalize_third_party_apps_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    canvas_source = source.get("infinite_canvas") if isinstance(source.get("infinite_canvas"), dict) else {}
    return {
        "infinite_canvas": {
            "enabled": _normalize_bool(canvas_source.get("enabled"), False),
            "url": str(canvas_source.get("url") or DEFAULT_THIRD_PARTY_APPS["infinite_canvas"]["url"]).strip(),
        },
    }


def _normalize_account_refresh_all_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    concurrency = _normalize_positive_int(source.get("concurrency"), int(DEFAULT_ACCOUNT_REFRESH_ALL["concurrency"]), 1)
    max_concurrency = _normalize_positive_int(source.get("max_concurrency"), int(DEFAULT_ACCOUNT_REFRESH_ALL["max_concurrency"]), 1)
    return {
        "concurrency": min(concurrency, max_concurrency),
        "max_concurrency": max_concurrency,
        "batch_size": _normalize_positive_int(source.get("batch_size"), int(DEFAULT_ACCOUNT_REFRESH_ALL["batch_size"]), 1),
        "delay_between_accounts_sec": max(0.0, float(source.get("delay_between_accounts_sec", DEFAULT_ACCOUNT_REFRESH_ALL["delay_between_accounts_sec"]) or 0.0)),
        "delay_between_batches_sec": max(0.0, float(source.get("delay_between_batches_sec", DEFAULT_ACCOUNT_REFRESH_ALL["delay_between_batches_sec"]) or 0.0)),
        "stale_after_hours": _normalize_positive_int(source.get("stale_after_hours"), int(DEFAULT_ACCOUNT_REFRESH_ALL["stale_after_hours"]), 0),
        "include_recent": _normalize_bool(source.get("include_recent"), bool(DEFAULT_ACCOUNT_REFRESH_ALL["include_recent"])),
        "min_available_memory_mb": _normalize_positive_int(source.get("min_available_memory_mb"), int(DEFAULT_ACCOUNT_REFRESH_ALL["min_available_memory_mb"]), 0),
        "max_load_1m": max(0.0, float(source.get("max_load_1m", DEFAULT_ACCOUNT_REFRESH_ALL["max_load_1m"]) or 0.0)),
        "resource_pause_enabled": _normalize_bool(source.get("resource_pause_enabled"), bool(DEFAULT_ACCOUNT_REFRESH_ALL["resource_pause_enabled"])),
        "resource_check_interval_sec": max(1.0, float(source.get("resource_check_interval_sec", DEFAULT_ACCOUNT_REFRESH_ALL["resource_check_interval_sec"]) or 1.0)),
        "delete_invalid": _normalize_bool(source.get("delete_invalid"), bool(DEFAULT_ACCOUNT_REFRESH_ALL["delete_invalid"])),
        "delete_after_failures": _normalize_positive_int(source.get("delete_after_failures"), int(DEFAULT_ACCOUNT_REFRESH_ALL["delete_after_failures"]), 0),
        "expired_grace_hours": _normalize_positive_int(source.get("expired_grace_hours"), int(DEFAULT_ACCOUNT_REFRESH_ALL["expired_grace_hours"]), 0),
    }



def _normalize_positive_int_list(value: object, default: list[int], *, min_value: int = 1) -> list[int]:
    raw = value if isinstance(value, list) else default
    values: list[int] = []
    for item in raw:
        parsed = _normalize_positive_int(item, 0, 0)
        if parsed >= min_value and parsed not in values:
            values.append(parsed)
    return sorted(values) or list(default)

def _normalize_panda_sync_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    schedule_hours = _normalize_positive_int_list(
        source.get("probe_schedule_hours"),
        list(DEFAULT_PANDA_SYNC["probe_schedule_hours"]),
    )
    if "probe_schedule_minutes" in source:
        schedule_minutes = _normalize_positive_int_list(
            source.get("probe_schedule_minutes"),
            list(DEFAULT_PANDA_SYNC["probe_schedule_minutes"]),
        )
    elif "probe_schedule_hours" in source:
        schedule_minutes = [max(1, int(hour) * 60) for hour in schedule_hours]
    else:
        schedule_minutes = list(DEFAULT_PANDA_SYNC["probe_schedule_minutes"])
    low_schedule_minutes = _normalize_positive_int_list(
        source.get("low_probe_schedule_minutes"),
        list(DEFAULT_PANDA_SYNC["low_probe_schedule_minutes"]),
    )
    emergency_schedule_minutes = _normalize_positive_int_list(
        source.get("emergency_probe_schedule_minutes"),
        list(DEFAULT_PANDA_SYNC["emergency_probe_schedule_minutes"]),
    )
    high_watermark = _normalize_positive_int(source.get("high_watermark"), int(DEFAULT_PANDA_SYNC["high_watermark"]), 1)
    low_watermark = _normalize_positive_int(source.get("low_watermark"), int(DEFAULT_PANDA_SYNC["low_watermark"]), 0)
    if low_watermark >= high_watermark:
        low_watermark = max(0, high_watermark // 3)
    emergency_watermark = _normalize_positive_int(source.get("emergency_watermark"), int(DEFAULT_PANDA_SYNC["emergency_watermark"]), 0)
    emergency_watermark = min(emergency_watermark, low_watermark)
    upload_max_batch = _normalize_positive_int(source.get("upload_max_batch"), int(DEFAULT_PANDA_SYNC["upload_max_batch"]), 1)
    upload_min_batch = _normalize_positive_int(source.get("upload_min_batch"), int(DEFAULT_PANDA_SYNC["upload_min_batch"]), 1)
    upload_min_batch = min(upload_min_batch, upload_max_batch)
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PANDA_SYNC["enabled"])),
        "base_url": str(source.get("base_url") or "").strip().rstrip("/"),
        "auth_key": str(source.get("auth_key") or "").strip(),
        "batch_size": _normalize_positive_int(source.get("batch_size"), int(DEFAULT_PANDA_SYNC["batch_size"]), 1),
        "timeout_seconds": _normalize_positive_int(source.get("timeout_seconds"), int(DEFAULT_PANDA_SYNC["timeout_seconds"]), 5),
        "remove_local_on_success": _normalize_bool(source.get("remove_local_on_success"), bool(DEFAULT_PANDA_SYNC["remove_local_on_success"])),
        "queue_on_failure": _normalize_bool(source.get("queue_on_failure"), bool(DEFAULT_PANDA_SYNC["queue_on_failure"])),
        "cooldown_seconds": max(0.0, float(source.get("cooldown_seconds", DEFAULT_PANDA_SYNC["cooldown_seconds"]) or 0.0)),
        "staging_enabled": _normalize_bool(source.get("staging_enabled"), bool(DEFAULT_PANDA_SYNC["staging_enabled"])),
        "probe_before_upload": _normalize_bool(source.get("probe_before_upload"), bool(DEFAULT_PANDA_SYNC["probe_before_upload"])),
        "probe_schedule_hours": schedule_hours,
        "probe_schedule_minutes": schedule_minutes,
        "low_probe_schedule_minutes": low_schedule_minutes,
        "emergency_probe_schedule_minutes": emergency_schedule_minutes,
        "probe_batch_limit": _normalize_positive_int(source.get("probe_batch_limit"), int(DEFAULT_PANDA_SYNC["probe_batch_limit"]), 1),
        "low_probe_batch_limit": _normalize_positive_int(source.get("low_probe_batch_limit"), int(DEFAULT_PANDA_SYNC["low_probe_batch_limit"]), 1),
        "emergency_probe_batch_limit": _normalize_positive_int(source.get("emergency_probe_batch_limit"), int(DEFAULT_PANDA_SYNC["emergency_probe_batch_limit"]), 1),
        "probe_concurrency": min(_normalize_positive_int(source.get("probe_concurrency"), int(DEFAULT_PANDA_SYNC["probe_concurrency"]), 1), 8),
        "low_probe_concurrency": min(_normalize_positive_int(source.get("low_probe_concurrency"), int(DEFAULT_PANDA_SYNC["low_probe_concurrency"]), 1), 8),
        "emergency_probe_concurrency": min(_normalize_positive_int(source.get("emergency_probe_concurrency"), int(DEFAULT_PANDA_SYNC["emergency_probe_concurrency"]), 1), 8),
        "probe_cooldown_sec": max(10.0, float(source.get("probe_cooldown_sec", DEFAULT_PANDA_SYNC["probe_cooldown_sec"]) or 10.0)),
        "low_probe_cooldown_sec": max(10.0, float(source.get("low_probe_cooldown_sec", DEFAULT_PANDA_SYNC["low_probe_cooldown_sec"]) or 10.0)),
        "emergency_probe_cooldown_sec": max(10.0, float(source.get("emergency_probe_cooldown_sec", DEFAULT_PANDA_SYNC["emergency_probe_cooldown_sec"]) or 10.0)),
        "probe_transient_backoff_sec": max(60.0, float(source.get("probe_transient_backoff_sec", DEFAULT_PANDA_SYNC["probe_transient_backoff_sec"]) or 60.0)),
        "watermark_enabled": _normalize_bool(source.get("watermark_enabled"), bool(DEFAULT_PANDA_SYNC["watermark_enabled"])),
        "high_watermark": high_watermark,
        "low_watermark": low_watermark,
        "emergency_watermark": emergency_watermark,
        "upload_min_batch": upload_min_batch,
        "upload_max_batch": upload_max_batch,
        "low_upload_max_batch": _normalize_positive_int(source.get("low_upload_max_batch"), int(DEFAULT_PANDA_SYNC["low_upload_max_batch"]), 1),
        "emergency_upload_max_batch": _normalize_positive_int(source.get("emergency_upload_max_batch"), int(DEFAULT_PANDA_SYNC["emergency_upload_max_batch"]), 1),
        "sync_interval_minutes": _normalize_positive_int(source.get("sync_interval_minutes"), int(DEFAULT_PANDA_SYNC["sync_interval_minutes"]), 1),
        "low_sync_interval_sec": _normalize_positive_int(source.get("low_sync_interval_sec"), int(DEFAULT_PANDA_SYNC["low_sync_interval_sec"]), 1),
        "emergency_sync_interval_sec": _normalize_positive_int(source.get("emergency_sync_interval_sec"), int(DEFAULT_PANDA_SYNC["emergency_sync_interval_sec"]), 1),
        "remote_stats_ttl_sec": _normalize_positive_int(source.get("remote_stats_ttl_sec"), int(DEFAULT_PANDA_SYNC["remote_stats_ttl_sec"]), 0),
        "public_import_min_interval_sec": _normalize_positive_int(source.get("public_import_min_interval_sec"), int(DEFAULT_PANDA_SYNC["public_import_min_interval_sec"]), 0),
        "public_import_max_batch_size": _normalize_positive_int(source.get("public_import_max_batch_size"), int(DEFAULT_PANDA_SYNC["public_import_max_batch_size"]), 1),
    }


def _normalize_account_maintenance_loop_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["enabled"])),
        "batch_limit": _normalize_positive_int(source.get("batch_limit"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["batch_limit"]), 1),
        "concurrency": min(
            _normalize_positive_int(source.get("concurrency"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["concurrency"]), 1),
            8,
        ),
        "batch_size": _normalize_positive_int(source.get("batch_size"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["batch_size"]), 1),
        "delay_between_accounts_sec": max(0.0, float(source.get("delay_between_accounts_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["delay_between_accounts_sec"]) or 0.0)),
        "delay_between_batches_sec": max(0.0, float(source.get("delay_between_batches_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["delay_between_batches_sec"]) or 0.0)),
        "cooldown_sec": max(1.0, float(source.get("cooldown_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["cooldown_sec"]) or 1.0)),
        "stale_after_hours": _normalize_positive_int(source.get("stale_after_hours"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["stale_after_hours"]), 0),
        "include_recent": _normalize_bool(source.get("include_recent"), bool(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["include_recent"])),
        "min_available_memory_mb": _normalize_positive_int(source.get("min_available_memory_mb"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["min_available_memory_mb"]), 0),
        "slow_min_available_memory_mb": _normalize_positive_int(source.get("slow_min_available_memory_mb"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["slow_min_available_memory_mb"]), 0),
        "max_load_1m": max(0.0, float(source.get("max_load_1m", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["max_load_1m"]) or 0.0)),
        "resource_pause_enabled": _normalize_bool(source.get("resource_pause_enabled"), bool(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["resource_pause_enabled"])),
        "resource_check_interval_sec": max(1.0, float(source.get("resource_check_interval_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["resource_check_interval_sec"]) or 1.0)),
        "slow_when_image_inflight": _normalize_positive_int(source.get("slow_when_image_inflight"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["slow_when_image_inflight"]), 0),
        "pause_when_image_inflight": _normalize_positive_int(source.get("pause_when_image_inflight"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["pause_when_image_inflight"]), 0),
        "slow_batch_limit": _normalize_positive_int(source.get("slow_batch_limit"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["slow_batch_limit"]), 1),
        "slow_delay_between_accounts_sec": max(0.0, float(source.get("slow_delay_between_accounts_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["slow_delay_between_accounts_sec"]) or 0.0)),
        "slow_cooldown_sec": max(1.0, float(source.get("slow_cooldown_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["slow_cooldown_sec"]) or 1.0)),
        "startup_delay_sec": max(0.0, float(source.get("startup_delay_sec", DEFAULT_ACCOUNT_MAINTENANCE_LOOP["startup_delay_sec"]) or 0.0)),
        "delete_invalid": _normalize_bool(source.get("delete_invalid"), bool(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["delete_invalid"])),
        "delete_after_failures": _normalize_positive_int(source.get("delete_after_failures"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["delete_after_failures"]), 0),
        "expired_grace_hours": _normalize_positive_int(source.get("expired_grace_hours"), int(DEFAULT_ACCOUNT_MAINTENANCE_LOOP["expired_grace_hours"]), 0),
    }


def _normalize_outlook_auto_recovery_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_OUTLOOK_AUTO_RECOVERY["enabled"])),
        "interval_sec": max(
            60,
            _normalize_positive_int(
                source.get("interval_sec"),
                int(DEFAULT_OUTLOOK_AUTO_RECOVERY["interval_sec"]),
                60,
            ),
        ),
        "max_per_cycle": min(
            5,
            _normalize_positive_int(
                source.get("max_per_cycle"),
                int(DEFAULT_OUTLOOK_AUTO_RECOVERY["max_per_cycle"]),
                1,
            ),
        ),
        "startup_delay_sec": max(
            0.0,
            float(source.get("startup_delay_sec", DEFAULT_OUTLOOK_AUTO_RECOVERY["startup_delay_sec"]) or 0.0),
        ),
        "progress_poll_sec": max(
            0.5,
            float(source.get("progress_poll_sec", DEFAULT_OUTLOOK_AUTO_RECOVERY["progress_poll_sec"]) or 0.5),
        ),
    }


DEFAULT_WORKLOAD_SETTINGS: dict[str, object] = {
    "mode": "shadow",
    "text_queue_mode": "off",
    "canary_token_hashes": [],
    "global_text_inflight": 1,
    "auto_live_min_ready": 0,
}

DEFAULT_SCHEDULER_SETTINGS: dict[str, object] = {
    "enabled": False,
    "image_min_interval_sec": 60,
    "text_min_interval_sec": 30,
    "text_poisson_lambda_sec": 5,
    "jitter_lo": 0.65,
    "jitter_hi": 1.45,
    "extra_poisson_lambda_sec": 8,
    "daily_usage_ratio": 0.70,
    "cooldown_429_sec": 900,
    "night_soft_weight": 0.4,
    "lunch_soft_weight": 0.85,
    "auto_scale_global_concurrency": True,
    "fail_streak_threshold": 3,
    "fail_cooldown_min_sec": 1800,
    "fail_cooldown_max_sec": 5400,
    "new_account_usage_cap": 0.40,
    "cohort_terminal_threshold": 2,
    "cohort_pause_hours": 24,
    "submit_interval_jitter_lo": 0.70,
    "submit_interval_jitter_hi": 1.30,
    "resume_first_delay_sec": 5,
    "resume_backoff_base_sec": 5,
    "resume_backoff_cap_sec": 60,
    "resume_wall_sec": 240,
    "prompt_dedup_window_sec": 120,
    "prompt_dedup_max_parallel": 4,
    "unrestricted": True,
}

DEFAULT_TEXT_NURTURE: dict[str, object] = {
    "enabled": False,
    "worker_enabled": True,
    "poll_interval_sec": 3.0,
    "max_per_hour": 70,
    "max_per_account_per_day": 8,
    "daily_reset_tz": "Asia/Singapore",
    "turns_per_session": 3,
    "turn_gap_sec": 8.0,
    "require_persist_history": True,
    "auto_enqueue": True,
    "auto_enqueue_every_sec": 120.0,
    "auto_enqueue_rotate_accounts": True,
    "count_manual_toward_daily_limit": True,
    "prompts": [],
    "session_follow_up_prompts": [
        "Can you add one more practical detail?",
        "Give a shorter summary in one sentence.",
        "What would you do differently next time?",
    ],
    "model": "auto",
}

DEFAULT_PROACTIVE_REFRESH_SETTINGS: dict[str, object] = {
    "enabled": False,
    "timezone": "Asia/Singapore",
    "timezone_from_egress": True,
    "p_work": 1.0,
    "p_rest": 0.35,
    "window_work": ["09:00", "17:00"],
    "window_rest": ["10:00", "16:00"],
    "workdays": [1, 2, 3, 4, 5],
    "per_account_per_day": 1,
    "minute_cap_k": 2,
    "minute_cap_k_rest": 1,
    "slot_jitter_minutes": 10,
    "tick_sec": 60,
    "startup_delay_sec": 30,
}

DEFAULT_QUOTA_REFRESH_SCHEDULE: dict[str, object] = {
    "enabled": True,
    "phases_per_day": 4,
    "default_timezone": "Asia/Singapore",
    "timezone_from_egress": True,
    "account_jitter_min_minutes": 30,
    "account_jitter_max_minutes": 60,
    "binding_min_gap_hours": 2,
    "tick_sec": 60,
    "pre_restore_refresh_minutes": 45,
}

DEFAULT_QUOTA_WINDOW_PRIME: dict[str, object] = {
    "enabled": True,
    "full_quota": 25,
    "min_account_age_days": 7,
    "image_size": "256x256",
    "image_quality": "low",
    "auto_phase_index": 0,
    "max_concurrent_global": 2,
    "max_concurrent_per_binding": 1,
    "max_auto_attempts": 3,
    "retry_interval_hours": 24,
    "skip_panda_sync_states": ["staging", "ready"],
    "defer_if_refresh_due_within_minutes": 15,
    "tick_sec": 30,
}

DEFAULT_WEBSHARE_CF_SCAN_SETTINGS: dict[str, object] = {
    "enabled": False,
    "pool_path": "",
    "interval_min_sec": 3600,
    "interval_max_sec": 14400,
    "batch_size": 20,
    "workers": 4,
    "auto_quarantine": True,
    "skip_quarantined": True,
    "active_window": ["08:00", "23:00"],
    "timezone": "Asia/Singapore",
    "startup_delay_sec": 120,
    "probe_timeout_sec": 45.0,
    # Only CF-passing Webshare nodes may carry image traffic / be assigned.
    "require_cf_ok_for_image": True,
    "probe_on_assign": True,
    "scan_stale_sec": 86400,
    "block_unscanned_for_schedule": True,
}

DEFAULT_ACCOUNT_WARMUP_SETTINGS: dict[str, object] = {
    "enabled": True,
    "interval_sec": 60.0,
    "max_hot": 17,
    "max_sessions_per_hot": 3,
    "demote_cooldown_sec": 180.0,
    "freq_window_sec": 60.0,
    "freq_max_starts": 6,
    "startup_delay_sec": 20.0,
    # bootstrap = 首页 PoW 缓存；requirements = prepare+finalize 全链 CF 探活（更重）
    "depth": "requirements",
    "rotate_per_tick": 0,
    "hot_refresh_min_interval_sec": 300.0,
    "schedulable_only": True,
    # 连续 CF 探活失败后暂停对该号的 warmup 探活。
    # A2-1：旧默认 86400s(24h) 下，实测封禁到达率 1.67 个/小时 × 24h ≈ 40 个在途封禁
    # > 全池 19 个账号，该反馈环的不动点是可调度归零。改为分钟级基准 + 阶梯退避，
    # 上限 3600s：最坏情形 1.67 × 1h ≈ 1.7 个在途封禁（占 19 池的 8.8%）。
    "cf_fail_max_streak": 2,
    "cf_block_sec": 600.0,
    # 阶梯退避上限；同时兼作"退避记忆"衰减窗口：清白超过该时长后 repeat 计数归零。
    "cf_block_max_sec": 3600.0,
    "cf_block_backoff_factor": 2.0,
    # streak 滑动窗口：早于该时长的历史失败不再计入"连败"。
    # 旧实现无窗口无衰减，相隔任意时长的两次失败也会累积成 streak。
    # 600s = 10× tick(60s) = 2× hot_refresh(300s)，两次真正连续的探活失败仍会累积。
    "cf_fail_window_sec": 600.0,
    # A2-2 主动自愈：被封号按此间隔复探，每 tick 最多复探 N 个，避免猛打死号 / 重新制造 CF 压力。
    "cf_reprobe_interval_sec": 300.0,
    "cf_reprobe_max_per_tick": 2,
    # 单次探活失败后的"仅探活"冷却（不影响派发）：缓解 max_hot 常年大于存活数导致的反复探活。
    "cf_fail_probe_cooldown_sec": 120.0,
}


def _normalize_str_list(value: object, *, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    out = [str(item).strip() for item in value if str(item or "").strip()]
    return out or list(default)


def _normalize_text_nurture_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    prompts = _normalize_str_list(source.get("prompts"), default=[])
    follow_ups = _normalize_str_list(
        source.get("session_follow_up_prompts"),
        default=list(DEFAULT_TEXT_NURTURE["session_follow_up_prompts"]),  # type: ignore[arg-type]
    )
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_TEXT_NURTURE["enabled"])),
        "worker_enabled": _normalize_bool(source.get("worker_enabled"), bool(DEFAULT_TEXT_NURTURE["worker_enabled"])),
        "poll_interval_sec": max(2.0, float(source.get("poll_interval_sec", DEFAULT_TEXT_NURTURE["poll_interval_sec"]) or 2.0)),
        "max_per_hour": _normalize_positive_int(source.get("max_per_hour"), int(DEFAULT_TEXT_NURTURE["max_per_hour"]), 0),
        "max_per_account_per_day": _normalize_positive_int(
            source.get("max_per_account_per_day"),
            int(DEFAULT_TEXT_NURTURE["max_per_account_per_day"]),
            1,
        ),
        "daily_reset_tz": str(source.get("daily_reset_tz") or DEFAULT_TEXT_NURTURE["daily_reset_tz"]).strip()
        or str(DEFAULT_TEXT_NURTURE["daily_reset_tz"]),
        "turns_per_session": _normalize_positive_int(
            source.get("turns_per_session"),
            int(DEFAULT_TEXT_NURTURE["turns_per_session"]),
            1,
        ),
        "turn_gap_sec": max(0.0, float(source.get("turn_gap_sec", DEFAULT_TEXT_NURTURE["turn_gap_sec"]) or 0.0)),
        "require_persist_history": _normalize_bool(
            source.get("require_persist_history"),
            bool(DEFAULT_TEXT_NURTURE["require_persist_history"]),
        ),
        "auto_enqueue": _normalize_bool(source.get("auto_enqueue"), bool(DEFAULT_TEXT_NURTURE["auto_enqueue"])),
        "auto_enqueue_every_sec": max(
            60.0,
            float(source.get("auto_enqueue_every_sec", DEFAULT_TEXT_NURTURE["auto_enqueue_every_sec"]) or 60.0),
        ),
        "auto_enqueue_rotate_accounts": _normalize_bool(
            source.get("auto_enqueue_rotate_accounts"),
            bool(DEFAULT_TEXT_NURTURE["auto_enqueue_rotate_accounts"]),
        ),
        "count_manual_toward_daily_limit": _normalize_bool(
            source.get("count_manual_toward_daily_limit"),
            bool(DEFAULT_TEXT_NURTURE["count_manual_toward_daily_limit"]),
        ),
        "prompts": prompts,
        "session_follow_up_prompts": follow_ups,
        "model": str(source.get("model") or DEFAULT_TEXT_NURTURE["model"]).strip() or "auto",
    }


def _normalize_scheduler_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    jitter_lo = max(0.05, float(source.get("jitter_lo", DEFAULT_SCHEDULER_SETTINGS["jitter_lo"]) or 0.05))
    jitter_hi = max(jitter_lo, float(source.get("jitter_hi", DEFAULT_SCHEDULER_SETTINGS["jitter_hi"]) or jitter_lo))
    submit_lo = max(0.05, float(source.get("submit_interval_jitter_lo", DEFAULT_SCHEDULER_SETTINGS["submit_interval_jitter_lo"]) or 0.05))
    submit_hi = max(submit_lo, float(source.get("submit_interval_jitter_hi", DEFAULT_SCHEDULER_SETTINGS["submit_interval_jitter_hi"]) or submit_lo))
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_SCHEDULER_SETTINGS["enabled"])),
        "image_min_interval_sec": max(
            0.0,
            float(source.get("image_min_interval_sec", DEFAULT_SCHEDULER_SETTINGS["image_min_interval_sec"]) or 0.0),
        ),
        "text_min_interval_sec": max(
            0.0,
            float(source.get("text_min_interval_sec", DEFAULT_SCHEDULER_SETTINGS["text_min_interval_sec"]) or 0.0),
        ),
        "text_poisson_lambda_sec": max(
            0.0,
            float(source.get("text_poisson_lambda_sec", DEFAULT_SCHEDULER_SETTINGS["text_poisson_lambda_sec"]) or 0.0),
        ),
        "jitter_lo": jitter_lo,
        "jitter_hi": jitter_hi,
        "extra_poisson_lambda_sec": max(
            0.0,
            float(
                source.get("extra_poisson_lambda_sec", DEFAULT_SCHEDULER_SETTINGS["extra_poisson_lambda_sec"]) or 0.0
            ),
        ),
        "daily_usage_ratio": max(
            0.05,
            min(0.99, float(source.get("daily_usage_ratio", DEFAULT_SCHEDULER_SETTINGS["daily_usage_ratio"]) or 0.7)),
        ),
        "cooldown_429_sec": _normalize_positive_int(
            source.get("cooldown_429_sec"),
            int(DEFAULT_SCHEDULER_SETTINGS["cooldown_429_sec"]),
            60,
        ),
        "night_soft_weight": max(
            0.05,
            min(1.0, float(source.get("night_soft_weight", DEFAULT_SCHEDULER_SETTINGS["night_soft_weight"]) or 0.4)),
        ),
        "lunch_soft_weight": max(
            0.05,
            min(1.0, float(source.get("lunch_soft_weight", DEFAULT_SCHEDULER_SETTINGS["lunch_soft_weight"]) or 0.85)),
        ),
        "auto_scale_global_concurrency": _normalize_bool(
            source.get("auto_scale_global_concurrency"),
            bool(DEFAULT_SCHEDULER_SETTINGS["auto_scale_global_concurrency"]),
        ),
        "fail_streak_threshold": _normalize_positive_int(
            source.get("fail_streak_threshold"),
            int(DEFAULT_SCHEDULER_SETTINGS["fail_streak_threshold"]),
            1,
        ),
        "fail_cooldown_min_sec": max(
            60.0,
            float(source.get("fail_cooldown_min_sec", DEFAULT_SCHEDULER_SETTINGS["fail_cooldown_min_sec"]) or 60.0),
        ),
        "fail_cooldown_max_sec": max(
            60.0,
            float(source.get("fail_cooldown_max_sec", DEFAULT_SCHEDULER_SETTINGS["fail_cooldown_max_sec"]) or 60.0),
        ),
        "new_account_usage_cap": max(
            0.05,
            min(0.99, float(source.get("new_account_usage_cap", DEFAULT_SCHEDULER_SETTINGS["new_account_usage_cap"]) or 0.4)),
        ),
        "cohort_terminal_threshold": _normalize_positive_int(
            source.get("cohort_terminal_threshold"),
            int(DEFAULT_SCHEDULER_SETTINGS["cohort_terminal_threshold"]),
            1,
        ),
        "cohort_pause_hours": max(
            1.0,
            float(source.get("cohort_pause_hours", DEFAULT_SCHEDULER_SETTINGS["cohort_pause_hours"]) or 1.0),
        ),
        "submit_interval_jitter_lo": submit_lo,
        "submit_interval_jitter_hi": submit_hi,
        "resume_first_delay_sec": max(
            1.0,
            float(source.get("resume_first_delay_sec", DEFAULT_SCHEDULER_SETTINGS["resume_first_delay_sec"]) or 5.0),
        ),
        "resume_backoff_base_sec": max(
            1.0,
            float(source.get("resume_backoff_base_sec", DEFAULT_SCHEDULER_SETTINGS["resume_backoff_base_sec"]) or 5.0),
        ),
        "resume_backoff_cap_sec": max(
            5.0,
            float(source.get("resume_backoff_cap_sec", DEFAULT_SCHEDULER_SETTINGS["resume_backoff_cap_sec"]) or 60.0),
        ),
        "resume_wall_sec": max(
            60.0,
            float(source.get("resume_wall_sec", DEFAULT_SCHEDULER_SETTINGS["resume_wall_sec"]) or 240.0),
        ),
        "prompt_dedup_window_sec": max(
            0.0,
            float(source.get("prompt_dedup_window_sec", DEFAULT_SCHEDULER_SETTINGS["prompt_dedup_window_sec"]) or 0.0),
        ),
        "prompt_dedup_max_parallel": _normalize_positive_int(
            source.get("prompt_dedup_max_parallel"),
            int(DEFAULT_SCHEDULER_SETTINGS["prompt_dedup_max_parallel"]),
            1,
        ),
        "unrestricted": _normalize_bool(
            source.get("unrestricted"),
            bool(DEFAULT_SCHEDULER_SETTINGS["unrestricted"]),
        ),
    }


def _normalize_window_pair(value: object, default: list[str]) -> list[str]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [str(value[0] or default[0]), str(value[1] or default[1])]
    return [str(default[0]), str(default[1])]


def _normalize_proactive_refresh_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    workdays_raw = source.get("workdays", DEFAULT_PROACTIVE_REFRESH_SETTINGS["workdays"])
    workdays: list[int] = []
    if isinstance(workdays_raw, (list, tuple)):
        for item in workdays_raw:
            try:
                day = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 7 and day not in workdays:
                workdays.append(day)
    if not workdays:
        workdays = [1, 2, 3, 4, 5]
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROACTIVE_REFRESH_SETTINGS["enabled"])),
        "timezone": str(source.get("timezone") or DEFAULT_PROACTIVE_REFRESH_SETTINGS["timezone"]).strip()
        or "Asia/Singapore",
        "timezone_from_egress": _normalize_bool(
            source.get("timezone_from_egress"),
            bool(DEFAULT_PROACTIVE_REFRESH_SETTINGS["timezone_from_egress"]),
        ),
        "p_work": max(0.0, min(1.0, float(source.get("p_work", DEFAULT_PROACTIVE_REFRESH_SETTINGS["p_work"]) or 0.0))),
        "p_rest": max(0.0, min(1.0, float(source.get("p_rest", DEFAULT_PROACTIVE_REFRESH_SETTINGS["p_rest"]) or 0.0))),
        "window_work": _normalize_window_pair(
            source.get("window_work"),
            list(DEFAULT_PROACTIVE_REFRESH_SETTINGS["window_work"]),  # type: ignore[arg-type]
        ),
        "window_rest": _normalize_window_pair(
            source.get("window_rest"),
            list(DEFAULT_PROACTIVE_REFRESH_SETTINGS["window_rest"]),  # type: ignore[arg-type]
        ),
        "workdays": workdays,
        "per_account_per_day": max(
            1,
            _normalize_positive_int(
                source.get("per_account_per_day"),
                int(DEFAULT_PROACTIVE_REFRESH_SETTINGS["per_account_per_day"]),
                1,
            ),
        ),
        "minute_cap_k": max(
            1,
            _normalize_positive_int(
                source.get("minute_cap_k"),
                int(DEFAULT_PROACTIVE_REFRESH_SETTINGS["minute_cap_k"]),
                1,
            ),
        ),
        "minute_cap_k_rest": max(
            1,
            _normalize_positive_int(
                source.get("minute_cap_k_rest"),
                int(DEFAULT_PROACTIVE_REFRESH_SETTINGS["minute_cap_k_rest"]),
                1,
            ),
        ),
        "slot_jitter_minutes": _normalize_positive_int(
            source.get("slot_jitter_minutes"),
            int(DEFAULT_PROACTIVE_REFRESH_SETTINGS["slot_jitter_minutes"]),
            0,
        ),
        "tick_sec": max(15.0, float(source.get("tick_sec", DEFAULT_PROACTIVE_REFRESH_SETTINGS["tick_sec"]) or 60.0)),
        "startup_delay_sec": max(
            0.0,
            float(source.get("startup_delay_sec", DEFAULT_PROACTIVE_REFRESH_SETTINGS["startup_delay_sec"]) or 0.0),
        ),
    }


def _normalize_quota_refresh_schedule_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_QUOTA_REFRESH_SCHEDULE["enabled"])),
        "phases_per_day": max(
            1,
            _normalize_positive_int(
                source.get("phases_per_day"),
                int(DEFAULT_QUOTA_REFRESH_SCHEDULE["phases_per_day"]),
                1,
            ),
        ),
        "default_timezone": str(source.get("default_timezone") or DEFAULT_QUOTA_REFRESH_SCHEDULE["default_timezone"]).strip()
        or "Asia/Singapore",
        "timezone_from_egress": _normalize_bool(
            source.get("timezone_from_egress"),
            bool(DEFAULT_QUOTA_REFRESH_SCHEDULE["timezone_from_egress"]),
        ),
        "account_jitter_min_minutes": max(
            0,
            _normalize_positive_int(
                source.get("account_jitter_min_minutes"),
                int(DEFAULT_QUOTA_REFRESH_SCHEDULE["account_jitter_min_minutes"]),
                0,
            ),
        ),
        "account_jitter_max_minutes": max(
            0,
            _normalize_positive_int(
                source.get("account_jitter_max_minutes"),
                int(DEFAULT_QUOTA_REFRESH_SCHEDULE["account_jitter_max_minutes"]),
                0,
            ),
        ),
        "binding_min_gap_hours": max(
            0.0,
            float(source.get("binding_min_gap_hours", DEFAULT_QUOTA_REFRESH_SCHEDULE["binding_min_gap_hours"]) or 0.0),
        ),
        "tick_sec": max(5.0, float(source.get("tick_sec", DEFAULT_QUOTA_REFRESH_SCHEDULE["tick_sec"]) or 60.0)),
        "pre_restore_refresh_minutes": max(
            0.0,
            float(
                source.get("pre_restore_refresh_minutes", DEFAULT_QUOTA_REFRESH_SCHEDULE["pre_restore_refresh_minutes"])
                or 0.0
            ),
        ),
    }


def _normalize_quota_window_prime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    skip_raw = source.get("skip_panda_sync_states", DEFAULT_QUOTA_WINDOW_PRIME["skip_panda_sync_states"])
    skip_states = [str(item).strip() for item in skip_raw] if isinstance(skip_raw, list) else ["staging", "ready"]
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_QUOTA_WINDOW_PRIME["enabled"])),
        "full_quota": max(
            1,
            _normalize_positive_int(source.get("full_quota"), int(DEFAULT_QUOTA_WINDOW_PRIME["full_quota"]), 1),
        ),
        "min_account_age_days": max(
            0.0,
            float(source.get("min_account_age_days", DEFAULT_QUOTA_WINDOW_PRIME["min_account_age_days"]) or 0.0),
        ),
        "image_size": str(source.get("image_size") or DEFAULT_QUOTA_WINDOW_PRIME["image_size"]).strip() or "256x256",
        "image_quality": str(source.get("image_quality") or DEFAULT_QUOTA_WINDOW_PRIME["image_quality"]).strip() or "low",
        "auto_phase_index": max(
            0,
            min(3, _normalize_positive_int(source.get("auto_phase_index"), int(DEFAULT_QUOTA_WINDOW_PRIME["auto_phase_index"]), 0)),
        ),
        "max_concurrent_global": max(
            1,
            _normalize_positive_int(
                source.get("max_concurrent_global"),
                int(DEFAULT_QUOTA_WINDOW_PRIME["max_concurrent_global"]),
                1,
            ),
        ),
        "max_concurrent_per_binding": max(
            1,
            _normalize_positive_int(
                source.get("max_concurrent_per_binding"),
                int(DEFAULT_QUOTA_WINDOW_PRIME["max_concurrent_per_binding"]),
                1,
            ),
        ),
        "max_auto_attempts": max(
            1,
            _normalize_positive_int(
                source.get("max_auto_attempts"),
                int(DEFAULT_QUOTA_WINDOW_PRIME["max_auto_attempts"]),
                1,
            ),
        ),
        "retry_interval_hours": max(
            1.0,
            float(source.get("retry_interval_hours", DEFAULT_QUOTA_WINDOW_PRIME["retry_interval_hours"]) or 24.0),
        ),
        "skip_panda_sync_states": skip_states,
        "defer_if_refresh_due_within_minutes": max(
            0.0,
            float(
                source.get(
                    "defer_if_refresh_due_within_minutes",
                    DEFAULT_QUOTA_WINDOW_PRIME["defer_if_refresh_due_within_minutes"],
                )
                or 0.0
            ),
        ),
        "tick_sec": max(5.0, float(source.get("tick_sec", DEFAULT_QUOTA_WINDOW_PRIME["tick_sec"]) or 30.0)),
    }


def _normalize_webshare_cf_scan_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    interval_min = max(300.0, float(source.get("interval_min_sec", DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["interval_min_sec"]) or 3600))
    interval_max = max(
        interval_min,
        float(source.get("interval_max_sec", DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["interval_max_sec"]) or interval_min),
    )
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["enabled"])),
        "pool_path": str(source.get("pool_path") or DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["pool_path"]).strip(),
        "interval_min_sec": interval_min,
        "interval_max_sec": interval_max,
        "batch_size": max(
            1,
            _normalize_positive_int(
                source.get("batch_size"),
                int(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["batch_size"]),
                1,
            ),
        ),
        "workers": max(
            1,
            _normalize_positive_int(
                source.get("workers"),
                int(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["workers"]),
                1,
            ),
        ),
        "auto_quarantine": _normalize_bool(
            source.get("auto_quarantine"),
            bool(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["auto_quarantine"]),
        ),
        "skip_quarantined": _normalize_bool(
            source.get("skip_quarantined"),
            bool(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["skip_quarantined"]),
        ),
        "active_window": _normalize_window_pair(
            source.get("active_window"),
            list(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["active_window"]),  # type: ignore[arg-type]
        ),
        "timezone": str(source.get("timezone") or DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["timezone"]).strip()
        or "Asia/Singapore",
        "startup_delay_sec": max(
            0.0,
            float(source.get("startup_delay_sec", DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["startup_delay_sec"]) or 0.0),
        ),
        "probe_timeout_sec": max(
            10.0,
            float(source.get("probe_timeout_sec", DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["probe_timeout_sec"]) or 45.0),
        ),
        "require_cf_ok_for_image": _normalize_bool(
            source.get("require_cf_ok_for_image"),
            bool(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["require_cf_ok_for_image"]),
        ),
        "probe_on_assign": _normalize_bool(
            source.get("probe_on_assign"),
            bool(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["probe_on_assign"]),
        ),
        "scan_stale_sec": max(
            300.0,
            float(source.get("scan_stale_sec", DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["scan_stale_sec"]) or 86400),
        ),
        "block_unscanned_for_schedule": _normalize_bool(
            source.get("block_unscanned_for_schedule"),
            bool(DEFAULT_WEBSHARE_CF_SCAN_SETTINGS["block_unscanned_for_schedule"]),
        ),
    }


def _normalize_account_warmup_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    depth = str(source.get("depth") or DEFAULT_ACCOUNT_WARMUP_SETTINGS["depth"]).strip().lower()
    if depth not in {"bootstrap", "requirements"}:
        depth = str(DEFAULT_ACCOUNT_WARMUP_SETTINGS["depth"])
    # A2-1 上限先算出来，cf_block_sec 再夹进 [300, 上限]。
    # 向后兼容：历史配置里持久化的 cf_block_sec=86400 会被自动压到新上限，
    # 无需人工改配置文件即可退出"24h 封禁 → 可调度归零"的反馈环。
    cf_block_max_sec = min(
        21600.0,
        max(
            300.0,
            float(
                source.get("cf_block_max_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_block_max_sec"])
                or 3600.0
            ),
        ),
    )
    cf_block_sec = min(
        cf_block_max_sec,
        max(
            300.0,
            float(source.get("cf_block_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_block_sec"]) or 600.0),
        ),
    )
    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_ACCOUNT_WARMUP_SETTINGS["enabled"])),
        "interval_sec": max(
            30.0,
            float(source.get("interval_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["interval_sec"]) or 60.0),
        ),
        "max_hot": max(
            1,
            _normalize_positive_int(
                source.get("max_hot"),
                int(DEFAULT_ACCOUNT_WARMUP_SETTINGS["max_hot"]),
                1,
            ),
        ),
        "max_sessions_per_hot": max(
            1,
            _normalize_positive_int(
                source.get("max_sessions_per_hot"),
                int(DEFAULT_ACCOUNT_WARMUP_SETTINGS["max_sessions_per_hot"]),
                1,
            ),
        ),
        "demote_cooldown_sec": max(
            10.0,
            float(source.get("demote_cooldown_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["demote_cooldown_sec"]) or 180.0),
        ),
        "freq_window_sec": max(
            10.0,
            float(source.get("freq_window_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["freq_window_sec"]) or 60.0),
        ),
        "freq_max_starts": max(
            2,
            _normalize_positive_int(
                source.get("freq_max_starts"),
                int(DEFAULT_ACCOUNT_WARMUP_SETTINGS["freq_max_starts"]),
                2,
            ),
        ),
        "startup_delay_sec": max(
            0.0,
            float(source.get("startup_delay_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["startup_delay_sec"]) or 0.0),
        ),
        "depth": depth,
        "rotate_per_tick": max(
            0,
            _normalize_positive_int(
                source.get("rotate_per_tick"),
                int(DEFAULT_ACCOUNT_WARMUP_SETTINGS["rotate_per_tick"]),
                0,
            ),
        ),
        "hot_refresh_min_interval_sec": max(
            60.0,
            float(
                source.get(
                    "hot_refresh_min_interval_sec",
                    DEFAULT_ACCOUNT_WARMUP_SETTINGS["hot_refresh_min_interval_sec"],
                )
                or 300.0
            ),
        ),
        "schedulable_only": _normalize_bool(
            source.get("schedulable_only"),
            bool(DEFAULT_ACCOUNT_WARMUP_SETTINGS["schedulable_only"]),
        ),
        "cf_fail_max_streak": max(
            1,
            _normalize_positive_int(
                source.get("cf_fail_max_streak"),
                int(DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_fail_max_streak"]),
                1,
            ),
        ),
        "cf_block_sec": cf_block_sec,
        "cf_block_max_sec": cf_block_max_sec,
        "cf_block_backoff_factor": min(
            8.0,
            max(
                1.0,
                float(
                    source.get(
                        "cf_block_backoff_factor",
                        DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_block_backoff_factor"],
                    )
                    or 2.0
                ),
            ),
        ),
        "cf_fail_window_sec": max(
            60.0,
            float(
                source.get("cf_fail_window_sec", DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_fail_window_sec"])
                or 600.0
            ),
        ),
        "cf_reprobe_interval_sec": max(
            60.0,
            float(
                source.get(
                    "cf_reprobe_interval_sec",
                    DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_reprobe_interval_sec"],
                )
                or 300.0
            ),
        ),
        "cf_reprobe_max_per_tick": max(
            0,
            _normalize_positive_int(
                source.get("cf_reprobe_max_per_tick"),
                int(DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_reprobe_max_per_tick"]),
                0,
            ),
        ),
        "cf_fail_probe_cooldown_sec": max(
            0.0,
            float(
                source.get(
                    "cf_fail_probe_cooldown_sec",
                    DEFAULT_ACCOUNT_WARMUP_SETTINGS["cf_fail_probe_cooldown_sec"],
                )
                or 0.0
            ),
        ),
    }


def _normalize_workload_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    mode = str(source.get("mode") or DEFAULT_WORKLOAD_SETTINGS["mode"]).strip().lower()
    if mode not in {"shadow", "live"}:
        mode = "shadow"
    text_queue_mode = str(
        source.get("text_queue_mode") or DEFAULT_WORKLOAD_SETTINGS["text_queue_mode"]
    ).strip().lower()
    if text_queue_mode not in {"off", "busy_only", "always"}:
        text_queue_mode = "off"
    hashes_raw = source.get("canary_token_hashes")
    hashes: list[str] = []
    if isinstance(hashes_raw, list):
        for item in hashes_raw:
            text = str(item or "").strip().lower()
            if text and text not in hashes:
                hashes.append(text)
    return {
        "mode": mode,
        "text_queue_mode": text_queue_mode,
        "canary_token_hashes": hashes,
        "global_text_inflight": max(
            1,
            _normalize_positive_int(
                source.get("global_text_inflight"),
                int(DEFAULT_WORKLOAD_SETTINGS["global_text_inflight"]),
                1,
            ),
        ),
        "auto_live_min_ready": max(
            0,
            _normalize_positive_int(
                source.get("auto_live_min_ready"),
                int(DEFAULT_WORKLOAD_SETTINGS["auto_live_min_ready"]),
                0,
            ),
        ),
    }


def _validate_image_storage_settings(settings: dict[str, object]) -> None:
    if not _normalize_bool(settings.get("enabled"), False):
        return
    if not str(settings.get("webdav_url") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV URL")
    if not str(settings.get("webdav_password") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV 密码")


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_dir():
        print(
            f"Warning: {name} at '{path}' is a directory, ignoring it and falling back to other configuration sources.",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = _read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    try:
        refresh_interval = int(raw_config.get("refresh_account_interval_minute", 5))
    except (TypeError, ValueError):
        refresh_interval = 5

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
    )


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._storage_backend: StorageBackend | None = None
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def refresh_account_interval_minute(self) -> int:
        try:
            return int(self.data.get("refresh_account_interval_minute", 5))
        except (TypeError, ValueError):
            return 5

    @property
    def image_retention_days(self) -> int:
        try:
            return max(1, int(self.data.get("image_retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def image_poll_timeout_secs(self) -> int:
        try:
            return max(1, int(self.data.get("image_poll_timeout_secs", 120)))
        except (TypeError, ValueError):
            return 120

    @property
    def image_generation_poll_timeout_secs(self) -> int:
        try:
            settings = self.get_image_task_queue_settings()
            return max(self.image_poll_timeout_secs, int(settings.get("generation_poll_timeout_secs") or 180))
        except (TypeError, ValueError):
            return max(self.image_poll_timeout_secs, 180)

    @property
    def image_edit_poll_timeout_secs(self) -> int:
        try:
            settings = self.get_image_task_queue_settings()
            return max(self.image_poll_timeout_secs, int(settings.get("edit_poll_timeout_secs") or 300))
        except (TypeError, ValueError):
            return max(self.image_poll_timeout_secs, 300)

    @property
    def image_multi_reference_poll_timeout_secs(self) -> int:
        try:
            settings = self.get_image_task_queue_settings()
            return max(self.image_edit_poll_timeout_secs, int(settings.get("multi_reference_poll_timeout_secs") or 360))
        except (TypeError, ValueError):
            return max(self.image_edit_poll_timeout_secs, 360)

    @property
    def image_pre_conversation_timeout_secs(self) -> int:
        try:
            settings = self.get_image_task_queue_settings()
            return max(30, int(settings.get("pre_conversation_timeout_secs") or 240))
        except (TypeError, ValueError):
            return 240

    @property
    def image_sse_post_ready_timeout_secs(self) -> float | None:
        """Wall clock after conversation_id before abandoning SSE and falling through to poll.

        ``None``/false disables the valve (legacy hang-until-upstream-closes).
        Default 75s: normal e2e finishes earlier; hanging SSE must not block forever.
        Never use ~15s — that kills free-tier tool latency.
        """
        if "image_sse_post_ready_timeout_secs" not in self.data:
            return 75.0
        raw = self.data.get("image_sse_post_ready_timeout_secs")
        if raw is None or raw is False or raw == "":
            return None
        try:
            return max(5.0, float(raw))
        except (TypeError, ValueError):
            return 75.0

    @property
    def image_ss_stage_wall_timeout_secs(self) -> float:
        """Fast-fail wall for the SSE streaming phase only; default 75s.

        Armed at ``acquire_ss`` and disarmed when the SSE stream ends. It must NOT be
        used to bound the conversation poll (120/300/360s), URL resolve, download or
        return window — see ``PipelineRun.assert_ss_wall_ok`` and audit 28 §B1.
        For the whole-lease watchdog deadline use ``image_ss_slot_deadline_secs``.
        """
        try:
            settings = self.get_image_pipeline_settings()
            return max(5.0, float(settings.get("ss_stage_wall_timeout_secs") or 120))
        except (TypeError, ValueError):
            return 75.0

    @property
    def image_ss_slot_deadline_secs(self) -> float:
        """Watchdog forced-release deadline for a whole sS/account lease; default 900s.

        Covers SSE + poll + resolve + download + return window, so it must stay above
        ``image_multi_reference_poll_timeout_secs`` plus download headroom.
        """
        try:
            settings = self.get_image_pipeline_settings()
            return max(30.0, float(settings.get("ss_slot_deadline_secs") or 900))
        except (TypeError, ValueError):
            return 900.0

    @property
    def image_pool_acquire_timeout_secs(self) -> float:
        """Max queue wait for an upload/pS/sS/download slot; default 300s.

        ``SlotPool.acquire``/``SemaphorePool.acquire`` fall back to an unbounded
        ``Event.wait()`` when no timeout is supplied, so a single leaked slot used to
        wedge every subsequent image request forever (audit 28 §B2).
        """
        try:
            settings = self.get_image_pipeline_settings()
            return max(5.0, float(settings.get("pool_acquire_timeout_secs") or 300))
        except (TypeError, ValueError):
            return 300.0

    @property
    def image_pre_conversation_max_attempts(self) -> int:
        try:
            settings = self.get_image_task_queue_settings()
            return max(1, int(settings.get("pre_conversation_max_attempts") or 2))
        except (TypeError, ValueError):
            return 2

    @property
    def image_pre_conversation_retry_backoff_secs(self) -> float:
        try:
            settings = self.get_image_task_queue_settings()
            return max(0.0, float(settings.get("pre_conversation_retry_backoff_secs") or 0.0))
        except (TypeError, ValueError):
            return 5.0

    @property
    def image_poll_interval_secs(self) -> float:
        try:
            return max(0.5, float(self.data.get("image_poll_interval_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_poll_initial_wait_secs(self) -> float:
        """Image generation upstream takes ~30s; polling immediately wastes requests
        and trips a transient 429. Default 20s gives the conversation document time
        to commit before the first poll."""
        try:
            return max(0.0, float(self.data.get("image_poll_initial_wait_secs", 20.0)))
        except (TypeError, ValueError):
            return 20.0

    @property
    def image_poll_early_sse_ms(self) -> float:
        try:
            return max(0.0, float(self.data.get("image_poll_early_sse_ms", 5000.0)))
        except (TypeError, ValueError):
            return 5000.0

    @property
    def image_poll_early_sse_initial_wait_secs(self) -> float:
        try:
            return max(0.0, float(self.data.get("image_poll_early_sse_initial_wait_secs", 25.0)))
        except (TypeError, ValueError):
            return 25.0

    @property
    def image_poll_429_abort_streak(self) -> int:
        try:
            return max(1, int(self.data.get("image_poll_429_abort_streak", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def image_poll_max_upstream_gets(self) -> int:
        """Legacy hard cap on conversation GETs per poll loop (default 24).

        NOTE (audit 28 §B6 / fix A4-4): 24 GETs × 3s interval ≈ 82~120s of wall
        clock, which made the configured 300s/360s poll budgets unreachable. The
        poll loop now uses ``image_poll_max_upstream_gets_explicit``: when the key
        is absent the cap is derived from the wall budget so the wall binds. This
        property remains the value reported by ``get()`` and the fallback for
        callers that want a plain int.
        """
        try:
            return max(1, int(self.data.get("image_poll_max_upstream_gets", 24)))
        except (TypeError, ValueError):
            return 24

    @property
    def image_poll_max_upstream_gets_explicit(self) -> int | None:
        """``image_poll_max_upstream_gets`` when the operator set it, else ``None``.

        ``None`` means "derive the GET cap from the wall budget" (fix A4-4). An
        explicitly configured value is still honoured verbatim so existing
        deployments do not silently change behaviour.
        """
        if "image_poll_max_upstream_gets" not in self.data:
            return None
        raw = self.data.get("image_poll_max_upstream_gets")
        if raw is None or raw is False or raw == "":
            return None
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return None

    @property
    def image_poll_max_tasks_gets(self) -> int:
        """Hard cap on /backend-api/tasks GETs per logical poll loop (low-frequency)."""
        try:
            return max(0, int(self.data.get("image_poll_max_tasks_gets", 4)))
        except (TypeError, ValueError):
            return 4

    @property
    def image_poll_tasks_every_n_attempts(self) -> int:
        """Query tasks on attempt 1 and every N conversation poll attempts thereafter."""
        try:
            return max(1, int(self.data.get("image_poll_tasks_every_n_attempts", 4)))
        except (TypeError, ValueError):
            return 4

    @property
    def image_poll_cf_abort_streak(self) -> int:
        """连续 CF 边缘拦截多少次后中止生图轮询（默认 2，避免空挂到 poll_timeout）。"""
        try:
            return max(1, int(self.data.get("image_poll_cf_abort_streak", 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def image_poll_cf_retry_backoff_secs(self) -> float:
        """poll 遇 CF403 时的短退避（秒），优先于通用指数退避。"""
        try:
            return max(0.0, min(30.0, float(self.data.get("image_poll_cf_retry_backoff_secs", 1.5))))
        except (TypeError, ValueError):
            return 1.5

    @property
    def image_poll_cf_swap_retry(self) -> bool:
        """poll cf_abort 后是否换出口并重试 resolve（默认开）。"""
        try:
            raw = self.data.get("image_poll_cf_swap_retry", True)
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)
        except (TypeError, ValueError):
            return True

    @property
    def image_poll_cf_swap_retry_max(self) -> int:
        """poll cf_abort 后最多换出口重试轮数（默认 5）。"""
        try:
            return max(1, min(10, int(self.data.get("image_poll_cf_swap_retry_max", 5))))
        except (TypeError, ValueError):
            return 5

    @property
    def proxy_url(self) -> str:
        """兼容旧调用点的运行时代理地址。

        新配置集中在 ``proxy_runtime.proxy_url``；部分异步续轮询代码仍按
        ``config.proxy_url`` 读取。保留只读属性，避免 timeout_pending resume
        路径因属性缺失失败。
        """
        try:
            return str(self.get_proxy_runtime_settings().get("proxy_url") or "").strip()
        except Exception:
            return ""

    @property
    def image_account_concurrency(self) -> int:
        try:
            return max(1, int(self.data.get("image_account_concurrency", 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def dispatch_hot_only(self) -> bool:
        """为 True 时仅暖号池 hot 账号参与生图取号；默认 False（全部 image_schedulable 可派）。"""
        try:
            return bool(self.data.get("dispatch_hot_only", False))
        except Exception:
            return False

    @property
    def proxy_binding_max_accounts(self) -> int:
        """同一 Webshare proxy_binding 最多承载多少活跃账号（注册/生图调度）。

        默认 5：同 IP 可挂最多 5 个号；超过才视为过密并隔离/挡调度。
        """
        try:
            return max(1, int(self.data.get("proxy_binding_max_accounts", 5)))
        except (TypeError, ValueError):
            return 5

    @property
    def image_binding_inflight_max(self) -> int:
        """同一 proxy_binding 同时允许的生图路数；默认 1（STAB-B4：防同出口突发）。"""
        try:
            return max(1, int(self.data.get("image_binding_inflight_max", 1)))
        except (TypeError, ValueError):
            return 1

    @property
    def image_global_concurrency(self) -> int:
        """全局图片并发上限；0 表示不限制。

        单账号并发只能避免同一个账号被重复打爆，不能阻止无线画布一次性
        发起十几个请求把整个 Panda 进程拖入长尾。这里作为服务级闸门。
        """
        try:
            return max(0, int(self.data.get("image_global_concurrency", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def image_global_queue_timeout_secs(self) -> float:
        """全局并发满时最多等待多久；0 表示立即快速失败。"""
        try:
            return max(0.0, min(300.0, float(self.data.get("image_global_queue_timeout_secs", 0.0))))
        except (TypeError, ValueError):
            return 0.0

    @property
    def image_parallel_generation(self) -> bool:
        value = self.data.get("image_parallel_generation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_require_recent_quota_refresh(self) -> bool:
        value = self.data.get("image_require_recent_quota_refresh", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_quota_freshness_hours(self) -> float:
        try:
            return max(0.0, float(self.data.get("image_quota_freshness_hours", 24.0)))
        except (TypeError, ValueError):
            return 24.0

    @property
    def image_token_max_attempts(self) -> int:
        try:
            return max(1, min(1000, int(self.data.get("image_token_max_attempts", 100))))
        except (TypeError, ValueError):
            return 100

    @property
    def image_preflight_failure_backoff_sec(self) -> float:
        try:
            return max(0.0, min(3600.0, float(self.data.get("image_preflight_failure_backoff_sec", 600.0))))
        except (TypeError, ValueError):
            return 600.0

    @property
    def image_preflight_min_interval_sec(self) -> float:
        """同一账号图片取号 preflight 的最短间隔。

        在此窗口内若账号本地状态仍可调度，就不再每次生图前都触发远程配额刷新，
        以减少 OpenAI 探活请求与账号存储写入。
        """
        try:
            return max(0.0, min(3600.0, float(self.data.get("image_preflight_min_interval_sec", 120.0))))
        except (TypeError, ValueError):
            return 120.0

    @property
    def newapi_image_sync_wait_timeout_secs(self) -> float:
        try:
            raw = self.data.get("newapi_image_sync_wait_timeout_secs")
            if raw is None:
                raw = self.data.get("newapi_image_attempt_budget_secs", 180.0)
            return max(60.0, min(900.0, float(raw)))
        except (TypeError, ValueError):
            return 180.0

    @property
    def newapi_image_attempt_budget_secs(self) -> float:
        try:
            return max(60.0, min(900.0, float(self.data.get("newapi_image_attempt_budget_secs", 180.0))))
        except (TypeError, ValueError):
            return 180.0

    @property
    def image_attempt_sse_phase_secs(self) -> float:
        try:
            explicit = self.data.get("image_attempt_sse_phase_secs")
            if explicit is not None:
                return max(10.0, min(600.0, float(explicit)))
            return max(10.0, float(self.image_ss_stage_wall_timeout_secs))
        except (TypeError, ValueError):
            return 120.0

    @property
    def image_attempt_poll_phase_secs(self) -> float:
        try:
            explicit = self.data.get("image_attempt_poll_phase_secs")
            if explicit is not None:
                return max(5.0, min(600.0, float(explicit)))
            return max(5.0, float(self.image_generation_poll_timeout_secs))
        except (TypeError, ValueError):
            return 60.0

    @property
    def image_poll_after_slow_sse_ms(self) -> float:
        try:
            return max(0.0, float(self.data.get("image_poll_after_slow_sse_ms", 45000.0)))
        except (TypeError, ValueError):
            return 45000.0

    @property
    def image_poll_after_slow_sse_initial_wait_secs(self) -> float:
        try:
            return max(0.0, float(self.data.get("image_poll_after_slow_sse_initial_wait_secs", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    @property
    def newapi_image_sync_poll_interval_secs(self) -> float:
        try:
            return max(0.2, min(10.0, float(self.data.get("newapi_image_sync_poll_interval_secs", 1.5))))
        except (TypeError, ValueError):
            return 1.5

    @property
    def newapi_image_sync_handoff_on_timeout_pending(self) -> bool:
        try:
            raw = self.data.get("newapi_image_sync_handoff_on_timeout_pending", True)
            if isinstance(raw, str):
                return raw.strip().lower() not in {"0", "false", "no", "off"}
            return bool(raw)
        except Exception:
            return True

    @property
    def newapi_image_sync_admission_max(self) -> int:
        """同时挂起的同步 /v1/images/* 等待席位数；与上游 image_global_concurrency 解耦。"""
        try:
            return max(1, min(64, int(self.data.get("newapi_image_sync_admission_max", 12))))
        except (TypeError, ValueError):
            return 12

    @property
    def newapi_image_sync_admission_max_eta_secs(self) -> float:
        """同步准入的最大预估等待；超过则 429，避免单图被拖成超长尾。"""
        try:
            return max(30.0, min(600.0, float(self.data.get("newapi_image_sync_admission_max_eta_secs", 180.0))))
        except (TypeError, ValueError):
            return 180.0

    @property
    def image_return_window_size(self) -> int:
        """同时进入“下载图片 + b64/url 组装”回传窗口的最大图片数；0 表示不限制。"""
        try:
            return max(0, min(200, int(self.data.get("image_return_window_size", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def image_return_window_timeout_secs(self) -> float:
        """等待回传窗口的最长时间，超时返回 5xx，避免尾部无限拖尾。"""
        try:
            return max(1.0, min(600.0, float(self.data.get("image_return_window_timeout_secs", 180.0))))
        except (TypeError, ValueError):
            return 180.0

    @property
    def image_settle_enabled(self) -> bool:
        """图片二次确认机制：找到 file_ids 后等待一段时间再次确认。"""
        value = self.data.get("image_settle_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_spa_tool_path(self) -> bool:
        """纯 HTTP auto-tool 生图路径；默认开，false 回退 picture_v2 canary。"""
        value = self.data.get("image_spa_tool_path", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_check_before_hit_enabled(self) -> bool:
        """先check再hit：通过轮询确认 file_ids 存在后再返回，而非仅依赖 SSE 事件。"""
        value = self.data.get("image_check_before_hit_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_secs(self) -> float:
        """二次确认等待时间（秒）。"""
        try:
            return max(0.5, float(self.data.get("image_settle_secs", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    @property
    def image_sediment_fast_poll_enabled(self) -> bool:
        """SSE 已带 sediment id 时走短 settle + 跳过双确认 poll。"""
        value = self.data.get("image_sediment_fast_poll_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_secs_sediment(self) -> float:
        """sediment 快路径 settle（秒）。"""
        try:
            return max(0.0, float(self.data.get("image_settle_secs_sediment", 0.5)))
        except (TypeError, ValueError):
            return 0.5

    @property
    def image_release_account_after_sse(self) -> bool:
        """SSE 结束后释放账号 inflight，poll 不占全局槽。"""
        pipeline = self.get_image_pipeline_settings()
        if "release_account_after_sse" in pipeline:
            return bool(pipeline.get("release_account_after_sse"))
        value = self.data.get("image_release_account_after_sse", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_rate_limited_accounts(self) -> bool:
        value = self.data.get("auto_remove_rate_limited_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_relogin_after_refresh(self) -> bool:
        value = self.data.get("auto_relogin_after_refresh", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def text_chat_persist_history(self) -> bool:
        value = self.data.get("text_chat_persist_history", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def text_chat_reuse_conversation(self) -> bool:
        value = self.data.get("text_chat_reuse_conversation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def log_levels(self) -> list[str]:
        levels = self.data.get("log_levels")
        if not isinstance(levels, list):
            return []
        allowed = {"debug", "info", "warning", "error"}
        return [level for item in levels if (level := str(item or "").strip().lower()) in allowed]

    @property
    def sensitive_words(self) -> list[str]:
        words = self.data.get("sensitive_words")
        return [word for item in words if (word := str(item or "").strip())] if isinstance(words, list) else []

    @property
    def ai_review(self) -> dict[str, object]:
        value = self.data.get("ai_review")
        return value if isinstance(value, dict) else {}

    @property
    def global_system_prompt(self) -> str:
        return str(self.data.get("global_system_prompt") or "").strip()

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def image_thumbnails_dir(self) -> Path:
        path = DATA_DIR / "image_thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self) -> int:
        cutoff = time.time() - self.image_retention_days * 86400
        removed = 0
        for path in self.images_dir.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        for path in sorted((p for p in self.images_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        return removed

    @property
    def base_url(self) -> str:
        return str(
            os.getenv("CHATGPT2API_BASE_URL")
            or self.data.get("base_url")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get(self) -> dict[str, object]:
        data = dict(self.data)
        data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
        data["image_retention_days"] = self.image_retention_days
        data["image_poll_timeout_secs"] = self.image_poll_timeout_secs
        data["image_generation_poll_timeout_secs"] = self.image_generation_poll_timeout_secs
        data["image_edit_poll_timeout_secs"] = self.image_edit_poll_timeout_secs
        data["image_multi_reference_poll_timeout_secs"] = self.image_multi_reference_poll_timeout_secs
        data["image_pre_conversation_timeout_secs"] = self.image_pre_conversation_timeout_secs
        data["image_pre_conversation_max_attempts"] = self.image_pre_conversation_max_attempts
        data["image_pre_conversation_retry_backoff_secs"] = self.image_pre_conversation_retry_backoff_secs
        data["image_poll_interval_secs"] = self.image_poll_interval_secs
        data["image_poll_initial_wait_secs"] = self.image_poll_initial_wait_secs
        data["image_poll_early_sse_ms"] = self.image_poll_early_sse_ms
        data["image_poll_early_sse_initial_wait_secs"] = self.image_poll_early_sse_initial_wait_secs
        data["image_poll_429_abort_streak"] = self.image_poll_429_abort_streak
        data["image_poll_max_upstream_gets"] = self.image_poll_max_upstream_gets
        data["image_poll_max_tasks_gets"] = self.image_poll_max_tasks_gets
        data["image_poll_tasks_every_n_attempts"] = self.image_poll_tasks_every_n_attempts
        data["image_poll_cf_abort_streak"] = self.image_poll_cf_abort_streak
        data["image_poll_cf_retry_backoff_secs"] = self.image_poll_cf_retry_backoff_secs
        data["image_poll_cf_swap_retry"] = self.image_poll_cf_swap_retry
        data["image_poll_cf_swap_retry_max"] = self.image_poll_cf_swap_retry_max
        data["image_account_concurrency"] = self.image_account_concurrency
        data["dispatch_hot_only"] = self.dispatch_hot_only
        data["proxy_binding_max_accounts"] = self.proxy_binding_max_accounts
        data["image_binding_inflight_max"] = self.image_binding_inflight_max
        data["image_global_concurrency"] = self.image_global_concurrency
        data["image_global_queue_timeout_secs"] = self.image_global_queue_timeout_secs
        data["image_parallel_generation"] = self.image_parallel_generation
        data["image_spa_tool_path"] = self.image_spa_tool_path
        data["image_require_recent_quota_refresh"] = self.image_require_recent_quota_refresh
        data["image_quota_freshness_hours"] = self.image_quota_freshness_hours
        data["image_token_max_attempts"] = self.image_token_max_attempts
        data["image_preflight_failure_backoff_sec"] = self.image_preflight_failure_backoff_sec
        data["image_preflight_min_interval_sec"] = self.image_preflight_min_interval_sec
        data["image_return_window_size"] = self.image_return_window_size
        data["image_return_window_timeout_secs"] = self.image_return_window_timeout_secs
        data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
        data["auto_remove_rate_limited_accounts"] = self.auto_remove_rate_limited_accounts
        data["auto_relogin_after_refresh"] = self.auto_relogin_after_refresh
        data["log_levels"] = self.log_levels
        data["sensitive_words"] = self.sensitive_words
        data["ai_review"] = self.ai_review
        data["global_system_prompt"] = self.global_system_prompt
        data["text_nurture"] = self.get_text_nurture_settings()
        data["backup"] = self.get_backup_settings()
        data["image_storage"] = self.get_image_storage_settings()
        data["chat_completion_cache"] = self.get_chat_completion_cache_settings()
        data["image_task_queue"] = self.get_image_task_queue_settings()
        data["image_reference_assets"] = self.get_image_reference_assets_settings()
        data["image_deadlock_guard"] = self.get_image_deadlock_guard_settings()
        data["image_pipeline_watchdog"] = self.get_image_pipeline_watchdog_settings()
        data["proxy_runtime"] = self.get_public_proxy_runtime_settings()
        data["third_party_apps"] = self.get_third_party_apps_settings()
        data["account_refresh_all"] = self.get_account_refresh_all_settings()
        data["account_maintenance_loop"] = self.get_account_maintenance_loop_settings()
        data["outlook_auto_recovery"] = self.get_outlook_auto_recovery_settings()
        data["scheduler"] = self.get_scheduler_settings()
        data["proactive_refresh"] = self.get_proactive_refresh_settings()
        data["quota_refresh_schedule"] = self.get_quota_refresh_schedule_settings()
        data["quota_window_prime"] = self.get_quota_window_prime_settings()
        data["webshare_cf_scan"] = self.get_webshare_cf_scan_settings()
        data["account_warmup"] = self.get_account_warmup_settings()
        data["panda_sync"] = self.get_public_panda_sync_settings()
        data.pop("auth-key", None)
        return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return _normalize_proxy_runtime_settings(self.data.get("proxy_runtime"))

    def get_public_proxy_runtime_settings(self) -> dict[str, object]:
        runtime = copy.deepcopy(self.get_proxy_runtime_settings())
        clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
        if isinstance(clearance, dict):
            cf_cookies = str(clearance.get("cf_cookies") or "").strip()
            cf_clearance = str(clearance.get("cf_clearance") or "").strip()
            clearance["cf_cookies"] = ""
            clearance["cf_clearance"] = ""
            clearance["has_cf_cookies"] = bool(cf_cookies)
            clearance["has_cf_clearance"] = bool(cf_clearance)
        return runtime

    def get_third_party_apps_settings(self) -> dict[str, object]:
        return _normalize_third_party_apps_settings(self.data.get("third_party_apps"))

    def get_account_refresh_all_settings(self) -> dict[str, object]:
        return _normalize_account_refresh_all_settings(self.data.get("account_refresh_all"))

    def get_account_maintenance_loop_settings(self) -> dict[str, object]:
        return _normalize_account_maintenance_loop_settings(self.data.get("account_maintenance_loop"))

    def get_outlook_auto_recovery_settings(self) -> dict[str, object]:
        return _normalize_outlook_auto_recovery_settings(self.data.get("outlook_auto_recovery"))

    def get_scheduler_settings(self) -> dict[str, object]:
        return _normalize_scheduler_settings(self.data.get("scheduler"))

    def get_proactive_refresh_settings(self) -> dict[str, object]:
        return _normalize_proactive_refresh_settings(self.data.get("proactive_refresh"))

    def get_quota_refresh_schedule_settings(self) -> dict[str, object]:
        return _normalize_quota_refresh_schedule_settings(self.data.get("quota_refresh_schedule"))

    def get_quota_window_prime_settings(self) -> dict[str, object]:
        return _normalize_quota_window_prime_settings(self.data.get("quota_window_prime"))

    def get_webshare_cf_scan_settings(self) -> dict[str, object]:
        return _normalize_webshare_cf_scan_settings(self.data.get("webshare_cf_scan"))

    def get_account_warmup_settings(self) -> dict[str, object]:
        return _normalize_account_warmup_settings(self.data.get("account_warmup"))

    def get_workload_settings(self) -> dict[str, object]:
        return _normalize_workload_settings(self.data.get("workload"))

    def get_text_nurture_settings(self) -> dict[str, object]:
        return _normalize_text_nurture_settings(self.data.get("text_nurture"))

    @property
    def workload_mode(self) -> str:
        return str(self.get_workload_settings().get("mode") or "shadow")

    @property
    def text_queue_mode(self) -> str:
        return str(self.get_workload_settings().get("text_queue_mode") or "off")

    @property
    def workload_canary_token_hashes(self) -> list[str]:
        raw = self.get_workload_settings().get("canary_token_hashes") or []
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def get_panda_sync_settings(self) -> dict[str, object]:
        return _normalize_panda_sync_settings(self.data.get("panda_sync"))

    def get_public_panda_sync_settings(self) -> dict[str, object]:
        settings = self.get_panda_sync_settings()
        settings["auth_key"] = ""
        settings["has_auth_key"] = bool(str(self.get_panda_sync_settings().get("auth_key") or "").strip())
        return settings

    def update(self, data: dict[str, object]) -> dict[str, object]:
        next_data = dict(self.data)
        next_data.update(dict(data or {}))
        if "backup" in next_data:
            next_data["backup"] = _normalize_backup_settings(next_data.get("backup"))
        if "image_storage" in next_data:
            next_data["image_storage"] = _normalize_image_storage_settings(next_data.get("image_storage"))
            _validate_image_storage_settings(next_data["image_storage"])
        if "chat_completion_cache" in next_data:
            next_data["chat_completion_cache"] = _normalize_chat_completion_cache_settings(
                next_data.get("chat_completion_cache")
            )
        if "image_task_queue" in next_data:
            next_data["image_task_queue"] = _normalize_image_task_queue_settings(
                next_data.get("image_task_queue")
            )
        if "image_reference_assets" in next_data:
            next_data["image_reference_assets"] = _normalize_image_reference_assets_settings(
                next_data.get("image_reference_assets")
            )
        if "image_deadlock_guard" in next_data:
            next_data["image_deadlock_guard"] = _normalize_image_deadlock_guard_settings(
                next_data.get("image_deadlock_guard")
            )
        if "image_pipeline_watchdog" in next_data:
            next_data["image_pipeline_watchdog"] = _normalize_image_pipeline_watchdog_settings(
                next_data.get("image_pipeline_watchdog")
            )
        if "third_party_apps" in next_data:
            next_data["third_party_apps"] = _normalize_third_party_apps_settings(next_data.get("third_party_apps"))
        if "account_refresh_all" in next_data:
            next_data["account_refresh_all"] = _normalize_account_refresh_all_settings(next_data.get("account_refresh_all"))
        if "account_maintenance_loop" in next_data:
            next_data["account_maintenance_loop"] = _normalize_account_maintenance_loop_settings(next_data.get("account_maintenance_loop"))
        if "outlook_auto_recovery" in next_data:
            next_data["outlook_auto_recovery"] = _normalize_outlook_auto_recovery_settings(next_data.get("outlook_auto_recovery"))
        if "scheduler" in next_data:
            next_data["scheduler"] = _normalize_scheduler_settings(next_data.get("scheduler"))
        if "proactive_refresh" in next_data:
            next_data["proactive_refresh"] = _normalize_proactive_refresh_settings(next_data.get("proactive_refresh"))
        if "webshare_cf_scan" in next_data:
            next_data["webshare_cf_scan"] = _normalize_webshare_cf_scan_settings(next_data.get("webshare_cf_scan"))
        if "account_warmup" in next_data:
            next_data["account_warmup"] = _normalize_account_warmup_settings(next_data.get("account_warmup"))
        if "workload" in next_data:
            next_data["workload"] = _normalize_workload_settings(next_data.get("workload"))
        if "text_nurture" in next_data:
            next_data["text_nurture"] = _normalize_text_nurture_settings(next_data.get("text_nurture"))
        if "panda_sync" in next_data:
            next_data["panda_sync"] = _normalize_panda_sync_settings(next_data.get("panda_sync"))
        if "proxy_runtime" in next_data:
            incoming_runtime = next_data.get("proxy_runtime")
            if isinstance(incoming_runtime, dict):
                previous_clearance = self.get_proxy_runtime_settings().get("clearance")
                if isinstance(previous_clearance, dict):
                    incoming_runtime = dict(incoming_runtime)
                    incoming_runtime["_existing_cf_cookies"] = previous_clearance.get("cf_cookies")
                    incoming_runtime["_existing_cf_clearance"] = previous_clearance.get("cf_clearance")
            next_data["proxy_runtime"] = _normalize_proxy_runtime_settings(incoming_runtime)
        next_data.pop("backup_state", None)
        self.data = next_data
        self._save()
        return self.get()

    def get_backup_settings(self) -> dict[str, object]:
        return _normalize_backup_settings(self.data.get("backup"))

    def get_image_storage_settings(self) -> dict[str, object]:
        return _normalize_image_storage_settings(self.data.get("image_storage"))

    def get_chat_completion_cache_settings(self) -> dict[str, object]:
        return _normalize_chat_completion_cache_settings(self.data.get("chat_completion_cache"))

    def get_image_task_queue_settings(self) -> dict[str, object]:
        return _normalize_image_task_queue_settings(self.data.get("image_task_queue"))

    def get_image_pipeline_settings(self) -> dict[str, object]:
        return _normalize_image_pipeline_settings(self.data.get("image_pipeline"))

    def get_image_reference_assets_settings(self) -> dict[str, object]:
        return _normalize_image_reference_assets_settings(self.data.get("image_reference_assets"))

    def get_image_deadlock_guard_settings(self) -> dict[str, object]:
        return _normalize_image_deadlock_guard_settings(self.data.get("image_deadlock_guard"))

    def get_image_pipeline_watchdog_settings(self) -> dict[str, object]:
        return _normalize_image_pipeline_watchdog_settings(self.data.get("image_pipeline_watchdog"))

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend


def load_backup_state() -> dict[str, object]:
    return _normalize_backup_state(_read_json_object(BACKUP_STATE_FILE, name="backup_state.json"))


def save_backup_state(state: dict[str, object]) -> dict[str, object]:
    normalized = _normalize_backup_state(state)
    BACKUP_STATE_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


config = ConfigStore(CONFIG_FILE)
