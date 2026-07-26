#!/usr/bin/env python3
"""Diagnose inflight slots vs unfinished image tasks."""
from __future__ import annotations

import json

from services.account_service import account_service
from services.image_task_service import image_task_service, UNFINISHED_STATUSES


def main() -> int:
    accounts = account_service.list_accounts()
    inflight_accounts = [
        {
            "email": a.get("email"),
            "inflight": a.get("image_inflight"),
            "quota": a.get("quota"),
            "egress": a.get("proxy_egress_ip"),
        }
        for a in accounts
        if int(a.get("image_inflight") or 0) > 0
    ]
    unfinished = []
    for key, task in image_task_service._tasks.items():
        if task.get("status") not in UNFINISHED_STATUSES:
            continue
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        unfinished.append(
            {
                "key": key,
                "status": task.get("status"),
                "progress": task.get("progress"),
                "preferred": payload.get("preferred_account_email"),
                "error": task.get("error"),
                "updated_at": task.get("updated_at"),
            }
        )
    print(
        json.dumps(
            {
                "inflight_accounts": inflight_accounts,
                "inflight_total": account_service.get_total_image_inflight(),
                "unfinished_tasks": unfinished,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
