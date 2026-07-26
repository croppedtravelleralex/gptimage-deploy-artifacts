#!/usr/bin/env python3
import json
import sys

sys.path.insert(0, "/app")
from services.account_service import account_service

account_service.reload_from_storage()
emails = [e.strip().lower() for e in sys.argv[1:]]
for email in emails:
    for a in account_service.list_accounts():
        if str(a.get("email", "")).lower() == email:
            print(
                json.dumps(
                    {
                        "email": email,
                        "quota": a.get("quota"),
                        "available_image_quota": account_service.available_image_quota_for_account(a),
                        "image_quota_state": account_service.image_quota_state(a),
                        "image_quota_unknown": a.get("image_quota_unknown"),
                        "panda_receive_state": a.get("panda_receive_state"),
                        "is_image_schedulable": account_service._is_image_account_schedulable(a),
                        "last_quota_refresh_at": a.get("last_quota_refresh_at"),
                        "limits_progress": a.get("limits_progress"),
                    },
                    ensure_ascii=False,
                )
            )
            break
