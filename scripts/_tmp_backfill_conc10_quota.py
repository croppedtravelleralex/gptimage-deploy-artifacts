#!/usr/bin/env python3
"""Backfill local quota decrement for a completed conc10 run (async task API)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONC10_EMAILS = [
    "agustinkelly59361@outlook.com",
    "alexandradonald2005@outlook.com",
    "alexnnnmmm@proton.me",
    "alvinian4635@outlook.com",
    "ameliaabraham68376@outlook.com",
    "aspenvincent99941@outlook.com",
    "barneyharry7891@outlook.com",
    "barthcherry24674@outlook.com",
    "blakekyle5108@outlook.com",
    "davidlynn8783@outlook.com",
]


def _load_emails_from_report(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    emails: list[str] = []
    for item in data.get("submits") or []:
        email = str(item.get("preferred_account_email") or "").strip()
        if email:
            emails.append(email)
    return emails or list(CONC10_EMAILS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="", help="conc10 report json path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fetch-remote", action="store_true", help="pull remote quota after mark")
    args = ap.parse_args()

    emails = _load_emails_from_report(Path(args.report)) if args.report else list(CONC10_EMAILS)
    emails = sorted({e for e in emails if e})

    from services.account_service import account_service

    email_to_token: dict[str, str] = {}
    for token, acc in account_service._accounts.items():
        email = str(acc.get("email") or "").strip()
        if email in emails:
            email_to_token[email] = token

    rows: list[dict] = []
    for email in emails:
        token = email_to_token.get(email)
        if not token:
            rows.append({"email": email, "ok": False, "error": "account_not_found"})
            continue
        before = dict(account_service.get_account(token) or {})
        before_q = int(before.get("quota") or 0)
        unknown = bool(before.get("image_quota_unknown"))
        unlimited = account_service._is_true_unlimited_image_account(before)
        if args.dry_run:
            rows.append(
                {
                    "email": email,
                    "dry_run": True,
                    "quota_before": before_q,
                    "image_quota_unknown": unknown,
                    "true_unlimited": unlimited,
                    "would_decrement": not unknown and not unlimited and before_q > 0,
                }
            )
            continue
        if unknown or unlimited:
            rows.append(
                {
                    "email": email,
                    "ok": True,
                    "skipped": True,
                    "reason": "unknown_or_unlimited",
                    "quota": before_q,
                    "image_quota_unknown": unknown,
                }
            )
            continue
        account_service.mark_image_result(token, True)
        after = dict(account_service.get_account(token) or {})
        after_q = int(after.get("quota") or 0)
        remote_q = None
        remote_err = None
        if args.fetch_remote:
            try:
                remote = account_service.fetch_remote_info(token, "conc10_quota_backfill")
                if remote:
                    remote_q = int(remote.get("quota") or 0)
            except Exception as exc:
                remote_err = str(exc)[:200]
        rows.append(
            {
                "email": email,
                "ok": True,
                "quota_before": before_q,
                "quota_after_mark": after_q,
                "delta": after_q - before_q,
                "quota_remote": remote_q,
                "remote_error": remote_err,
            }
        )

    out = {"emails": emails, "found": len(email_to_token), "rows": rows}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    failed = sum(1 for r in rows if not r.get("ok", True) and not r.get("dry_run"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
