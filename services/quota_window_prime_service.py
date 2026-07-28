"""额度窗口预热：满额未进周期号打 1 张最小生图钉住 reset_after。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from services.account_identity import binding_key_for_account
from services.account_service import account_service
from services.config import config
from services.image_pipeline.binding_calendar import (
    PRIME_SALT,
    account_key_for_account,
    compute_account_phase_slot,
    engine_info,
    evaluate_prime_eligibility,
    list_prime_eligible,
    local_date_for_account,
    prime_account_input,
    prime_settings_input,
    resolve_tz_for_account,
)
from utils.log import logger


class QuotaWindowPrimeService:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._binding_running: dict[str, int] = {}
        self._pending_tokens: set[str] = set()
        self._status: dict[str, Any] = {
            "state": "off",
            "enabled": False,
            "queue_depth": 0,
            "last_prime_at": None,
            "last_account": None,
            "totals": {"ticks": 0, "enqueued": 0, "completed": 0, "failed": 0, "skipped": 0},
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="quota-window-prime", daemon=True)
        self._thread.start()
        logger.info({"event": "quota_window_prime_started"})

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        settings = config.get_quota_window_prime_settings()
        with self._lock:
            status = dict(self._status)
            status["settings"] = settings
            status["enabled"] = bool(settings.get("enabled"))
            status["queue_depth"] = len(self._pending_tokens)
            status["binding_running"] = dict(self._binding_running)
            status["prime_engine"] = engine_info()
            return status

    def _now_utc(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    def _now_unix(self) -> int:
        return int(self._clock())

    def check_eligibility(
        self,
        account: dict[str, Any],
        *,
        force: bool = False,
        settings: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        settings = settings or config.get_quota_window_prime_settings()
        mode = "force" if force else "auto"
        payload = {
            "mode": mode,
            "now_unix": self._now_unix(),
            "settings": prime_settings_input(settings),
            "account": prime_account_input(account),
        }
        result = evaluate_prime_eligibility(payload)
        return {
            "eligible": bool(result.get("eligible")),
            "reason": str(result.get("reason") or ""),
            "engine": engine_info().get("engine", "python"),
        }

    def _binding_can_run(self, binding_key: str, settings: dict[str, object]) -> bool:
        max_per_binding = max(1, int(settings.get("max_concurrent_per_binding") or 1))
        return int(self._binding_running.get(binding_key) or 0) < max_per_binding

    def _global_can_run(self, settings: dict[str, object]) -> bool:
        max_global = max(1, int(settings.get("max_concurrent_global") or 2))
        running = sum(int(v) for v in self._binding_running.values())
        return running < max_global

    def _mark_state(self, token: str, state: str, **extra: Any) -> None:
        updates: dict[str, Any] = {"quota_window_prime_state": state}
        updates.update(extra)
        account_service.update_account(token, updates, quiet=True)

    def enqueue(self, token: str, *, force: bool = False) -> dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            raise ValueError("access_token is required")
        account = account_service.get_account(token)
        if not account:
            raise ValueError("account not found")
        settings = config.get_quota_window_prime_settings()
        if not bool(settings.get("enabled")):
            raise RuntimeError("quota_window_prime is disabled")

        check = self.check_eligibility(account, force=force, settings=settings)
        if not check.get("eligible"):
            raise ValueError(f"not eligible: {check.get('reason')}")

        with self._lock:
            if token in self._pending_tokens:
                return {
                    "ok": True,
                    "state": "pending",
                    "duplicate": True,
                    "email": account.get("email"),
                    "reason": check.get("reason"),
                }
            self._pending_tokens.add(token)
            self._status["totals"]["enqueued"] = int(self._status["totals"].get("enqueued") or 0) + 1

        self._mark_state(token, "pending", quota_window_prime_last_error=None)
        return {
            "ok": True,
            "state": "pending",
            "email": account.get("email"),
            "reason": check.get("reason"),
        }

    def enqueue_many(self, tokens: list[str], *, force: bool = False) -> dict[str, Any]:
        results = []
        errors = []
        for token in tokens:
            try:
                results.append(self.enqueue(token, force=force))
            except Exception as exc:
                errors.append({"token_prefix": str(token)[:16], "error": str(exc)})
        return {"ok": not errors, "enqueued": len(results), "errors": errors, "items": results}

    def enqueue_by_email(self, email: str, *, force: bool = False) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        if not target:
            raise ValueError("email is required")
        for account in account_service.list_accounts():
            if not isinstance(account, dict):
                continue
            if str(account.get("email") or "").strip().lower() != target:
                continue
            token = str(account.get("access_token") or "").strip()
            if token:
                return self.enqueue(token, force=force)
        raise ValueError("account not found")

    def on_task_terminal(self, *, payload: dict[str, Any], success: bool, access_token: str) -> None:
        if str(payload.get("task_kind") or "") != "quota_prime":
            return
        token = str(access_token or "").strip()
        if not token:
            return
        binding = ""
        account = account_service.get_account(token)
        if account:
            binding = binding_key_for_account(account)
        with self._lock:
            self._pending_tokens.discard(token)
            if binding:
                self._binding_running[binding] = max(0, int(self._binding_running.get(binding) or 0) - 1)
        if success:
            try:
                refreshed = account_service.fetch_remote_info(token, event="quota_prime:post")
                restore_at = refreshed.get("restore_at") if isinstance(refreshed, dict) else None
                self._mark_state(
                    token,
                    "done",
                    quota_window_primed_at=datetime.now(timezone.utc).isoformat(),
                    quota_window_primed_restore_at=restore_at,
                    quota_window_prime_last_error=None,
                )
                with self._lock:
                    self._status["totals"]["completed"] = int(self._status["totals"].get("completed") or 0) + 1
                    self._status["last_prime_at"] = self._now_utc().isoformat()
                    self._status["last_account"] = str((account or {}).get("email") or token[:16])
            except Exception as exc:
                self._mark_state(token, "failed", quota_window_prime_last_error=str(exc)[:240])
                with self._lock:
                    self._status["totals"]["failed"] = int(self._status["totals"].get("failed") or 0) + 1
        else:
            attempts = 0
            if account:
                try:
                    attempts = int(account.get("quota_window_prime_attempts") or 0) + 1
                except (TypeError, ValueError):
                    attempts = 1
            self._mark_state(
                token,
                "failed",
                quota_window_prime_attempts=attempts,
                quota_window_prime_last_error=str(payload.get("prime_error") or "prime failed")[:240],
            )
            with self._lock:
                self._status["totals"]["failed"] = int(self._status["totals"].get("failed") or 0) + 1

    def _dispatch_one(self, token: str, account: dict[str, Any], settings: dict[str, object]) -> bool:
        binding = binding_key_for_account(account)
        if not self._binding_can_run(binding, settings) or not self._global_can_run(settings):
            return False
        email = str(account.get("email") or "").strip()
        client_task_id = f"prime-{token[:16]}-{int(self._clock())}"
        try:
            from services.image_pipeline import schedule_trace as _trace

            if _trace.enabled():
                run = _trace.begin(client_task_id, email)
                tok = _trace.bind(run)
                _trace.emit("quota_prime_start")
            else:
                tok = None
                run = None
        except Exception:
            tok = None
            run = None
        try:
            from services.image_task_service import image_task_service

            image_task_service.submit_quota_prime(
                access_token=token,
                email=email,
                client_task_id=client_task_id,
            )
        except Exception as exc:
            self._mark_state(token, "failed", quota_window_prime_last_error=str(exc)[:240])
            with self._lock:
                self._pending_tokens.discard(token)
                self._status["totals"]["failed"] = int(self._status["totals"].get("failed") or 0) + 1
            return False
        finally:
            try:
                if run is not None:
                    from services.image_pipeline import schedule_trace as _trace

                    _trace.emit("quota_prime_end")
                    if tok is not None:
                        _trace.unbind(tok)
                    _trace.pop(client_task_id)
            except Exception:
                pass

        with self._lock:
            self._pending_tokens.discard(token)
            self._binding_running[binding] = int(self._binding_running.get(binding) or 0) + 1
        self._mark_state(token, "running")
        return True

    def _auto_slot_due(self, account: dict[str, Any], settings: dict[str, object]) -> bool:
        now = self._now_utc()
        schedule = config.get_quota_refresh_schedule_settings()
        default_tz = str(schedule.get("default_timezone") or "Asia/Singapore")
        tz_from_egress = bool(schedule.get("timezone_from_egress", True))
        tz_name = resolve_tz_for_account(account, default_tz=default_tz, timezone_from_egress=tz_from_egress)
        local_day = local_date_for_account(
            now,
            account,
            default_tz=default_tz,
            timezone_from_egress=tz_from_egress,
        )
        phase_index = int(settings.get("auto_phase_index") or 0)
        slot = compute_account_phase_slot(
            account_key=account_key_for_account(account),
            binding_key=binding_key_for_account(account),
            local_day=local_day,
            phase_index=phase_index,
            tz_name=tz_name,
            jitter_min_minutes=int(settings.get("account_jitter_min_minutes") or 30),
            jitter_max_minutes=int(settings.get("account_jitter_max_minutes") or 90),
            salt=PRIME_SALT,
        )
        return slot["account_slot_utc"] <= now

    def _tick(self) -> None:
        settings = config.get_quota_window_prime_settings()
        if not bool(settings.get("enabled")):
            return
        with self._lock:
            self._status["totals"]["ticks"] = int(self._status["totals"].get("ticks") or 0) + 1

        with self._lock:
            pending = list(self._pending_tokens)
        for token in pending:
            account = account_service.get_account(token)
            if not account:
                with self._lock:
                    self._pending_tokens.discard(token)
                continue
            if self._dispatch_one(token, account, settings):
                return

        accounts_raw = account_service.list_accounts()
        indexed_accounts: list[dict[str, Any]] = []
        token_by_index: dict[int, str] = {}
        account_by_index: dict[int, dict[str, Any]] = {}
        for index, account in enumerate(accounts_raw):
            if not isinstance(account, dict):
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            indexed_accounts.append(prime_account_input(account, index=index))
            token_by_index[index] = token
            account_by_index[index] = account

        max_attempts = int(settings.get("max_auto_attempts") or 3)
        eligible = list_prime_eligible(
            {
                "now_unix": self._now_unix(),
                "settings": prime_settings_input(settings),
                "max_attempts": max_attempts,
                "accounts": indexed_accounts,
            }
        )
        for idx in eligible.get("indices") or []:
            try:
                index = int(idx)
            except (TypeError, ValueError):
                continue
            token = token_by_index.get(index)
            account = account_by_index.get(index)
            if not token or account is None:
                continue
            if not self._auto_slot_due(account, settings):
                continue
            if self._dispatch_one(token, account, settings):
                return

        with self._lock:
            self._status["totals"]["skipped"] = int(self._status["totals"].get("skipped") or 0) + 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = config.get_quota_window_prime_settings()
            enabled = bool(settings.get("enabled"))
            with self._lock:
                self._status["enabled"] = enabled
                self._status["state"] = "idle" if enabled else "off"
            if enabled:
                try:
                    self._tick()
                except Exception as exc:
                    logger.warning({"event": "quota_window_prime_tick_error", "error": str(exc)[:240]})
            self._stop.wait(max(15.0, float(settings.get("tick_sec") or 30.0)))


quota_window_prime_service = QuotaWindowPrimeService()
