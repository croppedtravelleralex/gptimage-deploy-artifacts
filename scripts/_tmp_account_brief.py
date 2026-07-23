#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))

from services.account_service import account_service

account_service.reload_from_storage()
rows = []
for token in account_service.list_tokens():
    a = account_service.get_account(token) or {}
    if not account_service._is_image_account_available(a):
        continue
    sched = account_service._is_image_account_schedulable(a)
    rows.append(
        {
            "email": a.get("email"),
            "quota": a.get("quota"),
            "image_quota_unknown": a.get("image_quota_unknown"),
            "proxy_hash": a.get("proxy_binding_hash"),
            "egress_ip": a.get("proxy_egress_ip"),
            "schedulable": sched,
            "success": a.get("success"),
            "cooldown": a.get("image_fail_cooldown_until"),
        }
    )
rows.sort(key=lambda r: (not r["schedulable"], str(r.get("quota") or "")))
print(json.dumps({"count": len(rows), "accounts": rows[:20]}, ensure_ascii=False, indent=2))
