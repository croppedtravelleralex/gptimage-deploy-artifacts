"""工作日/休息日日历主动探活（Asia/Singapore），替代机械 maintenance 齐刷。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any

from services.account_service import account_service
from services.config import config
from services.humanlike_scheduler import decide_proactive_refresh, resolve_account_tz_name, resolve_tz_name
from zoneinfo import ZoneInfo


class ProactiveRefreshLoopService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._startup_done = False
        self._minute_bucket = ""
        self._minute_count = 0
        self._status: dict[str, Any] = {
            "state": "off",
            "enabled": False,
            "last_update_at": None,
            "last_refresh_at": None,
            "last_account": None,
            "last_reason": "",
            "totals": {"ticks": 0, "refreshed": 0, "skipped": 0, "errors": 0},
        }

    def start_background(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = Thread(target=self._run, name="proactive-refresh-loop", daemon=True)
            self._thread.start()

    def stop_background(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        settings = config.get_proactive_refresh_settings()
        with self._lock:
            status = dict(self._status)
            status["settings"] = settings
            status["enabled"] = bool(settings.get("enabled"))
            if status["enabled"] and status.get("state") == "off":
                status["state"] = "starting"
            return status

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self._status.update(kwargs)
            self._status["last_update_at"] = datetime.now(timezone.utc).isoformat()

    def _account_key(self, account: dict) -> str:
        for field in ("token_hash", "email", "user_id", "access_token"):
            value = str(account.get(field) or "").strip()
            if value:
                return value[:64]
        return "unknown"

    def _pick_due_account(self, settings: dict[str, object]) -> tuple[str, dict, str] | None:
        now = datetime.now(timezone.utc)
        tz = ZoneInfo(resolve_tz_name(str(settings.get("timezone") or "Asia/Singapore")))
        local = now.astimezone(tz)
        local_minute = local.strftime("%Y%m%d%H%M")
        is_work = local.isoweekday() in {int(x) for x in (settings.get("workdays") or [1, 2, 3, 4, 5])}
        cap = int(settings.get("minute_cap_k") if is_work else settings.get("minute_cap_k_rest") or 1)
        if self._minute_bucket != local_minute:
            self._minute_bucket = local_minute
            self._minute_count = 0
        if self._minute_count >= cap:
            return None

        window_work = settings.get("window_work") or ["09:00", "17:00"]
        window_rest = settings.get("window_rest") or ["10:00", "16:00"]
        work_pair = (str(window_work[0]), str(window_work[1]))
        rest_pair = (str(window_rest[0]), str(window_rest[1]))

        with account_service._lock:
            accounts = [dict(item) for item in account_service._accounts.values()]

        for account in accounts:
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            if str(account.get("panda_receive_state") or "").strip().lower() == "identity_isolated":
                continue
            if account.get("status") in {"禁用", "异常"}:
                continue
            decision = decide_proactive_refresh(
                now_utc=now,
                account_key=self._account_key(account),
                done_date=str(account.get("proactive_refresh_done_date") or "") or None,
                tz_name=resolve_account_tz_name(
                    account,
                    timezone_from_egress=bool(settings.get("timezone_from_egress")),
                    default_tz=str(settings.get("timezone") or "Asia/Singapore"),
                ),
                workdays=tuple(int(x) for x in (settings.get("workdays") or [1, 2, 3, 4, 5])),
                p_work=float(settings.get("p_work") or 1.0),
                p_rest=float(settings.get("p_rest") or 0.35),
                window_work=work_pair,
                window_rest=rest_pair,
                slot_jitter_minutes=int(settings.get("slot_jitter_minutes") or 10),
            )
            if decision.due and decision.local_date:
                return token, account, decision.local_date
        return None

    def _refresh_one(self, token: str, local_date: str) -> None:
        account_service.fetch_remote_info(token, event="proactive_refresh")
        account_service.update_account(
            token,
            {"proactive_refresh_done_date": local_date},
            quiet=True,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = config.get_proactive_refresh_settings()
            enabled = bool(settings.get("enabled"))
            self._set(enabled=enabled, state="idle" if enabled else "off")
            if not enabled:
                if self._stop_event.wait(5.0):
                    return
                continue

            if not self._startup_done:
                self._startup_done = True
                delay = float(settings.get("startup_delay_sec") or 0)
                if delay > 0 and self._stop_event.wait(delay):
                    return

            with self._lock:
                totals = dict(self._status.get("totals") or {})
                totals["ticks"] = int(totals.get("ticks") or 0) + 1
                self._status["totals"] = totals

            try:
                picked = self._pick_due_account(settings)
                if picked is None:
                    with self._lock:
                        self._status["totals"]["skipped"] = int(self._status["totals"].get("skipped") or 0) + 1
                else:
                    token, _account, local_date = picked
                    self._refresh_one(token, local_date)
                    self._minute_count += 1
                    with self._lock:
                        self._status["totals"]["refreshed"] = int(self._status["totals"].get("refreshed") or 0) + 1
                        self._status["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
                        self._status["last_account"] = token[:16]
                        self._status["last_reason"] = "due"
                        self._status["state"] = "running"
            except Exception as exc:
                with self._lock:
                    self._status["totals"]["errors"] = int(self._status["totals"].get("errors") or 0) + 1
                    self._status["last_reason"] = f"error:{exc}"

            tick = float(settings.get("tick_sec") or 60)
            if self._stop_event.wait(tick):
                return


proactive_refresh_loop_service = ProactiveRefreshLoopService()
