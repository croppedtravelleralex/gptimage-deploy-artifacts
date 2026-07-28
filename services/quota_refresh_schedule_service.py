"""Binding 四段日历额度刷新（替代 60s 全池 tick）。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from services.account_identity import binding_key_for_account
from services.account_service import account_service
from services.config import config
from services.image_quota_refresh_service import image_quota_refresh_service
from services.quota_binding_calendar import (
    account_key_for_account,
    compute_account_phase_slot,
    compute_next_account_slot,
    engine_info,
    evaluate_schedule_pick,
    local_date_for_account,
    resolve_tz_for_account,
)
from utils.log import logger


def _calendar_state(account: dict[str, Any]) -> dict[str, Any]:
    raw = account.get("quota_calendar_refresh")
    return dict(raw) if isinstance(raw, dict) else {}


def _phases_done_for_date(state: dict[str, Any], local_date: str) -> list[int]:
    if str(state.get("local_date") or "") != local_date:
        return []
    raw = state.get("phases_done")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


class QuotaRefreshScheduleService:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._binding_last_refresh_ts: dict[str, float] = {}
        self._status: dict[str, Any] = {
            "state": "off",
            "enabled": False,
            "last_tick_at": None,
            "last_refresh_at": None,
            "last_account": None,
            "totals": {"ticks": 0, "refreshed": 0, "skipped": 0, "errors": 0, "manual": 0},
        }

    def _now_utc(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="quota-refresh-schedule", daemon=True)
        self._thread.start()
        logger.info({"event": "quota_refresh_schedule_started"})

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        settings = config.get_quota_refresh_schedule_settings()
        with self._lock:
            status = dict(self._status)
            status["settings"] = settings
            status["enabled"] = bool(settings.get("enabled"))
            status["calendar_engine"] = engine_info()
            status["queue"] = image_quota_refresh_service.snapshot()
            return status

    def schedule_manual_refresh(self, token: str) -> None:
        image_quota_refresh_service.schedule_refresh(token)
        with self._lock:
            self._status["totals"]["manual"] = int(self._status["totals"].get("manual") or 0) + 1

    def preview_slots(self, *, local_date: str | None = None) -> list[dict[str, Any]]:
        settings = config.get_quota_refresh_schedule_settings()
        now = self._now_utc()
        default_tz = str(settings.get("default_timezone") or "Asia/Singapore")
        rows: list[dict[str, Any]] = []
        for account in account_service.list_accounts():
            if not isinstance(account, dict):
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            tz_name = resolve_tz_for_account(
                account,
                default_tz=default_tz,
                timezone_from_egress=bool(settings.get("timezone_from_egress")),
            )
            day = local_date
            if not day:
                day = local_date_for_account(
                    now,
                    account,
                    default_tz=default_tz,
                    timezone_from_egress=bool(settings.get("timezone_from_egress")),
                ).isoformat()
            binding = binding_key_for_account(account)
            account_key = account_key_for_account(account)
            for phase in range(4):
                slot = compute_account_phase_slot(
                    account_key=account_key,
                    binding_key=binding,
                    local_day=datetime.fromisoformat(day).date(),
                    phase_index=phase,
                    tz_name=tz_name,
                    jitter_min_minutes=int(settings.get("account_jitter_min_minutes") or 30),
                    jitter_max_minutes=int(settings.get("account_jitter_max_minutes") or 60),
                )
                rows.append(
                    {
                        "email": account.get("email"),
                        "binding_key": binding,
                        "phase_index": phase,
                        "account_slot_utc": slot["account_slot_utc"].isoformat(),
                        "binding_slot_utc": slot["binding_slot_utc"].isoformat(),
                    }
                )
        return rows

    def _binding_gap_ok(self, binding_key: str, gap_hours: float) -> bool:
        last = self._binding_last_refresh_ts.get(binding_key, 0.0)
        return (self._clock() - last) >= gap_hours * 3600.0

    def _mark_binding_refresh(self, binding_key: str) -> None:
        self._binding_last_refresh_ts[binding_key] = self._clock()

    def _refresh_account(self, token: str, account: dict[str, Any], *, event: str, phase_index: int | None = None) -> bool:
        try:
            from services.image_pipeline import schedule_trace as _trace

            if _trace.enabled():
                run = _trace.begin(f"quota-refresh-{token[:12]}", str(account.get("email") or ""))
                token_ctx = _trace.bind(run)
                _trace.emit("quota_refresh_start")
            else:
                token_ctx = None
                run = None
        except Exception:
            token_ctx = None
            run = None
        try:
            result = account_service.fetch_remote_info(token, event=event)
            if result is None:
                return False
            settings = config.get_quota_refresh_schedule_settings()
            now = self._now_utc()
            updates: dict[str, Any] = {
                "last_quota_refresh_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
            next_slot = compute_next_account_slot(account, now_utc=now, settings=settings)
            if next_slot is not None:
                updates["next_quota_refresh_at"] = next_slot.isoformat()
            if phase_index is not None:
                default_tz = str(settings.get("default_timezone") or "Asia/Singapore")
                local_date = local_date_for_account(
                    now,
                    account,
                    default_tz=default_tz,
                    timezone_from_egress=bool(settings.get("timezone_from_egress")),
                ).isoformat()
                state = _calendar_state(account)
                phases = _phases_done_for_date(state, local_date)
                if phase_index not in phases:
                    phases.append(phase_index)
                updates["quota_calendar_refresh"] = {
                    "local_date": local_date,
                    "phases_done": sorted(phases),
                }
            account_service.update_account(token, updates, quiet=True)
            return True
        except Exception as exc:
            with self._lock:
                self._status["totals"]["errors"] = int(self._status["totals"].get("errors") or 0) + 1
                self._status["last_reason"] = str(exc)[:240]
            logger.warning({"event": "quota_refresh_schedule_fail", "token_prefix": token[:16], "error": str(exc)[:240]})
            return False
        finally:
            try:
                if run is not None:
                    from services.image_pipeline import schedule_trace as _trace

                    _trace.emit("quota_refresh_end")
                    if token_ctx is not None:
                        _trace.unbind(token_ctx)
                    _trace.pop(f"quota-refresh-{token[:12]}")
            except Exception:
                pass

    def _pick_due_calendar_account(self, settings: dict[str, object]) -> tuple[str, dict[str, Any], int] | None:
        now = self._now_utc()
        now_unix = int(now.timestamp())
        default_tz = str(settings.get("default_timezone") or "Asia/Singapore")
        gap_hours = max(0.0, float(settings.get("binding_min_gap_hours") or 2.0))
        tz_from_egress = bool(settings.get("timezone_from_egress"))

        accounts_raw = account_service.list_accounts()
        indexed: list[dict[str, Any]] = []
        token_by_index: dict[int, str] = {}
        account_by_index: dict[int, dict[str, Any]] = {}

        for index, account in enumerate(accounts_raw):
            if not isinstance(account, dict):
                continue
            if not account_service._is_image_account_schedulable(account):
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            tz_name = resolve_tz_for_account(
                account,
                default_tz=default_tz,
                timezone_from_egress=tz_from_egress,
            )
            local_date = local_date_for_account(
                now,
                account,
                default_tz=default_tz,
                timezone_from_egress=tz_from_egress,
            ).isoformat()
            indexed.append(
                {
                    "index": index,
                    "account_key": account_key_for_account(account),
                    "binding_key": binding_key_for_account(account),
                    "tz_name": tz_name,
                    "local_date": local_date,
                    "phases_done": _phases_done_for_date(_calendar_state(account), local_date),
                    "schedulable": True,
                }
            )
            token_by_index[index] = token
            account_by_index[index] = account

        payload = {
            "now_unix": now_unix,
            "binding_gap_sec": gap_hours * 3600.0,
            "binding_last_refresh_unix": dict(self._binding_last_refresh_ts),
            "jitter_min_minutes": int(settings.get("account_jitter_min_minutes") or 30),
            "jitter_max_minutes": int(settings.get("account_jitter_max_minutes") or 60),
            "accounts": indexed,
        }
        result = evaluate_schedule_pick(payload)
        picked = result.get("picked") if isinstance(result, dict) else None
        if not isinstance(picked, dict):
            return None
        idx = int(picked.get("index", -1))
        phase = int(picked.get("phase_index", -1))
        token = token_by_index.get(idx)
        account = account_by_index.get(idx)
        if not token or account is None or phase < 0:
            return None
        return token, account, phase

    def _maybe_pre_restore_refresh(self, settings: dict[str, object]) -> bool:
        minutes = float(settings.get("pre_restore_refresh_minutes") or 0)
        if minutes <= 0:
            return False
        now = self._now_utc()
        for account in account_service.list_accounts():
            if not isinstance(account, dict):
                continue
            if not account_service._is_image_account_schedulable(account):
                continue
            try:
                quota = int(account.get("quota") or 0)
            except (TypeError, ValueError):
                quota = 0
            if quota > 0:
                continue
            restore_raw = account.get("restore_at")
            if not restore_raw:
                continue
            try:
                restore_at = datetime.fromisoformat(str(restore_raw).replace("Z", "+00:00"))
                if restore_at.tzinfo is None:
                    restore_at = restore_at.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            delta_min = (restore_at - now).total_seconds() / 60.0
            if 0 < delta_min <= minutes:
                token = str(account.get("access_token") or "").strip()
                if token and self._refresh_account(token, account, event="quota_refresh:pre_restore"):
                    return True
        return False

    def _tick(self) -> None:
        settings = config.get_quota_refresh_schedule_settings()
        if not bool(settings.get("enabled")):
            return
        with self._lock:
            self._status["totals"]["ticks"] = int(self._status["totals"].get("ticks") or 0) + 1
            self._status["last_tick_at"] = self._now_utc().isoformat()
            self._status["state"] = "running"

        picked = self._pick_due_calendar_account(settings)
        if picked is not None:
            token, account, phase = picked
            binding = binding_key_for_account(account)
            if self._refresh_account(token, account, event="quota_refresh:calendar", phase_index=phase):
                self._mark_binding_refresh(binding)
                with self._lock:
                    self._status["totals"]["refreshed"] = int(self._status["totals"].get("refreshed") or 0) + 1
                    self._status["last_refresh_at"] = self._now_utc().isoformat()
                    self._status["last_account"] = str(account.get("email") or token[:16])
            return

        if self._maybe_pre_restore_refresh(settings):
            with self._lock:
                self._status["totals"]["refreshed"] = int(self._status["totals"].get("refreshed") or 0) + 1
            return

        with self._lock:
            self._status["totals"]["skipped"] = int(self._status["totals"].get("skipped") or 0) + 1
            self._status["state"] = "idle"

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = config.get_quota_refresh_schedule_settings()
            enabled = bool(settings.get("enabled"))
            with self._lock:
                self._status["enabled"] = enabled
                self._status["state"] = "idle" if enabled else "off"
            if enabled:
                try:
                    self._tick()
                except Exception as exc:
                    with self._lock:
                        self._status["totals"]["errors"] = int(self._status["totals"].get("errors") or 0) + 1
                        self._status["last_reason"] = str(exc)[:240]
                    logger.warning({"event": "quota_refresh_schedule_tick_error", "error": str(exc)[:240]})
            tick_sec = float(settings.get("tick_sec") or 60.0) if enabled else 5.0
            self._stop.wait(max(5.0, tick_sec))


quota_refresh_schedule_service = QuotaRefreshScheduleService()
