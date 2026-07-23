#!/usr/bin/env python3
"""Proton + Camoufox via Panda IP; persist with empty account proxy."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts._tmp_proton_camoufox_openai_observe as base  # noqa: E402
from services.register.real_browser_register import mask_email  # noqa: E402

SOURCE_DETAIL = "proton_camoufox_panda_ip_direct_20260720"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proton-email", required=True)
    ap.add_argument("--proton-password", required=True)
    ap.add_argument("--browser-proxy", default="socks5://127.0.0.1:18443")
    ap.add_argument("--mail-proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "runlogs" / "proton-panda-ip-20260720"))
    args = ap.parse_args()

    out = base.register_one(
        proton_email=args.proton_email,
        proton_password=args.proton_password,
        sticky_proxy=args.browser_proxy,
        mail_proxy=args.mail_proxy,
        out_dir=Path(args.out_dir),
        browser_proxy=args.browser_proxy,
    )
    if not out.get("ok"):
        return 1

    try:
        from services.account_service import account_service

        export = (out.get("export") or {}).get("path")
        token = ""
        if export and Path(export).exists():
            blob = json.loads(Path(export).read_text(encoding="utf-8"))
            token = str(blob.get("access_token") or "")
            blob["proxy"] = ""
            blob["proxy_provider"] = ""
            blob["proxy_scope"] = "panda_direct"
            blob["lifecycle_ip_mode"] = "panda_host_direct"
            blob["registration_proxy_scope"] = "panda_host_direct_chain"
            blob["source_detail"] = SOURCE_DETAIL
            Path(export).write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        if token:
            account_service.update_account(
                token,
                {
                    "proxy": "",
                    "proxy_provider": "",
                    "proxy_scope": "panda_direct",
                    "lifecycle_ip_mode": "panda_host_direct",
                    "registration_proxy_scope": "panda_host_direct_chain",
                    "source_detail": SOURCE_DETAIL,
                },
                quiet=True,
            )
            print(
                json.dumps(
                    {"post_clear_proxy": True, "email_mask": mask_email(args.proton_email)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps({"post_clear_proxy_error": f"{type(exc).__name__}:{exc}"[:200]}, ensure_ascii=False),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
