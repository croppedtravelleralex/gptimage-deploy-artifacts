from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any

from services.account_refresh_all_service import (
    AccountRefreshAllOptions,
    account_refresh_all_service,
)
from services.account_service import AccountService, account_service
from services.config import DATA_DIR, config


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AccountMaintenanceLoopService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._cursor_file = DATA_DIR / ".account_maintenance_cursor"
        self._status: dict[str, Any] = self._initial_status()
        self._startup_delay_done = False

    def _initial_status(self) -> dict[str, Any]:
        settings = config.get_account_maintenance_loop_settings()
        return {
            "state": "off" if not settings.get("enabled") else "idle",
            "enabled": bool(settings.get("enabled")),
            "started_at": None,
            "last_update_at": None,
            "next_run_at": None,
            "pause_reason": "",
            "mode": "normal",
            "cursor": self._load_cursor(),
            "current_batch_id": "",
            "current_batch": None,
            "last_batch": None,
            "totals": {
                "batches": 0,
                "processed": 0,
                "refreshed": 0,
                "available": 0,
                "failed": 0,
                "removed": 0,
            },
            "recent": [],
            "resource": {},
            "estimate": {},
            "settings": settings,
        }

    def _load_cursor(self) -> int:
        try:
            return max(0, int(self._cursor_file.read_text(encoding="utf-8").strip() or "0"))
        except Exception:
            return 0

    def _save_cursor(self, cursor: int) -> None:
        cursor = max(0, int(cursor or 0))
        try:
            self._cursor_file.parent.mkdir(parents=True, exist_ok=True)
            self._cursor_file.write_text(str(cursor), encoding="utf-8")
        except Exception:
            pass
        with self._lock:
            self._status["cursor"] = cursor

    def start_background(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event = Event()
            self._thread = Thread(target=self._run, name="account-maintenance-loop", daemon=True)
            self._thread.start()

    def stop_background(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread:
            thread.join(timeout=1)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = config.get_account_maintenance_loop_settings()
        merged = {**current, **dict(updates or {})}
        saved = config.update({"account_maintenance_loop": merged})
        settings = saved.get("account_maintenance_loop") if isinstance(saved, dict) else config.get_account_maintenance_loop_settings()
        with self._lock:
            self._status["settings"] = settings
            self._status["enabled"] = bool(settings.get("enabled"))
            if not settings.get("enabled"):
                self._status["state"] = "off"
                self._status["next_run_at"] = None
                self._status["pause_reason"] = "disabled"
            elif self._status.get("state") == "off":
                self._status["state"] = "idle"
                self._status["pause_reason"] = ""
            self._status["last_update_at"] = _iso_now()
        return self.get_status()

    def _set_status(self, **updates: Any) -> None:
        with self._lock:
            self._status.update(updates)
            self._status["last_update_at"] = _iso_now()

    def _append_recent(self, item: dict[str, Any]) -> None:
        with self._lock:
            recent = list(self._status.get("recent") or [])
            recent.append(item)
            self._status["recent"] = recent[-20:]

    def _cooldown(self, seconds: float, state: str = "cooldown") -> None:
        deadline = time.time() + max(1.0, seconds)
        while not self._stop_event.is_set():
            settings = config.get_account_maintenance_loop_settings()
            if not settings.get("enabled"):
                self._set_status(state="off", enabled=False, next_run_at=None, pause_reason="disabled")
                return
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                return
            self._set_status(state=state, enabled=True, next_run_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat())
            if self._stop_event.wait(min(remaining, 5.0)):
                return

    def _resource_mode(self, settings: dict[str, object]) -> tuple[str, str, dict[str, Any]]:
        options = AccountRefreshAllOptions.from_mapping(settings)
        ok, reason, resource = account_refresh_all_service._resource_ok(options)
        inflight = account_service.get_total_image_inflight()
        resource["image_inflight"] = inflight
        hard_reasons = [reason] if not ok and reason else []
        try:
            from services.image_deadlock_guard_service import image_deadlock_guard_service

            guard_status = image_deadlock_guard_service.status()
            resource["image_deadlock_guard"] = guard_status
            if guard_status.get("tripped"):
                return "pause", str(guard_status.get("reason") or "image deadlock guard tripped"), resource
        except Exception as exc:
            resource["image_deadlock_guard_error"] = str(exc)
        pause_threshold = _as_int(settings.get("pause_when_image_inflight"), 0)
        if pause_threshold > 0 and inflight >= pause_threshold:
            hard_reasons.append(f"image inflight {inflight} >= pause {pause_threshold}")
        if hard_reasons and bool(settings.get("resource_pause_enabled")):
            return "pause", "; ".join(hard_reasons), resource

        slow_reasons: list[str] = list(hard_reasons)
        available_mb = resource.get("available_memory_mb")
        slow_memory = _as_int(settings.get("slow_min_available_memory_mb"), 0)
        if (
            slow_memory > 0
            and isinstance(available_mb, (int, float))
            and available_mb < slow_memory
        ):
            slow_reasons.append(f"available memory {available_mb}MB < slow {slow_memory}MB")
        slow_inflight = _as_int(settings.get("slow_when_image_inflight"), 0)
        if slow_inflight > 0 and inflight >= slow_inflight:
            slow_reasons.append(f"image inflight {inflight} >= slow {slow_inflight}")
        if slow_reasons:
            return "slow", "; ".join(slow_reasons), resource
        return "normal", "", resource

    def _resource_pause_reason(self, settings: dict[str, object]) -> tuple[str, dict[str, Any]]:
        mode, reason, resource = self._resource_mode(settings)
        return (reason if mode == "pause" else ""), resource

    def _select_tokens(self, limit: int, settings: dict[str, object]) -> list[str]:
        options = AccountRefreshAllOptions.from_mapping({
            "stale_after_hours": settings.get("stale_after_hours"),
            "include_recent": settings.get("include_recent"),
            "delete_invalid": settings.get("delete_invalid"),
            "delete_after_failures": settings.get("delete_after_failures"),
            "expired_grace_hours": settings.get("expired_grace_hours"),
        })
        tokens, _skipped = account_refresh_all_service._build_token_queue(options)
        if not tokens:
            return []
        cursor = self._load_cursor() % len(tokens)
        rotated = tokens[cursor:] + tokens[:cursor]
        selected = rotated[:limit]
        self._save_cursor((cursor + len(selected)) % len(tokens))
        return selected

    def _estimate_cost(self, settings: dict[str, object], mode: str) -> dict[str, Any]:
        """估算当前维护策略下单批/全池探活成本，给 Panda 调参用。"""
        try:
            total_accounts = len(account_service.list_accounts())
            limit = min(500, _as_int(settings.get("batch_limit"), 10))
            if mode == "slow":
                limit = min(limit, _as_int(settings.get("slow_batch_limit"), 3))
            concurrency = max(1, min(_as_int(settings.get("concurrency"), 1), 8))
            batch_size = max(1, _as_int(settings.get("batch_size"), 10))
            delay_account = _as_float(settings.get("delay_between_accounts_sec"), 0.0)
            if mode == "slow":
                delay_account = max(delay_account, _as_float(settings.get("slow_delay_between_accounts_sec"), 8.0))
            delay_batch = _as_float(settings.get("delay_between_batches_sec"), 0.0)
            last = self._status.get("last_batch") if isinstance(self._status.get("last_batch"), dict) else {}
            measured_sec_per_account = None
            if isinstance(last, dict) and int(last.get("processed") or 0) > 0:
                started = datetime.fromisoformat(str(last.get("started_at")).replace("Z", "+00:00"))
                finished = datetime.fromisoformat(str(last.get("finished_at")).replace("Z", "+00:00"))
                measured_sec_per_account = max(
                    0.0,
                    (finished - started).total_seconds() / max(1, int(last.get("processed") or 1)),
                )
            # 没有历史样本时只估算节流下限；有历史样本时用历史单号耗时估算更贴近真实。
            base_sec_per_account = measured_sec_per_account if measured_sec_per_account is not None else max(1.0, delay_account)
            batch_accounts = min(limit, total_accounts)
            waves = (batch_accounts + concurrency - 1) // concurrency
            batch_groups = (batch_accounts + batch_size - 1) // batch_size
            estimated_batch_seconds = waves * base_sec_per_account + max(0, batch_groups - 1) * delay_batch
            full_batches = (total_accounts + max(1, limit) - 1) // max(1, limit)
            return {
                "mode": mode,
                "total_accounts": total_accounts,
                "batch_limit": limit,
                "concurrency": concurrency,
                "batch_size": batch_size,
                "delay_between_accounts_sec": delay_account,
                "delay_between_batches_sec": delay_batch,
                "measured_sec_per_account": round(measured_sec_per_account, 3) if measured_sec_per_account is not None else None,
                "estimated_batch_seconds": round(estimated_batch_seconds, 1),
                "estimated_full_cycle_seconds": round(estimated_batch_seconds * max(1, full_batches), 1),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _build_batch_options(self, settings: dict[str, object], tokens: list[str], mode: str) -> AccountRefreshAllOptions:
        batch_size = min(_as_int(settings.get("batch_size"), 10), max(1, len(tokens)))
        concurrency = min(_as_int(settings.get("concurrency"), 1), max(1, len(tokens)), 8)
        delay_between_accounts = settings.get("delay_between_accounts_sec")
        if mode == "slow":
            batch_size = min(batch_size, _as_int(settings.get("slow_batch_limit"), 3), max(1, len(tokens)))
            delay_between_accounts = max(
                _as_float(delay_between_accounts, 0.0),
                _as_float(settings.get("slow_delay_between_accounts_sec"), 8.0),
            )
        return AccountRefreshAllOptions.from_mapping({
            "concurrency": concurrency,
            "max_concurrency": concurrency,
            "batch_size": batch_size,
            "delay_between_accounts_sec": delay_between_accounts,
            "delay_between_batches_sec": settings.get("delay_between_batches_sec"),
            "stale_after_hours": settings.get("stale_after_hours"),
            "include_recent": True,
            "min_available_memory_mb": settings.get("min_available_memory_mb"),
            "max_load_1m": settings.get("max_load_1m"),
            "resource_pause_enabled": settings.get("resource_pause_enabled"),
            "resource_check_interval_sec": settings.get("resource_check_interval_sec"),
            "limit": len(tokens),
            "delete_invalid": settings.get("delete_invalid"),
            "delete_after_failures": settings.get("delete_after_failures"),
            "expired_grace_hours": settings.get("expired_grace_hours"),
            "tokens": tokens,
        })

    def _run_batch(self, settings: dict[str, object], mode: str = "normal") -> None:
        limit = min(500, _as_int(settings.get("batch_limit"), 10))
        if mode == "slow":
            limit = min(limit, _as_int(settings.get("slow_batch_limit"), 3))
        tokens = self._select_tokens(limit, settings)
        if not tokens:
            self._set_status(state="idle", mode=mode, pause_reason="", current_batch=None, next_run_at=None)
            self._cooldown(_as_float(settings.get("cooldown_sec"), 30.0), state="idle")
            return

        batch_id = f"maint-{int(time.time())}"
        started = _iso_now()
        options = self._build_batch_options(settings, tokens, mode)
        self._set_status(
            state="running_batch",
            mode=mode,
            pause_reason="",
            current_batch_id=batch_id,
            current_batch={
                "batch_id": batch_id,
                "started_at": started,
                "mode": mode,
                "total": len(tokens),
                "status": None,
            },
            next_run_at=None,
        )
        try:
            status = account_refresh_all_service.start(options)
        except RuntimeError as exc:
            self._set_status(state="manual_paused", pause_reason=str(exc))
            self._cooldown(_as_float(settings.get("cooldown_sec"), 30.0), state="manual_paused")
            return

        while not self._stop_event.is_set():
            status = account_refresh_all_service.get_status()
            with self._lock:
                current = dict(self._status.get("current_batch") or {})
                current["status"] = status
                self._status["current_batch"] = current
                self._status["last_update_at"] = _iso_now()
            if not status.get("running"):
                break
            if self._stop_event.wait(2.0):
                account_refresh_all_service.stop()
                break

        if self._stop_event.is_set():
            return

        status = account_refresh_all_service.get_status()
        finished = _iso_now()
        summary = {
            "batch_id": batch_id,
            "started_at": started,
            "finished_at": finished,
            "total": status.get("total", len(tokens)),
            "processed": status.get("processed", 0),
            "refreshed": status.get("refreshed", 0),
            "available": status.get("available", 0),
            "failed": status.get("failed", 0),
            "removed": status.get("removed", 0),
            "state": status.get("state"),
        }
        with self._lock:
            totals = dict(self._status.get("totals") or {})
            totals["batches"] = int(totals.get("batches") or 0) + 1
            for key in ("processed", "refreshed", "available", "failed", "removed"):
                totals[key] = int(totals.get(key) or 0) + int(summary.get(key) or 0)
            self._status["totals"] = totals
            self._status["last_batch"] = summary
            self._status["current_batch"] = None
            self._status["last_update_at"] = finished
        self._append_recent(summary)
        cooldown = _as_float(settings.get("cooldown_sec"), 30.0)
        if mode == "slow":
            cooldown = max(cooldown, _as_float(settings.get("slow_cooldown_sec"), 10.0))
        self._cooldown(cooldown)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = config.get_account_maintenance_loop_settings()
            with self._lock:
                self._status["settings"] = settings
                self._status["enabled"] = bool(settings.get("enabled"))
            if not settings.get("enabled"):
                self._set_status(state="off", pause_reason="disabled", next_run_at=None)
                if self._stop_event.wait(5.0):
                    return
                continue

            if not self._status.get("started_at"):
                self._set_status(started_at=_iso_now())
            if not self._startup_delay_done:
                self._startup_delay_done = True
                startup_delay = _as_float(settings.get("startup_delay_sec"), 60.0)
                if startup_delay > 0:
                    self._cooldown(startup_delay, state="idle")
                    continue

            mode, reason, resource = self._resource_mode(settings)
            with self._lock:
                self._status["resource"] = resource
                self._status["mode"] = mode
                self._status["estimate"] = self._estimate_cost(settings, mode)
            if reason:
                if mode == "pause":
                    self._set_status(state="resource_paused", mode=mode, pause_reason=reason, current_batch=None, next_run_at=None)
                    if self._stop_event.wait(_as_float(settings.get("resource_check_interval_sec"), 10.0)):
                        return
                    continue
                self._set_status(state="resource_slow", mode=mode, pause_reason=reason, next_run_at=None)
            else:
                self._set_status(mode=mode, pause_reason="")

            self._run_batch(settings, mode)


account_maintenance_loop_service = AccountMaintenanceLoopService()
