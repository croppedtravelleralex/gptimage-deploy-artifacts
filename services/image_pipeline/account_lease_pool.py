"""Pre-warm preferred account emails without holding image_inflight slots."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from services.account_service import account_service
from services.config import config


@dataclass
class _EmailHint:
    email: str
    created_ts: float


class AccountLeasePool:
    """Queue of schedulable account emails for faster preferred routing.

    Hints do **not** call ``get_available_access_token`` — they never consume
    ``image_inflight`` until a worker actually acquires for sS.
    """

    HINT_TTL_SECS = 60.0
    MAX_HINTS = 20

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hints: deque[_EmailHint] = deque()

    def _enabled(self) -> bool:
        try:
            settings = config.get_image_pipeline_settings()
            return bool(settings.get("account_lease_prewarm_enabled", True))
        except Exception:
            return True

    def _trim_stale_locked(self) -> None:
        now = time.time()
        while self._hints and (now - self._hints[0].created_ts) > self.HINT_TTL_SECS:
            self._hints.popleft()

    def _known_emails_locked(self) -> set[str]:
        return {item.email for item in self._hints if item.email}

    def _pick_email(self, exclude: set[str]) -> str:
        try:
            accounts = account_service.list_accounts()
        except Exception:
            accounts = []
        try:
            from services.config import config

            max_conc = max(1, int(config.image_account_concurrency or 1))
        except Exception:
            max_conc = 1
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if not account.get("image_schedulable"):
                continue
            if int(account.get("image_inflight") or 0) >= max_conc:
                continue
            email = str(account.get("email") or "").strip().lower()
            if not email or email in exclude:
                continue
            return email
        return ""

    def seed_hint(self, email: str) -> bool:
        """Reserve a preferred email in the hint queue (no slot consumption)."""
        prefer = str(email or "").strip().lower()
        if not prefer:
            return False
        with self._lock:
            self._trim_stale_locked()
            known = self._known_emails_locked()
            if prefer in known:
                return True
            if len(self._hints) >= self.MAX_HINTS:
                return False
            self._hints.appendleft(_EmailHint(email=prefer, created_ts=time.time()))
            return True

    def seed_queued_preferences(self, tasks: dict[str, object]) -> int:
        """Seed hints for all queued preferred emails (burst conc submit)."""
        if not self._enabled():
            return 0
        preferred: list[str] = []
        for task in tasks.values():
            if not isinstance(task, dict):
                continue
            if str(task.get("status") or "") != "queued":
                continue
            payload = task.get("payload")
            if not isinstance(payload, dict):
                continue
            email = str(payload.get("preferred_account_email") or "").strip().lower()
            if email:
                preferred.append(email)
        seeded = 0
        with self._lock:
            self._trim_stale_locked()
            known = self._known_emails_locked()
            for email in preferred:
                if email in known:
                    continue
                if len(self._hints) >= self.MAX_HINTS:
                    break
                self._hints.appendleft(_EmailHint(email=email, created_ts=time.time()))
                known.add(email)
                seeded += 1
        return seeded

    def maintain(self, *, max_acquire: int = 10) -> int:
        if not self._enabled():
            return 0
        acquired = 0
        with self._lock:
            self._trim_stale_locked()
            known = self._known_emails_locked()
            while acquired < max_acquire and len(self._hints) < self.MAX_HINTS:
                email = self._pick_email(known)
                if not email:
                    break
                self._hints.append(_EmailHint(email=email, created_ts=time.time()))
                known.add(email)
                acquired += 1
        return acquired

    def pop_hint(self, preferred_email: str = "") -> str:
        prefer = str(preferred_email or "").strip().lower()
        with self._lock:
            self._trim_stale_locked()
            if prefer:
                for index, item in enumerate(self._hints):
                    if item.email == prefer:
                        del self._hints[index]
                        return prefer
            if self._hints:
                return self._hints.popleft().email
        return ""


account_lease_pool = AccountLeasePool()
