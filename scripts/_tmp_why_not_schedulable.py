from __future__ import annotations

import json
from collections import Counter

from services.account_service import account_service
from services import config as config_mod

cfg = config_mod.config

rows = []
for acc in account_service.list_accounts():
    email = str(acc.get("email") or "").strip()
    status = str(acc.get("status") or "")
    recv = str(acc.get("panda_receive_state") or "").strip().lower()
    scheduling_on = account_service.is_manual_scheduling_enabled(acc) and status == "正常"
    image_ok = account_service._is_image_account_schedulable(acc)
    reasons = []
    if not account_service._is_image_account_available(acc):
        reasons.append("not_available")
        # dig deeper
        if status != "正常":
            reasons.append(f"status={status}")
        q = acc.get("quota")
        if q is not None and int(q or 0) <= 0 and not account_service._is_true_unlimited_image_account(acc):
            reasons.append(f"quota={q}")
        if account_service._is_unknown_image_quota_account(acc):
            reasons.append("unknown_quota")
    if account_service._has_image_account_failure_evidence(acc):
        reasons.append("failure_evidence")
        if int(acc.get("invalid_count") or 0) > 0:
            reasons.append(f"invalid_count={acc.get('invalid_count')}")
        if acc.get("last_refresh_error"):
            reasons.append(f"last_refresh_error={str(acc.get('last_refresh_error'))[:80]}")
        if acc.get("last_token_refresh_error"):
            reasons.append(f"last_token_refresh_error={str(acc.get('last_token_refresh_error'))[:80]}")
        if int(acc.get("quota_refresh_fail_count") or 0) > 0:
            reasons.append(f"quota_refresh_fail_count={acc.get('quota_refresh_fail_count')}")
        if acc.get("quota_refresh_failure_kind"):
            reasons.append(f"quota_refresh_failure_kind={acc.get('quota_refresh_failure_kind')}")
        if acc.get("last_quota_refresh_error"):
            reasons.append(f"last_quota_refresh_error={str(acc.get('last_quota_refresh_error'))[:80]}")
    if account_service._requires_panda_receive_verification(acc):
        reasons.append(f"receive_state={recv or 'empty'}")
    if getattr(cfg, "image_require_recent_quota_refresh", False) and not account_service._is_recent_image_quota(acc):
        reasons.append("stale_quota_refresh")
        reasons.append(f"last_quota_refresh_at={acc.get('last_quota_refresh_at')}")
    if account_service._active_proxy_binding_duplicate(acc):
        reasons.append("dup_proxy_binding")
        reasons.append(f"proxy_binding_hash={str(acc.get('proxy_binding_hash') or '')[:12]}")
        reasons.append(f"proxy_ep={str(acc.get('proxy') or '').split('@')[-1]}")

    rows.append({
        "email": email,
        "status": status,
        "recv": recv,
        "quota": acc.get("quota"),
        "scheduling_on": scheduling_on,
        "image_schedulable": image_ok,
        "reasons": reasons,
        "proxy_ep": str(acc.get("proxy") or "").split("@")[-1],
        "soft_capped": bool(acc.get("image_soft_capped")),
    })

scheduling = [r for r in rows if r["scheduling_on"]]
image = [r for r in rows if r["image_schedulable"]]
gap = [r for r in scheduling if not r["image_schedulable"]]

print("TOTAL", len(rows))
print("SCHEDULING_ON", len(scheduling))
print("IMAGE_SCHEDULABLE", len(image))
print("GAP", len(gap))
print("--- GAP DETAIL ---")
for r in sorted(gap, key=lambda x: x["email"]):
    print(json.dumps(r, ensure_ascii=False))

print("--- REASON COUNTS ---")
c = Counter()
for r in gap:
    for reason in r["reasons"]:
        key = reason.split("=")[0]
        c[key] += 1
print(dict(c))

bd = account_service.get_schedulable_breakdown()
print("--- BREAKDOWN BUCKETS ---")
print(json.dumps(bd.get("buckets") or bd, ensure_ascii=False, default=str)[:2000])
