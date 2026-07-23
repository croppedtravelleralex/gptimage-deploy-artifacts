#!/usr/bin/env python3
"""Scan Outlook credential file for mailbox preflight OK."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.outlook_camoufox_stable_register import load_outlook_line, preflight_mailbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts-file", required=True)
    ap.add_argument("--skip-indices", default="")
    ap.add_argument("--skip-emails", default="")
    ap.add_argument("--max-scan", type=int, default=30)
    args = ap.parse_args()

    skip_idx = {int(x) for x in str(args.skip_indices or "").split(",") if x.strip().isdigit()}
    skip_emails = {x.strip().lower() for x in str(args.skip_emails or "").split(",") if x.strip()}
    path = Path(args.accounts_file)
    lines = [l for l in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if l.strip()]
    results = []
    for index in range(min(len(lines), args.max_scan)):
        if index in skip_idx:
            continue
        try:
            cred = load_outlook_line(path, index)
        except Exception as exc:
            results.append({"index": index, "ok": False, "stage": "load", "error": str(exc)[:120]})
            continue
        email = str(cred.get("email") or "").strip().lower()
        if email in skip_emails:
            results.append({"index": index, "email": email, "ok": False, "stage": "skip", "error": "in_pool"})
            continue
        try:
            preflight_mailbox(cred, "")
            results.append({"index": index, "email": email, "ok": True})
        except Exception as exc:
            results.append({"index": index, "email": email, "ok": False, "stage": "mailbox", "error": str(exc)[:160]})
    good = [r for r in results if r.get("ok")]
    print(json.dumps({"good": good[:5], "scanned": len(results), "all": results}, ensure_ascii=False, indent=2))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
