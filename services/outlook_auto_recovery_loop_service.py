from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any

from services.account_service import account_service
from services.config import config
from services.outlook_account_recovery_service import (
    _is_terminal_outlook_recovery,
    _is_outlook_email,
    outlook_account_recovery_service,
)
from utils.log import logger


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked = "***" if len(local) <= 3 else f"{local[:2]}***{local[-1]}"
    return f"{masked}@{domain}"


def _parse_account_time(value: object) -> datetime | None:
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


def _has_stale_invalid_evidence(account: dict[str, Any]) -> bool:
    """正常态但仍有明确 invalid 证据，且已过确认窗/新号宽限。"""
    try:
        invalid_count = int(account.get("invalid_count") or 0)
    except (TypeError, ValueError):
        invalid_count = 0
    if invalid_count <= 0:
        return False
    # invalid_count>0 本身即证据；错误文案缺失时仍允许陈旧 invalid 进入串行恢复。
    now = datetime.now(timezone.utc)
    created_at = _parse_account_time(account.get("created_at"))
    if created_at is not None and (now - created_at).total_seconds() < 10 * 60:
        return False
    last_invalid_at = _parse_account_time(account.get("last_invalid_at"))
    if last_invalid_at is None:
        # 有 invalid_count 但无时间戳：视为已过确认窗，允许进入恢复候选。
        return True
    return (now - last_invalid_at).total_seconds() >= 30


def is_outlook_auto_recovery_candidate(account: dict[str, Any]) -> bool:
    """异常、rejected，或超过确认窗的「正常+invalid」Outlook；终态不重试。"""
    email = str(account.get("email") or "").strip().lower()
    if not _is_outlook_email(email):
        return False
    status = str(account.get("status") or "").strip()
    if status == "禁用" or _is_terminal_outlook_recovery(account):
        return False
    receive = str(account.get("panda_receive_state") or "").strip().lower()
    if status == "异常" or receive == "rejected":
        return True
    if status == "正常" and _has_stale_invalid_evidence(account):
        return True
    return False


def select_outlook_auto_recovery_candidates(
    accounts: list[dict[str, Any]],
    *,
    limit: int = 1,
    skip_emails: set[str] | None = None,
) -> list[dict[str, Any]]:
    skipped = {str(item or "").strip().lower() for item in (skip_emails or set()) if str(item or "").strip()}
    selected: list[dict[str, Any]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if not is_outlook_auto_recovery_candidate(account):
            continue
        email = str(account.get("email") or "").strip().lower()
        if email in skipped:
            continue
        token = str(account.get("access_token") or "").strip()
        if not token:
            continue
        selected.append(dict(account))
        if len(selected) >= max(1, int(limit or 1)):
            break
    return selected


class OutlookAutoRecoveryLoopService:
    """按间隔扫描异常 Outlook 并串行触发已有 recover-outlook 链路。"""

    def __init__(
        self,
        *,
        account_service: Any = account_service,
        recovery_service: Any = outlook_account_recovery_service,
    ) -> None:
        self.account_service = account_service
        self.recovery_service = recovery_service
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._startup_delay_done = False
        self._status: dict[str, Any] = self._initial_status()

    def _initial_status(self) -> dict[str, Any]:
        settings = config.get_outlook_auto_recovery_settings()
        return {
            "state": "off" if not settings.get("enabled") else "idle",
            "enabled": bool(settings.get("enabled")),
            "started_at": None,
            "last_update_at": None,
            "last_run_at": None,
            "next_run_at": None,
            "seconds_until_next_run": None,
            "pause_reason": "",
            "current": None,
            "last_result": None,
            "candidate_count": 0,
            "terminal_count": 0,
            "totals": {
                "cycles": 0,
                "scanned": 0,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped_busy": 0,
                "skipped_paused": 0,
            },
            "recent": [],
            "settings": settings,
        }

    def start_background(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event = Event()
            self._thread = Thread(target=self._run, name="outlook-auto-recovery-loop", daemon=True)
            self._thread.start()

    def stop_background(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread:
            thread.join(timeout=1)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            status = dict(self._status)
            next_run_at = status.get("next_run_at")
            seconds = None
            if next_run_at:
                try:
                    deadline = datetime.fromisoformat(str(next_run_at).replace("Z", "+00:00"))
                    seconds = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
                except Exception:
                    seconds = None
            status["seconds_until_next_run"] = seconds
            return status

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = config.get_outlook_auto_recovery_settings()
        merged = {**current, **dict(updates or {})}
        saved = config.update({"outlook_auto_recovery": merged})
        settings = (
            saved.get("outlook_auto_recovery")
            if isinstance(saved, dict)
            else config.get_outlook_auto_recovery_settings()
        )
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

    def _bump_total(self, key: str, delta: int = 1) -> None:
        with self._lock:
            totals = dict(self._status.get("totals") or {})
            totals[key] = int(totals.get(key) or 0) + int(delta)
            self._status["totals"] = totals

    def _append_recent(self, item: dict[str, Any]) -> None:
        with self._lock:
            recent = list(self._status.get("recent") or [])
            recent.append(item)
            self._status["recent"] = recent[-20:]

    def _wait_until(self, deadline: float, *, state: str = "idle") -> None:
        while not self._stop_event.is_set():
            settings = config.get_outlook_auto_recovery_settings()
            if not settings.get("enabled"):
                self._set_status(state="off", enabled=False, next_run_at=None, pause_reason="disabled")
                return
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                return
            self._set_status(
                state=state,
                enabled=True,
                next_run_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
                pause_reason="",
            )
            if self._stop_event.wait(min(remaining, 5.0)):
                return

    def _prerequisites_ok(self) -> tuple[bool, str]:
        checker = getattr(self.recovery_service, "check_prerequisites", None)
        if callable(checker):
            try:
                return checker()
            except Exception as exc:
                return False, str(exc)
        return True, ""

    def _wait_progress(self, progress_id: str, *, poll_sec: float) -> dict[str, Any]:
        deadline = time.time() + float(self.recovery_service.timeout_secs or 900.0) + 30.0
        while not self._stop_event.is_set():
            progress = self.recovery_service.get_progress(progress_id) or {}
            if bool(progress.get("done")):
                return progress
            if time.time() >= deadline:
                return {
                    "done": True,
                    "ok": False,
                    "error": "auto recovery wait timeout",
                    "stage": "failed",
                    "email": progress.get("email") or "",
                }
            self._set_status(
                state="recovering",
                current={
                    "progress_id": progress_id,
                    "email": progress.get("email"),
                    "stage": progress.get("stage"),
                    "message": progress.get("message"),
                },
            )
            if self._stop_event.wait(max(0.5, float(poll_sec))):
                break
        return self.recovery_service.get_progress(progress_id) or {
            "done": True,
            "ok": False,
            "error": "auto recovery interrupted",
            "stage": "failed",
        }

    def _run_cycle(self, settings: dict[str, object]) -> None:
        self._bump_total("cycles")
        self._set_status(state="scanning", current=None, pause_reason="", last_run_at=_iso_now())

        ok, reason = self._prerequisites_ok()
        if not ok:
            self._bump_total("skipped_paused")
            self._set_status(state="paused", pause_reason=reason or "recovery prerequisites missing")
            return

        if bool(getattr(self.recovery_service, "is_busy", lambda: False)()):
            self._bump_total("skipped_busy")
            self._set_status(
                state="idle",
                pause_reason="",
                last_result={
                    "at": _iso_now(),
                    "ok": False,
                    "skipped": True,
                    "reason": "manual_or_other_recovery_in_progress",
                },
            )
            return

        accounts = self.account_service.list_accounts()
        limit = int(settings.get("max_per_cycle") or 1)
        candidates = select_outlook_auto_recovery_candidates(accounts, limit=limit)
        terminal_count = sum(
            1
            for account in accounts
            if isinstance(account, dict)
            and _is_outlook_email(account.get("email"))
            and _is_terminal_outlook_recovery(account)
        )
        self._set_status(candidate_count=len(candidates), terminal_count=terminal_count)
        self._bump_total("scanned", len(accounts))

        if not candidates:
            self._set_status(
                state="idle",
                last_result={"at": _iso_now(), "ok": True, "attempted": 0, "candidate_count": 0},
            )
            return

        for account in candidates:
            if self._stop_event.is_set():
                return
            if bool(getattr(self.recovery_service, "is_busy", lambda: False)()):
                self._bump_total("skipped_busy")
                break
            email = str(account.get("email") or "").strip().lower()
            token = str(account.get("access_token") or "").strip()
            masked = _mask_email(email)
            self._set_status(
                state="recovering",
                current={"email": masked, "stage": "queued", "message": "自动恢复已排队"},
            )
            self._bump_total("attempted")
            try:
                progress_id = self.recovery_service.start(token)
                progress = self._wait_progress(
                    progress_id,
                    poll_sec=float(settings.get("progress_poll_sec") or 2.0),
                )
                ok_result = bool(progress.get("ok"))
                result = {
                    "at": _iso_now(),
                    "ok": ok_result,
                    "email": progress.get("email") or masked,
                    "stage": progress.get("stage"),
                    "error": progress.get("error") or "",
                    "quota": (progress.get("result") or {}).get("quota") if isinstance(progress.get("result"), dict) else None,
                    "progress_id": progress_id,
                }
                if ok_result:
                    self._bump_total("succeeded")
                else:
                    self._bump_total("failed")
                self._append_recent(result)
                self._set_status(last_result=result, current=None, state="idle", pause_reason="")
                logger.info(
                    {
                        "event": "outlook_auto_recovery_cycle_result",
                        "email": masked,
                        "ok": ok_result,
                        "error": result.get("error") or "",
                    }
                )
            except Exception as exc:
                self._bump_total("failed")
                result = {
                    "at": _iso_now(),
                    "ok": False,
                    "email": masked,
                    "stage": "failed",
                    "error": str(exc)[:500],
                }
                self._append_recent(result)
                self._set_status(last_result=result, current=None, state="idle", pause_reason="")
                logger.warning(
                    {
                        "event": "outlook_auto_recovery_cycle_failed",
                        "email": masked,
                        "error": result["error"],
                    }
                )

    def _run(self) -> None:
        self._set_status(started_at=_iso_now())
        while not self._stop_event.is_set():
            settings = config.get_outlook_auto_recovery_settings()
            self._set_status(settings=settings, enabled=bool(settings.get("enabled")))
            if not settings.get("enabled"):
                self._set_status(state="off", next_run_at=None, pause_reason="disabled")
                if self._stop_event.wait(5.0):
                    break
                continue

            if not self._startup_delay_done:
                delay = float(settings.get("startup_delay_sec") or 0.0)
                if delay > 0:
                    self._wait_until(time.time() + delay, state="idle")
                    if self._stop_event.is_set():
                        break
                    if not config.get_outlook_auto_recovery_settings().get("enabled"):
                        continue
                self._startup_delay_done = True

            try:
                self._run_cycle(config.get_outlook_auto_recovery_settings())
            except Exception as exc:
                logger.warning({"event": "outlook_auto_recovery_loop_error", "error": str(exc)[:500]})
                self._set_status(state="idle", pause_reason=str(exc)[:300], current=None)

            settings = config.get_outlook_auto_recovery_settings()
            if not settings.get("enabled"):
                continue
            interval = max(60.0, float(settings.get("interval_sec") or 1800.0))
            self._wait_until(time.time() + interval, state="idle")


outlook_auto_recovery_loop_service = OutlookAutoRecoveryLoopService()
