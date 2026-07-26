#!/usr/bin/env python3
"""Diagnose why schedulable accounts are excluded from ready candidate pool."""
from __future__ import annotations

import json

from services.account_service import account_service
from services.config import config


def main() -> int:
    ready = account_service._list_ready_candidate_tokens()
    all_acc = account_service.list_accounts()
    sched = [a for a in all_acc if a.get("image_schedulable")]
    sched_emails = {str(a.get("email") or "").lower() for a in sched}
    ready_emails: set[str] = set()
    for tok in ready:
        acc = account_service.get_account(tok) or {}
        ready_emails.add(str(acc.get("email") or "").lower())

    try:
        from services.account_warmup_service import account_warmup_service

        hot = {str(e).strip().lower() for e in account_warmup_service.hot_emails()}
    except Exception:
        hot = set()

    not_ready = sorted(sched_emails - ready_emails)
    out = {
        "dispatch_hot_only": config.dispatch_hot_only,
        "hot_count": len(hot),
        "image_require_recent_quota_refresh": config.image_require_recent_quota_refresh,
        "ready_count": len(ready),
        "schedulable_count": len(sched_emails),
        "gap": len(not_ready),
        "excluded": [],
    }
    for email in not_ready:
        acc = next(a for a in all_acc if str(a.get("email") or "").lower() == email)
        tok = str(acc.get("access_token") or "")
        reasons: list[str] = []
        if config.dispatch_hot_only and hot and email not in hot:
            reasons.append("not_hot")
        if not account_service._is_image_interval_ready(acc):
            reasons.append("interval_not_ready")
        if account_service._cohort_paused(acc):
            reasons.append("cohort_paused")
        if account_service._is_warmup_dispatch_blocked(acc):
            reasons.append("warmup_blocked")
        if account_service._is_image_preflight_backed_off(tok):
            reasons.append("preflight_backoff")
        recent = account_service._is_recent_image_quota(acc)
        lazy = account_service._quota_window_due_for_lazy_refresh(acc)
        if config.image_require_recent_quota_refresh and not recent and not lazy:
            reasons.append("quota_not_recent")
        if not reasons:
            reasons.append("unknown")
        out["excluded"].append(
            {
                "email": email,
                "reasons": reasons,
                "quota": acc.get("quota"),
                "inflight": acc.get("image_inflight"),
                "image_next_ok_at": acc.get("image_next_ok_at"),
                "last_used_at": acc.get("last_used_at"),
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
