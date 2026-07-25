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

    HINT_TTL_SECS = 45.0
    MAX_HINTS = 3

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
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if not account.get("image_schedulable"):
                continue
            email = str(account.get("email") or "").strip().lower()
            if not email or email in exclude:
                continue
            return email
        return ""

    def maintain(self, *, max_acquire: int = 2) -> int:
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
