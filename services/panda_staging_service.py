from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any

from services.account_refresh_all_service import (
    AccountRefreshAllOptions,
    account_refresh_all_service,
)
from services.account_service import AccountService, account_service
from services.config import config
from utils.helper import anonymize_token


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PandaStagingService:
    """本地新注册账号的“成熟度探测 + 水位上传”后台循环。

    目标：
    - 新号只进本地 SQLite 号池，不立刻打到 Panda；
    - 按正常/低水位/应急三档跨过 5 分钟死亡窗口做三次探活；
    - 任一确定死号立即从本地删除；
    - 成熟 ready 后按 Panda 水位动态缩短上传间隔，成功 ACK 后删本地。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_upload_at = 0.0
        self._status: dict[str, Any] = self._initial_status()

    def _settings(self) -> dict[str, Any]:
        return dict(config.get_panda_sync_settings())

    def _initial_status(self) -> dict[str, Any]:
        settings = self._settings()
        return {
            "state": "off" if not settings.get("staging_enabled") else "idle",
            "enabled": bool(settings.get("staging_enabled")),
            "started_at": None,
            "last_update_at": None,
            "next_run_at": None,
            "current": None,
            "last_probe": None,
            "last_upload": None,
            "totals": {
                "probed": 0,
                "probe_ok": 0,
                "probe_failed": 0,
                "deleted": 0,
                "ready": 0,
                "uploaded": 0,
                "upload_failed": 0,
                "queued": 0,
            },
            "settings": settings,
            "counts": {},
        }

    def start_background(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event = Event()
            self._thread = Thread(target=self._run, name="panda-staging-loop", daemon=True)
            self._thread.start()

    def stop_background(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread:
            thread.join(timeout=1)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _set_status(self, **updates: Any) -> None:
        with self._lock:
            self._status.update(updates)
            self._status["last_update_at"] = _iso_now()

    def _bump_totals(self, **updates: int) -> None:
        with self._lock:
            totals = dict(self._status.get("totals") or {})
            for key, value in updates.items():
                totals[key] = int(totals.get(key) or 0) + int(value or 0)
            self._status["totals"] = totals
            self._status["last_update_at"] = _iso_now()

    def _panda_options(self, settings: dict[str, Any], *, batch_size: int | None = None) -> AccountRefreshAllOptions:
        return AccountRefreshAllOptions.from_mapping({
            "panda_sync_requested": True,
            "panda_sync_enabled": True,
            "panda_sync_base_url": settings.get("base_url"),
            "panda_sync_auth_key": settings.get("auth_key"),
            "panda_sync_batch_size": batch_size or settings.get("batch_size") or settings.get("upload_max_batch") or 20,
            "panda_sync_timeout_seconds": settings.get("timeout_seconds"),
            "panda_sync_remove_local_on_success": True,
            "panda_sync_cooldown_seconds": settings.get("cooldown_seconds"),
        })

    def _remote_stats(self, settings: dict[str, Any], *, force: bool = False) -> dict[str, Any] | None:
        if not settings.get("enabled") or not settings.get("base_url") or not settings.get("auth_key"):
            return None
        options = self._panda_options(settings)
        return account_refresh_all_service._fetch_panda_stats(options, force=force)

    @staticmethod
    def _remote_current(stats: dict[str, Any] | None) -> int | None:
        if not stats:
            return None
        return int(stats.get("schedulable") or 0) or int(stats.get("active") or 0) or int(stats.get("total") or 0)

    def _supply_mode(self, settings: dict[str, Any], stats: dict[str, Any] | None = None) -> str:
        if not bool(settings.get("watermark_enabled", True)):
            return "normal"
        current = self._remote_current(stats)
        if current is None:
            return "normal"
        if current <= max(0, _as_int(settings.get("emergency_watermark"), 200)):
            return "emergency"
        if current <= max(0, _as_int(settings.get("low_watermark"), 500)):
            return "low"
        return "normal"

    def _mode_value(self, settings: dict[str, Any], mode: str, name: str, default: object) -> object:
        if mode == "emergency" and f"emergency_{name}" in settings:
            return settings.get(f"emergency_{name}")
        if mode == "low" and f"low_{name}" in settings:
            return settings.get(f"low_{name}")
        return settings.get(name, default)

    @staticmethod
    def _int_list(value: object, default: list[int]) -> list[int]:
        raw = value if isinstance(value, list) else default
        values = sorted({max(1, _as_int(item, 0)) for item in raw if _as_int(item, 0) > 0})
        return values or list(default)

    def _probe_schedule_minutes(self, settings: dict[str, Any] | None = None, mode: str | None = None) -> list[int]:
        settings = settings or self._settings()
        if mode is None:
            mode = self._supply_mode(settings, self._remote_stats(settings))
        if mode == "emergency":
            return self._int_list(settings.get("emergency_probe_schedule_minutes"), [5, 15, 45])
        if mode == "low":
            return self._int_list(settings.get("low_probe_schedule_minutes"), [10, 30, 90])
        raw_minutes = settings.get("probe_schedule_minutes")
        if isinstance(raw_minutes, list):
            return self._int_list(raw_minutes, [30, 120, 360])
        raw_hours = settings.get("probe_schedule_hours")
        hours = self._int_list(raw_hours, [1, 3, 6])
        return [max(1, hour * 60) for hour in hours]

    def _probe_cooldown_seconds(self, settings: dict[str, Any] | None = None, mode: str | None = None) -> float:
        settings = settings or self._settings()
        if mode is None:
            mode = self._supply_mode(settings, self._remote_stats(settings))
        return max(10.0, _as_float(self._mode_value(settings, mode, "probe_cooldown_sec", 120.0), 120.0))

    def _next_probe_at(
        self,
        account: dict[str, Any],
        count: int,
        *,
        after_now: bool = False,
        settings: dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> str | None:
        settings = settings or self._settings()
        if mode is None:
            mode = self._supply_mode(settings, self._remote_stats(settings))
        schedule = self._probe_schedule_minutes(settings, mode)
        if count >= len(schedule):
            return None
        created_at = _parse_time(account.get("created_at")) or _utc_now()
        due = created_at + timedelta(minutes=schedule[count])
        if after_now and due <= _utc_now():
            due = _utc_now() + timedelta(seconds=self._probe_cooldown_seconds(settings, mode))
        return due.isoformat()

    def stage_account(self, account_or_token: dict[str, Any] | str, *, source: str = "register") -> dict[str, Any] | None:
        """把新号标记为 staging。若关闭 staging，则直接标记 ready。"""
        account = account_or_token if isinstance(account_or_token, dict) else account_service.get_account(str(account_or_token))
        if not isinstance(account, dict):
            return None
        token = str(account.get("access_token") or account.get("accessToken") or "").strip()
        if not token:
            return None
        settings = self._settings()
        if not bool(settings.get("staging_enabled", True)) or not bool(settings.get("probe_before_upload", True)):
            return account_service.update_account(
                token,
                {
                    "panda_sync_state": "ready",
                    "panda_ready_at": _iso_now(),
                    "panda_probe_last_error": None,
                },
                quiet=True,
            )
        current = account_service.get_account(token) or account
        if str(current.get("panda_sync_state") or "").lower() in {"ready", "synced"}:
            return current
        updates = {
            "panda_sync_state": "staging",
            "panda_probe_count": int(current.get("panda_probe_count") or 0),
            "panda_probe_next_at": current.get("panda_probe_next_at")
            or self._next_probe_at(current, int(current.get("panda_probe_count") or 0), after_now=True),
            "panda_probe_last_error": None,
            "panda_stage_source": source,
        }
        return account_service.update_account(token, updates, quiet=True)

    def _counts(self) -> dict[str, int]:
        items = account_service.list_accounts()
        return {
            "staging": sum(1 for item in items if str(item.get("panda_sync_state") or "").lower() == "staging"),
            "ready": sum(1 for item in items if str(item.get("panda_sync_state") or "").lower() == "ready"),
            "synced": sum(1 for item in items if str(item.get("panda_sync_state") or "").lower() == "synced"),
            "available_total": sum(1 for item in items if AccountService._is_image_account_available(item)),
        }

    def _due_probe_tokens(self) -> list[str]:
        now = _utc_now()
        due: list[tuple[datetime, str]] = []
        settings = self._settings()
        stats = self._remote_stats(settings)
        mode = self._supply_mode(settings, stats)
        schedule_len = len(self._probe_schedule_minutes(settings, mode))
        for account in account_service.list_accounts():
            if str(account.get("panda_sync_state") or "").lower() != "staging":
                continue
            if str(account.get("status") or "") == "禁用":
                continue
            count = int(account.get("panda_probe_count") or 0)
            if count >= schedule_len:
                token = str(account.get("access_token") or "").strip()
                if token:
                    account_service.update_account(
                        token,
                        {"panda_sync_state": "ready", "panda_ready_at": _iso_now(), "panda_probe_next_at": None},
                        quiet=True,
                    )
                continue
            stored_due_at = _parse_time(account.get("panda_probe_next_at"))
            computed_due_at = _parse_time(self._next_probe_at(account, count, settings=settings, mode=mode))
            if account.get("panda_probe_last_error"):
                due_at = stored_due_at or computed_due_at
            elif stored_due_at and computed_due_at:
                due_at = min(stored_due_at, computed_due_at)
            else:
                due_at = stored_due_at or computed_due_at
            token = str(account.get("access_token") or "").strip()
            if token and due_at is not None and due_at <= now:
                due.append((due_at, token))
        due.sort(key=lambda item: item[0])
        limit = max(1, _as_int(self._mode_value(settings, mode, "probe_batch_limit", 100), 100))
        return [token for _, token in due[:limit]]

    def _is_invalid_error(self, error: object) -> bool:
        return account_refresh_all_service._is_invalid_token_error(str(error or ""))

    def _is_transient_error(self, error: object) -> bool:
        return account_refresh_all_service._is_transient_refresh_error(str(error or ""))

    def _probe_one(self, token: str) -> dict[str, Any]:
        started = _iso_now()
        before = account_service.get_account(token)
        if not before:
            return {"token": token, "status": "missing", "deleted": 0}
        try:
            account = account_service.fetch_remote_info(token, "panda_staging_probe", defer_invalid_removal=False)
        except Exception as exc:
            error = str(exc)
            if self._is_invalid_error(error):
                removed = int(account_service.delete_accounts([token], include_items=False).get("removed") or 0)
                return {"token": token, "status": "deleted", "deleted": removed, "error": error[:300]}
            next_at = (
                _utc_now() + timedelta(seconds=max(60.0, _as_float(self._settings().get("probe_transient_backoff_sec"), 1800.0)))
            ).isoformat()
            account_service.update_account(
                token,
                {
                    "panda_probe_last_at": started,
                    "panda_probe_last_error": error[:500],
                    "panda_probe_next_at": next_at,
                },
                quiet=True,
            )
            return {"token": token, "status": "failed", "deleted": 0, "error": error[:300]}

        if not account:
            return {"token": token, "status": "missing", "deleted": 0}
        resolved = str(account.get("access_token") or token)
        available = AccountService._is_image_account_available(account)
        count = int(account.get("panda_probe_count") or 0) + (1 if available else 0)
        history = list(account.get("panda_probe_history") or [])
        history.append({
            "at": started,
            "ok": bool(available),
            "quota": int(account.get("quota") or 0),
            "status": str(account.get("status") or ""),
        })
        settings = self._settings()
        mode = self._supply_mode(settings, self._remote_stats(settings))
        schedule_len = len(self._probe_schedule_minutes(settings, mode))
        updates: dict[str, Any] = {
            "panda_probe_last_at": started,
            "panda_probe_last_error": None if available else "not_available_after_probe",
            "panda_probe_history": history[-10:],
        }
        if available:
            updates["panda_probe_count"] = count
            if count >= schedule_len:
                updates.update({
                    "panda_sync_state": "ready",
                    "panda_ready_at": _iso_now(),
                    "panda_probe_next_at": None,
                })
            else:
                updates["panda_probe_next_at"] = self._next_probe_at(account, count, after_now=True, settings=settings, mode=mode)
        else:
            updates["panda_probe_next_at"] = (
                _utc_now() + timedelta(seconds=max(60.0, _as_float(self._settings().get("probe_transient_backoff_sec"), 1800.0)))
            ).isoformat()
        account_service.update_account(resolved, updates, quiet=True)
        return {
            "token": resolved,
            "status": "ready" if available and count >= schedule_len else ("ok" if available else "not_available"),
            "deleted": 0,
            "available": available,
        }

    def _run_due_probes(self) -> dict[str, int]:
        tokens = self._due_probe_tokens()
        if not tokens:
            return {"probed": 0, "ok": 0, "failed": 0, "deleted": 0, "ready": 0}
        self._set_status(state="probing", current={"tokens": [anonymize_token(token) for token in tokens]})
        settings = self._settings()
        mode = self._supply_mode(settings, self._remote_stats(settings))
        concurrency = min(max(1, _as_int(self._mode_value(settings, mode, "probe_concurrency", 4), 4)), len(tokens), 8)
        summary = {"probed": 0, "ok": 0, "failed": 0, "deleted": 0, "ready": 0}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(self._probe_one, token): token for token in tokens}
            for future in as_completed(futures):
                result = future.result()
                summary["probed"] += 1
                status = str(result.get("status") or "")
                if status in {"ok", "ready"}:
                    summary["ok"] += 1
                else:
                    summary["failed"] += 1
                if status == "ready":
                    summary["ready"] += 1
                summary["deleted"] += int(result.get("deleted") or 0)
                safe_result = dict(result)
                if safe_result.get("token"):
                    safe_result["token"] = anonymize_token(str(safe_result.get("token") or ""))
                self._set_status(last_probe=safe_result)
        self._bump_totals(
            probed=summary["probed"],
            probe_ok=summary["ok"],
            probe_failed=summary["failed"],
            deleted=summary["deleted"],
            ready=summary["ready"],
        )
        return summary

    def _ready_accounts(self) -> list[dict[str, Any]]:
        ready: list[tuple[datetime, dict[str, Any]]] = []
        for account in account_service.list_accounts():
            if not account_refresh_all_service._is_panda_sync_ready(account):
                continue
            ready_at = _parse_time(account.get("panda_ready_at")) or _parse_time(account.get("created_at")) or _utc_now()
            ready.append((ready_at, account))
        ready.sort(key=lambda item: item[0])
        return [account for _, account in ready]

    def _upload_interval_seconds(self, settings: dict[str, Any], mode: str) -> int:
        public_min_interval = max(0, _as_int(settings.get("public_import_min_interval_sec"), 0))
        if mode == "emergency":
            return max(public_min_interval, _as_int(settings.get("emergency_sync_interval_sec"), 30))
        if mode == "low":
            return max(public_min_interval, _as_int(settings.get("low_sync_interval_sec"), 60))
        return max(60, _as_int(settings.get("sync_interval_minutes"), 30) * 60)

    def _upload_batch_limit(self, settings: dict[str, Any], mode: str) -> int:
        if mode == "emergency":
            configured = max(1, _as_int(settings.get("emergency_upload_max_batch"), 20))
        elif mode == "low":
            configured = max(1, _as_int(settings.get("low_upload_max_batch"), 20))
        else:
            configured = max(1, _as_int(settings.get("upload_max_batch"), 20))
        public_max_batch = max(1, _as_int(settings.get("public_import_max_batch_size"), configured))
        return min(configured, public_max_batch)

    def _upload_ready_accounts(self) -> dict[str, int]:
        settings = self._settings()
        if not settings.get("enabled") or not settings.get("base_url") or not settings.get("auth_key"):
            return {"synced": 0, "failed": 0, "queued": 0}
        stats = self._remote_stats(settings)
        mode = self._supply_mode(settings, stats)
        current = self._remote_current(stats)
        if bool(settings.get("watermark_enabled", True)) and current is not None:
            high = max(1, _as_int(settings.get("high_watermark"), 1500))
            low = max(0, _as_int(settings.get("low_watermark"), 500))
            if current >= high or current > low:
                return {"synced": 0, "failed": 0, "queued": 0}
        interval = self._upload_interval_seconds(settings, mode)
        if self._last_upload_at and time.monotonic() - self._last_upload_at < interval:
            return {"synced": 0, "failed": 0, "queued": 0}
        ready = self._ready_accounts()
        if not ready:
            return {"synced": 0, "failed": 0, "queued": 0}
        max_batch = self._upload_batch_limit(settings, mode)
        if bool(settings.get("watermark_enabled", True)) and current is not None:
            high = max(1, _as_int(settings.get("high_watermark"), 1500))
            max_batch = min(max_batch, max(0, high - current))
        if max_batch <= 0:
            return {"synced": 0, "failed": 0, "queued": 0}
        batch = ready[:max_batch]
        options = self._panda_options(settings, batch_size=max_batch)
        self._set_status(state="uploading", current={"ready": len(ready), "batch": len(batch), "mode": mode, "remote_current": current})
        synced, failed, queued = account_refresh_all_service._queue_or_sync_accounts_to_panda(batch, options)
        self._last_upload_at = time.monotonic()
        if failed and not synced:
            # 失败不无限排队，只把本地 ready 保留给下一轮；记录错误状态即可。
            for account in batch:
                token = str(account.get("access_token") or "").strip()
                if token:
                    account_service.update_account(
                        token,
                        {"panda_probe_last_error": "panda_upload_failed", "panda_probe_next_at": None},
                        quiet=True,
                    )
        summary = {"synced": synced, "failed": failed, "queued": queued}
        self._set_status(last_upload={**summary, "at": _iso_now(), "attempted": len(batch), "mode": mode, "remote_current": current})
        self._bump_totals(uploaded=synced, upload_failed=failed, queued=queued)
        return summary


    def _should_prioritize_ready_upload(self, settings: dict[str, Any], stats: dict[str, Any] | None) -> bool:
        """Panda 低水位且本地 ready 已有积压时，优先补 Panda，避免大批探活拖住上传。"""
        mode = self._supply_mode(settings, stats)
        if mode not in {"low", "emergency"}:
            return False
        counts = self._counts()
        ready_count = int(counts.get("ready") or 0)
        return ready_count >= self._upload_batch_limit(settings, mode)

    def _sleep_until_next(self, settings: dict[str, Any]) -> None:
        mode = self._supply_mode(settings, self._remote_stats(settings))
        cooldown = self._probe_cooldown_seconds(settings, mode)
        deadline = time.time() + cooldown
        self._set_status(state="idle", next_run_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat())
        self._stop_event.wait(cooldown)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = self._settings()
            self._set_status(
                enabled=bool(settings.get("staging_enabled")),
                settings=settings,
                counts=self._counts(),
                started_at=self._status.get("started_at") or _iso_now(),
            )
            if not settings.get("staging_enabled"):
                self._set_status(state="off", next_run_at=None)
                if self._stop_event.wait(5.0):
                    return
                continue
            try:
                self._upload_ready_accounts()
                stats = self._remote_stats(settings)
                if self._should_prioritize_ready_upload(settings, stats):
                    self._set_status(
                        state="idle",
                        current={"skipped_probe_reason": "ready_backlog_prioritized_for_panda_upload"},
                    )
                else:
                    self._run_due_probes()
                    self._upload_ready_accounts()
            except Exception as exc:
                self._set_status(state="error", current={"error": str(exc)[:500]})
            if self._stop_event.is_set():
                return
            self._sleep_until_next(settings)


panda_staging_service = PandaStagingService()
