"""Browser chat session → account allocation (1:1 / least-loaded / degrade)."""
from __future__ import annotations

import threading
import time
from typing import Any

from services.config import config


class ChatSessionService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_by_email: dict[str, int] = {}
        self._session_email: dict[str, str] = {}

    def _text_candidates(self, account_service: Any) -> list[dict[str, Any]]:
        from services.account_workload_policy_service import account_workload_policy_service

        with account_service._lock:
            raw = [
                dict(item)
                for item in account_service._accounts.values()
                if item.get("status") not in {"禁用", "异常"}
                and str(item.get("access_token") or "").strip()
                and account_service._is_text_interval_ready(item)
                and not account_service._cohort_paused(item)
            ]
        snapshot = account_workload_policy_service.build_snapshot(force_text_demand=True)
        candidates: list[dict[str, Any]] = []
        for account in raw:
            token = str(account.get("access_token") or "")
            gate = account_workload_policy_service.decide_for_account(
                account,
                "text",
                access_token=token,
                force_text_demand=True,
                snapshot=snapshot,
            )
            if gate.mode == "live" and not gate.admitted:
                continue
            candidates.append(account)
        return candidates

    def count_text_ready(self, account_service: Any) -> int:
        return len(self._text_candidates(account_service))

    def allocate(self, account_service: Any, *, session_id: str = "") -> dict[str, Any]:
        sid = str(session_id or "").strip() or f"anon-{int(time.time() * 1000)}"
        with self._lock:
            prev = self._session_email.pop(sid, None)
            if prev:
                self._active_by_email[prev] = max(0, int(self._active_by_email.get(prev, 0)) - 1)
                if self._active_by_email.get(prev, 0) <= 0:
                    self._active_by_email.pop(prev, None)

        candidates = self._text_candidates(account_service)
        if not candidates:
            token = account_service.get_text_access_token()
            if not token:
                return {"ok": False, "error": "no_text_account", "mode": "none"}
            email = ""
            for account in account_service.list_accounts():
                if str(account.get("access_token") or "") == token:
                    email = str(account.get("email") or "").strip()
                    break
            self._ensure_chat_flags(account_service, token)
            return {
                "ok": True,
                "email": email,
                "access_token": token,
                "mode": "degraded",
            }

        with self._lock:
            active = dict(self._active_by_email)

        def email_key(acc: dict) -> str:
            return str(acc.get("email") or "").strip().lower()

        free = [c for c in candidates if active.get(email_key(c), 0) == 0]
        if free:
            chosen = free[0]
            mode = "exclusive"
        else:
            chosen = min(candidates, key=lambda c: (active.get(email_key(c), 0), email_key(c)))
            mode = "shared"

        email = email_key(chosen)
        token = str(chosen.get("access_token") or "")
        with self._lock:
            self._active_by_email[email] = int(self._active_by_email.get(email, 0)) + 1
            self._session_email[sid] = email

        self._ensure_chat_flags(account_service, token)
        return {
            "ok": True,
            "email": email,
            "access_token": token,
            "mode": mode,
            "session_id": sid,
        }

    def release(self, *, session_id: str = "", email: str = "") -> None:
        sid = str(session_id or "").strip()
        mail = str(email or "").strip().lower()
        with self._lock:
            if sid and sid in self._session_email:
                mail = self._session_email.pop(sid)
            if not mail:
                return
            self._active_by_email[mail] = max(0, int(self._active_by_email.get(mail, 0)) - 1)
            if self._active_by_email.get(mail, 0) <= 0:
                self._active_by_email.pop(mail, None)

    @staticmethod
    def _ensure_chat_flags(account_service: Any, access_token: str) -> None:
        if not access_token:
            return
        with account_service._lock:
            token = account_service._resolve_access_token_locked(access_token)
            current = account_service._accounts.get(token)
            if current is None:
                return
            next_item = dict(current)
            changed = False
            if bool(getattr(config, "text_chat_persist_history", False)):
                if not next_item.get("chat_persist_history"):
                    next_item["chat_persist_history"] = True
                    changed = True
            if bool(getattr(config, "text_chat_reuse_conversation", False)):
                if not next_item.get("chat_reuse_conversation"):
                    next_item["chat_reuse_conversation"] = True
                    changed = True
            if not changed:
                return
            account = account_service._normalize_account(next_item)
            if account is None:
                return
            account_service._accounts[token] = account
            account_service._persist_upsert_accounts([account])


chat_session_service = ChatSessionService()
