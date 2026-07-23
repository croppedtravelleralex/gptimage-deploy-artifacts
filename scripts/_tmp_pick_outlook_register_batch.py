#!/usr/bin/env python3
"""Pick unused Outlook lines + fresh Webshare nodes for stable Camoufox register."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.outlook_mail import parse_outlook_credentials


def load_used_emails(paths: list[Path]) -> set[str]:
    used: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix == ".db":
            import sqlite3

            con = sqlite3.connect(path)
            for (raw,) in con.execute("select data from accounts"):
                try:
                    d = json.loads(raw or "{}")
                except Exception:
                    continue
                email = str(d.get("email") or "").strip().lower()
                if email:
                    used.add(email)
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for line in text.splitlines():
            if "@" in line:
                used.add(line.split("----", 1)[0].strip().lower())
    return used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts-file", required=True)
    ap.add_argument("--used-email-file", action="append", default=[])
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--skip-indices", default="")
    args = ap.parse_args()

    skip = {int(x) for x in str(args.skip_indices or "").split(",") if str(x).strip().isdigit()}
    used = load_used_emails([Path(p) for p in args.used_email_file])
    creds = parse_outlook_credentials(Path(args.accounts_file).read_text(encoding="utf-8-sig", errors="ignore"))
    picked: list[dict] = []
    for index, cred in enumerate(creds):
        if index in skip:
            continue
        email = str(cred.get("email") or "").strip().lower()
        if not email or email in used:
            continue
        picked.append({"index": index, "email": email})
        if len(picked) >= max(1, int(args.count)):
            break
    print(json.dumps({"picked": picked, "used_count": len(used)}, ensure_ascii=False, indent=2))
    return 0 if picked else 1


if __name__ == "__main__":
    raise SystemExit(main())
