#!/usr/bin/env python3
"""Probe sticky proxy CF for conc10-failed accounts; quarantine bad egress."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EMAILS = [
    "felicitypamela2673@outlook.com",
    "ivorbrown70573@outlook.com",
    "qaflow0ytb7bbp0z@proton.me",
    "qaflowfbdb3ovksr@proton.me",
    "qaflowgq5wyuxhe9@proton.me",
    "qaflowud630wbo2a@proton.me",
    "blakekyle5108@outlook.com",
]


def main() -> int:
    from services.account_service import account_service
    from services.proxy_cf_probe import probe_proxy_cf
    from services.proxy_quarantine import (
        is_gpt_unavailable_proxy,
        mark_gpt_unavailable,
        proxy_endpoint_key,
    )

    account_service.reload_from_storage()
    quarantine_path = ROOT / "data" / "gpt_unavailable_proxies.json"

    by_email: dict[str, dict] = {}
    for item in account_service.list_accounts():
        em = str(item.get("email") or "").strip().lower()
        if em:
            by_email[em] = item

    print("email\tegress_ip\tcf_ok\tlatency_ms\taction_taken")
    for email in EMAILS:
        email_l = email.strip().lower()
        acc = by_email.get(email_l)
        if not acc:
            print(f"{email_l}\t\tfalse\t0\taccount_not_found")
            continue
        proxy = str(acc.get("proxy") or acc.get("proxy_url") or "").strip()
        if not proxy:
            print(f"{email_l}\t\tfalse\t0\tno_proxy")
            continue
        probe = probe_proxy_cf(proxy, timeout=45.0)
        egress = probe.get("egress") if isinstance(probe.get("egress"), dict) else {}
        egress_ip = str(egress.get("ip") or acc.get("proxy_egress_ip") or "")
        cf_ok = bool(probe.get("ok"))
        latency_ms = int(probe.get("elapsed_ms") or 0)
        if cf_ok:
            action = "none"
        elif is_gpt_unavailable_proxy(proxy, path=quarantine_path):
            action = "already_quarantined"
        else:
            reason = str(probe.get("cf_classification") or "cf_probe_fail")
            if probe.get("error"):
                reason = f"{reason}:{str(probe.get('error'))[:80]}"
            mark_gpt_unavailable(
                proxy,
                reason=reason,
                former_account=email_l,
                path=quarantine_path,
            )
            action = f"quarantined:{proxy_endpoint_key(proxy)}"
        print(f"{email_l}\t{egress_ip}\t{str(cf_ok).lower()}\t{latency_ms}\t{action}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
