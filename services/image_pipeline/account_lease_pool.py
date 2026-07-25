"""Pre-acquired account leases sized to free sS / global slots."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from services.account_service import account_service
from services.config import config


@dataclass
class _PooledLease:
    access_token: str
    email: str
    account_id: str
    created_ts: float


class AccountLeasePool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: deque[_PooledLease] = deque()
        self._by_email: dict[str, _PooledLease] = {}

    def _enabled(self) -> bool:
        try:
            settings = config.get_image_pipeline_settings()
            return bool(settings.get("account_lease_prewarm_enabled", True))
        except Exception:
            return True

    def _target_depth(self) -> int:
        try:
            pipeline = config.get_image_pipeline_settings()
            cap = int(pipeline.get("sse_slots") or 10)
        except Exception:
            cap = 10
        try:
            stats = account_service.get_image_candidate_runtime_stats()
            inflight = int(stats.get("image_inflight_count") or 0)
            global_limit = int(stats.get("image_global_limit") or stats.get("image_global_concurrency_limit") or cap)
        except Exception:
            inflight = 0
            global_limit = cap
        cap = max(1, min(cap, global_limit))
        with self._lock:
            pooled = len(self._leases)
        return max(0, cap - inflight - pooled)

    def maintain(self, *, max_acquire: int = 3) -> int:
        if not self._enabled():
            return 0
        acquired = 0
        while acquired < max_acquire:
            need = self._target_depth()
            if need <= 0:
                break
            try:
                token = account_service.get_available_access_token(skip_global_limit=False)
            except Exception:
                break
            account = account_service.get_account(token) or {}
            email = str(account.get("email") or "").strip().lower()
            lease = _PooledLease(
                access_token=token,
                email=email,
                account_id=email or str(account.get("id") or token[:12]),
                created_ts=time.time(),
            )
            with self._lock:
                self._leases.append(lease)
                if email:
                    self._by_email[email] = lease
            acquired += 1
        return acquired

    def try_take(self, preferred_email: str = "") -> _PooledLease | None:
        prefer = str(preferred_email or "").strip().lower()
        with self._lock:
            if prefer and prefer in self._by_email:
                lease = self._by_email.pop(prefer)
                try:
                    self._leases.remove(lease)
                except ValueError:
                    pass
                return lease
            if self._leases:
                lease = self._leases.popleft()
                if lease.email and self._by_email.get(lease.email) is lease:
                    self._by_email.pop(lease.email, None)
                return lease
        return None

    def return_lease(self, lease: _PooledLease) -> None:
        if not lease or not lease.access_token:
            return
        account_service.release_image_slot(lease.access_token)
        with self._lock:
            if lease in self._leases:
                return
            self._leases.append(lease)
            if lease.email:
                self._by_email[lease.email] = lease


account_lease_pool = AccountLeasePool()
