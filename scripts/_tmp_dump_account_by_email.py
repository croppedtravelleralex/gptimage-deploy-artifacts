#!/usr/bin/env python3
"""Dump one account by email from Panda SQLite."""
from __future__ import annotations

import json
import sqlite3
import sys

email = (sys.argv[1] if len(sys.argv) > 1 else "ivetterock54353@outlook.com").strip().lower()
con = sqlite3.connect("/root/gptimage/data/accounts.db")
con.row_factory = sqlite3.Row
hit = None
token = None
for r in con.execute("select access_token, data from accounts"):
    d = json.loads(r["data"] or "{}")
    if str(d.get("email") or "").strip().lower() == email:
        hit = d
        token = r["access_token"]
        break
print("FOUND", bool(hit))
if not hit:
    raise SystemExit(1)
for k in [
    "email",
    "status",
    "quota",
    "plan_type",
    "scheduling_enabled",
    "lifecycle",
    "lifecycle_state",
    "verified_ready",
    "identity_isolated",
    "restore_at",
    "image_fail_streak",
    "success",
    "fail",
    "last_used_at",
    "last_quota_refresh_at",
    "proxy_provider",
    "proxy_egress_ip",
    "register_egress_ip",
    "proxy_binding_hash",
]:
    print(f"{k}={hit.get(k)!r}")
print("---errors---")
for k, v in sorted(hit.items()):
    lk = k.lower()
    if any(x in lk for x in ("error", "abnormal", "403", "cf_", "refresh", "panda_", "disabled", "taint", "reason")):
        s = str(v)
        print(f"{k}={(s[:300] + '...') if len(s) > 300 else s}")
px = str(hit.get("proxy") or "")
print("proxy_prefix=", px[:70])
print("token_len=", len(str(token or "")))
