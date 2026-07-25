from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.account_service import account_service


class AccountLease(Protocol):
    @property
    def access_token(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def email(self) -> str: ...

    def release(self) -> None: ...


@dataclass
class _Lease:
    access_token: str
    account_id: str
    email: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        account_service.release_image_slot(self.access_token)


class StageAccountProvider:
    """Preferred-different-account selection for sS; small-pool fallback."""

    SMALL_POOL_THRESHOLD = 3
    SAME_ACCOUNT_BACKOFF_SECS = 3.0

    def __init__(self) -> None:
        self._ps_tokens: set[str] = set()
        self._ps_account_ids: set[str] = set()

    def note_ps_account(self, *, access_token: str, account_id: str = "", email: str = "") -> None:
        token = str(access_token or "").strip()
        if token:
            self._ps_tokens.add(token)
        account_key = str(account_id or email or "").strip()
        if account_key:
            self._ps_account_ids.add(account_key)

    def _dispatchable_count(self) -> int:
        try:
            stats = account_service.get_image_candidate_runtime_stats()
            return int(stats.get("dispatchable_candidate_count") or 0)
        except Exception:
            return 0

    def _build_exclude_tokens(self) -> set[str]:
        if self._dispatchable_count() >= self.SMALL_POOL_THRESHOLD:
            return set(self._ps_tokens)
        return set()

    def acquire_for_ss(
        self,
        *,
        plan_type: str | None = None,
        source_type: str | None = None,
        plan_types: set[str] | tuple[str, ...] | None = None,
        skip_global_limit: bool = False,
        preferred_email: str = "",
    ) -> _Lease:
        from services.image_pipeline.account_lease_pool import account_lease_pool

        pooled = account_lease_pool.try_take(preferred_email)
        if pooled is not None:
            return _Lease(
                access_token=pooled.access_token,
                account_id=pooled.account_id,
                email=pooled.email,
            )
        exclude = self._build_exclude_tokens()
        token = account_service.get_available_access_token(
            plan_type=plan_type,
            source_type=source_type,
            plan_types=plan_types,
            skip_global_limit=skip_global_limit,
            preferred_email=preferred_email,
            excluded_tokens=exclude or None,
        )
        account = account_service.get_account(token) or {}
        email = str(account.get("email") or "").strip()
        account_id = email or str(account.get("id") or token[:12])
        if exclude and token in self._ps_tokens:
            try:
                account_service.record_image_transient_backoff(
                    token,
                    "pipeline same-account ps/ss fallback",
                )
            except Exception:
                pass
        return _Lease(access_token=token, account_id=account_id, email=email)

    def acquire_for_ps(
        self,
        *,
        plan_type: str | None = None,
        skip_global_limit: bool = False,
    ) -> _Lease:
        token = account_service.get_available_access_token(
            plan_type=plan_type,
            skip_global_limit=skip_global_limit,
        )
        account = account_service.get_account(token) or {}
        email = str(account.get("email") or "").strip()
        account_id = email or str(account.get("id") or token[:12])
        self.note_ps_account(access_token=token, account_id=account_id, email=email)
        return _Lease(access_token=token, account_id=account_id, email=email)
