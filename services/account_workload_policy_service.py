"""Production wiring for AccountWorkloadPolicy (shadow | live)."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from services.account_workload_policy import (
    AccountWorkloadCapabilities,
    WorkloadAction,
    WorkloadDecision,
    WorkloadSnapshot,
    decide_account_workload,
    image_reserve_count,
)

logger = logging.getLogger(__name__)

Purpose = Literal["text", "image"]


def _token_hash(token: object, length: int = 16) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def account_node_bound(account: dict[str, Any] | None) -> bool:
    """Soft node lease: missing lease → bound (legacy); expired lease → unbound."""
    if not isinstance(account, dict):
        return True
    lease_id = str(account.get("node_lease_id") or "").strip()
    bound_until = str(account.get("node_bound_until") or "").strip()
    if not lease_id and not bound_until:
        # No soft lease recorded yet → treat as bound for backward compatibility.
        return bool(str(account.get("proxy") or "").strip()) or True
    if not bound_until:
        return bool(lease_id)
    try:
        until = datetime.fromisoformat(bound_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class WorkloadGateResult:
    admitted: bool
    decision: WorkloadDecision
    mode: str
    reason: str
    canary_exempt: bool = False


class AccountWorkloadPolicyService:
    def __init__(self) -> None:
        self._text_inflight = 0

    def settings(self) -> dict[str, Any]:
        from services.config import config

        return dict(config.get_workload_settings())

    @property
    def mode(self) -> str:
        settings = self.settings()
        configured = str(settings.get("mode") or "shadow").strip().lower()
        if configured == "live":
            return "live"
        auto_min = int(settings.get("auto_live_min_ready") or 0)
        if auto_min <= 0:
            return configured if configured in {"shadow", "live"} else "shadow"
        try:
            snapshot = self.build_snapshot()
            ready = int(snapshot.dispatchable_image_accounts or 0)
        except Exception:
            ready = 0
        if ready >= auto_min:
            return "live"
        return "shadow"

    @property
    def text_queue_mode(self) -> str:
        return str(self.settings().get("text_queue_mode") or "off").strip().lower()

    def canary_hashes(self) -> set[str]:
        raw = self.settings().get("canary_token_hashes") or []
        return {str(item).strip().lower() for item in raw if str(item).strip()}

    def is_canary(self, account: dict[str, Any] | None, access_token: str = "") -> bool:
        hashes = self.canary_hashes()
        if not hashes:
            return False
        token = access_token or str((account or {}).get("access_token") or "")
        th = _token_hash(token, 16)
        stored = str((account or {}).get("token_hash") or "").strip().lower()
        return th.lower() in hashes or stored[:16].lower() in hashes or stored.lower() in hashes

    def build_snapshot(self, *, force_text_demand: bool = False) -> WorkloadSnapshot:
        from services.account_service import account_service
        from services.image_task_service import image_task_service
        from services.text_task_queue import text_task_queue

        accounts = account_service.list_accounts()
        dispatchable = 0
        free = 0
        for account in accounts:
            if not account_service._is_image_account_schedulable(account):
                continue
            dispatchable += 1
            inflight = int(account.get("image_inflight") or 0)
            if inflight <= 0:
                free += 1

        try:
            image_depth = int(image_task_service.queue_depth() if hasattr(image_task_service, "queue_depth") else 0)
        except Exception:
            image_depth = 0
        if image_depth <= 0:
            try:
                stats = getattr(image_task_service, "stats", None)
                if callable(stats):
                    payload = stats() or {}
                    image_depth = int(payload.get("queued") or payload.get("pending") or 0)
            except Exception:
                image_depth = 0

        text_depth = text_task_queue.depth()
        if force_text_demand and text_depth == 0:
            text_depth = 1
        return WorkloadSnapshot(
            dispatchable_image_accounts=dispatchable,
            free_image_accounts=min(free, dispatchable),
            image_queue=max(0, image_depth),
            text_queue=max(0, text_depth),
        )

    def capabilities_for(self, account: dict[str, Any] | None) -> AccountWorkloadCapabilities:
        from services.account_service import account_service

        account = account or {}
        status = str(account.get("status") or "")
        text_healthy = status not in {"禁用", "异常"}
        image_eligible = bool(account_service._is_image_account_schedulable(account))
        return AccountWorkloadCapabilities(
            text_healthy=text_healthy,
            image_eligible=image_eligible,
            node_bound=account_node_bound(account),
        )

    def decide_for_account(
        self,
        account: dict[str, Any] | None,
        purpose: Purpose,
        *,
        access_token: str = "",
        force_text_demand: bool = False,
    ) -> WorkloadGateResult:
        account = account or {}
        canary = self.is_canary(account, access_token=access_token)
        snapshot = self.build_snapshot(force_text_demand=force_text_demand or purpose == "text")
        caps = self.capabilities_for(account)
        decision = decide_account_workload(
            snapshot,
            caps,
            allowlist_rimg_exempt=canary and purpose == "text",
        )
        mode = self.mode
        admitted = True
        reason = decision.reason

        if purpose == "text":
            text_ok = decision.action == WorkloadAction.TEXT
            if mode == "live" and not text_ok:
                admitted = False
                reason = decision.reason
            elif mode == "shadow" and not text_ok:
                logger.info(
                    {
                        "event": "workload_shadow_diff",
                        "purpose": "text",
                        "would_block": True,
                        "reason": decision.reason,
                        "image_reserve": decision.image_reserve,
                        "D": snapshot.dispatchable_image_accounts,
                        "F": snapshot.free_image_accounts,
                        "canary": canary,
                    }
                )
        else:
            if mode == "live" and not caps.node_bound:
                admitted = False
                reason = "node_not_bound"
            elif mode == "live" and decision.reason == "node_not_bound":
                admitted = False
            elif mode == "shadow" and not caps.node_bound:
                logger.info(
                    {
                        "event": "workload_shadow_diff",
                        "purpose": "image",
                        "would_block": True,
                        "reason": "node_not_bound",
                        "canary": canary,
                    }
                )

        return WorkloadGateResult(
            admitted=admitted,
            decision=decision,
            mode=mode,
            reason=reason,
            canary_exempt=canary and decision.reason == "canary_rimg_exempt",
        )


account_workload_policy_service = AccountWorkloadPolicyService()
