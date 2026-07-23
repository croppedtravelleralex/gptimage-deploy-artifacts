"""常驻预热：对可调度账号周期性 _ensure_bootstrap，并按并发/频率轮换热池。

热池账号加速首轮 TTFT；同号并发会话达阈或短时频率过高时 demote，另选号补位。
业务粘账号会话可继续用 demote 号，只是不再占用热缓存槽位。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from services.account_service import account_service
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from utils.log import logger


def _settings() -> dict[str, Any]:
    raw = config.data.get("account_warmup") if isinstance(getattr(config, "data", None), dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "interval_sec": max(30.0, float(raw.get("interval_sec", 120.0) or 120.0)),
        "max_hot": max(1, int(raw.get("max_hot", 3) or 3)),
        "max_sessions_per_hot": max(1, int(raw.get("max_sessions_per_hot", 3) or 3)),
        "demote_cooldown_sec": max(10.0, float(raw.get("demote_cooldown_sec", 180.0) or 180.0)),
        "freq_window_sec": max(10.0, float(raw.get("freq_window_sec", 60.0) or 60.0)),
        "freq_max_starts": max(2, int(raw.get("freq_max_starts", 6) or 6)),
        "startup_delay_sec": max(0.0, float(raw.get("startup_delay_sec", 20.0) or 20.0)),
    }


class AccountWarmupService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # email -> last successful warmup at
        self._hot: dict[str, float] = {}
        # email -> demoted_until
        self._demoted_until: dict[str, float] = {}
        # email -> inflight session count
        self._inflight: dict[str, int] = defaultdict(int)
        # email -> recent start timestamps
        self._starts: dict[str, deque[float]] = defaultdict(deque)
        self._last_error = ""
        self._last_ok_at = 0.0
        self._totals = {"ticks": 0, "warmed": 0, "demoted": 0, "errors": 0}

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-warmup", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def demote(self, email: str, *, reason: str = "manual") -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        settings = _settings()
        with self._lock:
            self._demote_locked(key, settings, reason=reason)

    def hot_emails(self) -> set[str]:
        with self._lock:
            return set(self._hot.keys())

    def status(self) -> dict[str, Any]:
        settings = _settings()
        with self._lock:
            return {
                "enabled": settings["enabled"],
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "hot": sorted(self._hot.keys()),
                "hot_count": len(self._hot),
                "inflight": dict(self._inflight),
                "demoted_until": {k: v for k, v in self._demoted_until.items() if v > time.time()},
                "last_error": self._last_error,
                "last_ok_at": self._last_ok_at or None,
                "totals": dict(self._totals),
                "settings": settings,
            }

    def begin_chat_session(self, email: str) -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        settings = _settings()
        now = time.time()
        with self._lock:
            self._inflight[key] += 1
            q = self._starts[key]
            q.append(now)
            window = float(settings["freq_window_sec"])
            while q and now - q[0] > window:
                q.popleft()
            inflight = self._inflight[key]
            freq = len(q)
            should_demote = key in self._hot and (
                inflight >= int(settings["max_sessions_per_hot"])
                or freq >= int(settings["freq_max_starts"])
            )
            if should_demote:
                self._demote_locked(key, settings, reason="inflight_or_freq")

    def end_chat_session(self, email: str) -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        with self._lock:
            cur = self._inflight.get(key, 0)
            if cur <= 1:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = cur - 1

    def is_hot(self, email: str) -> bool:
        key = str(email or "").strip().lower()
        with self._lock:
            return key in self._hot

    def _demote_locked(self, email: str, settings: dict[str, Any], *, reason: str) -> None:
        if email in self._hot:
            self._hot.pop(email, None)
            self._totals["demoted"] += 1
            self._demoted_until[email] = time.time() + float(settings["demote_cooldown_sec"])
            logger.info(
                {
                    "event": "account_warmup_demote",
                    "email": email,
                    "reason": reason,
                    "inflight": self._inflight.get(email, 0),
                    "cooldown_sec": settings["demote_cooldown_sec"],
                }
            )

    def _loop(self) -> None:
        settings = _settings()
        delay = float(settings["startup_delay_sec"])
        if delay > 0:
            self._stop.wait(delay)
        while not self._stop.is_set():
            settings = _settings()
            if settings["enabled"]:
                try:
                    self._tick(settings)
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"{type(exc).__name__}: {exc}"[:240]
                        self._totals["errors"] += 1
                    logger.warning({"event": "account_warmup_tick_error", "error": str(exc)[:240]})
            self._stop.wait(float(settings["interval_sec"]))

    def _candidate_accounts(self) -> list[dict[str, Any]]:
        """Prefer image-schedulable (verified*) accounts for warm pool."""
        items = account_service.list_accounts()
        out: list[dict[str, Any]] = []
        for account in items:
            if not isinstance(account, dict):
                continue
            if account.get("status") in {"禁用", "异常", "限流"}:
                continue
            receive = str(account.get("panda_receive_state") or "").strip().lower()
            if receive and receive not in {"verified_ready", "verified", "local_verified"}:
                continue
            token = str(account.get("access_token") or "").strip()
            email = str(account.get("email") or "").strip().lower()
            if not token or not email:
                continue
            out.append(account)
        return out

    def _tick(self, settings: dict[str, Any]) -> None:
        # list_accounts acquires account_service._lock — never call under warmup._lock.
        candidates = self._candidate_accounts()
        emails_alive = {str(a.get("email") or "").strip().lower() for a in candidates}
        with self._lock:
            self._totals["ticks"] += 1
            now = time.time()
            expired = [e for e, until in self._demoted_until.items() if until <= now]
            for e in expired:
                self._demoted_until.pop(e, None)
            for email in list(self._hot.keys()):
                if email not in emails_alive:
                    self._hot.pop(email, None)
            need = max(0, int(settings["max_hot"]) - len(self._hot))
            demoted = dict(self._demoted_until)
            hot_now = set(self._hot.keys())

        max_hot = int(settings["max_hot"])
        if need <= 0:
            for email in list(hot_now)[:max_hot]:
                account = next(
                    (a for a in candidates if str(a.get("email") or "").strip().lower() == email),
                    None,
                )
                if account:
                    self._warmup_one(account)
            return

        picked: list[dict[str, Any]] = []
        for account in candidates:
            email = str(account.get("email") or "").strip().lower()
            if email in hot_now:
                continue
            if demoted.get(email, 0) > time.time():
                continue
            picked.append(account)
            if len(picked) >= need:
                break

        for account in picked:
            ok = self._warmup_one(account)
            if not ok:
                continue
            email = str(account.get("email") or "").strip().lower()
            with self._lock:
                if len(self._hot) < max_hot:
                    self._hot[email] = time.time()
                    self._totals["warmed"] += 1
                    logger.info({"event": "account_warmup_promote", "email": email})

        with self._lock:
            hot_refresh = list(self._hot.keys())
        for email in hot_refresh:
            if any(str(a.get("email") or "").strip().lower() == email for a in picked):
                continue
            account = next(
                (a for a in candidates if str(a.get("email") or "").strip().lower() == email),
                None,
            )
            if account:
                self._warmup_one(account)

    def _warmup_one(self, account: dict[str, Any]) -> bool:
        token = str(account.get("access_token") or "").strip()
        email = str(account.get("email") or "").strip().lower()
        if not token:
            return False
        backend: OpenAIBackendAPI | None = None
        try:
            backend = OpenAIBackendAPI(access_token=token)
            backend._ensure_bootstrap()
            with self._lock:
                self._last_ok_at = time.time()
                self._last_error = ""
            return True
        except Exception as exc:
            with self._lock:
                self._last_error = f"{email}: {type(exc).__name__}: {exc}"[:240]
                self._totals["errors"] += 1
            logger.warning(
                {
                    "event": "account_warmup_fail",
                    "email": email,
                    "error": str(exc)[:240],
                }
            )
            return False
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass


account_warmup_service = AccountWarmupService()
