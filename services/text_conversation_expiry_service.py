"""Expire persisted upstream text conversations after TTL (default 14 days)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from services.config import config

logger = logging.getLogger(__name__)

_DEFAULT_TTL_DAYS = 14
_SWEEP_INTERVAL_SEC = 3600.0


class TextConversationExpiryService:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def ttl_days(self) -> int:
        try:
            raw = config.data.get("text_conversation_ttl_days", _DEFAULT_TTL_DAYS)
            return max(1, min(int(raw), 90))
        except (TypeError, ValueError):
            return _DEFAULT_TTL_DAYS

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="text-conversation-expiry", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(_SWEEP_INTERVAL_SEC):
            try:
                self.sweep_once()
            except Exception:
                logger.exception("text_conversation_expiry sweep failed")

    def sweep_once(self) -> dict[str, int]:
        from services.account_service import account_service

        ttl_days = self.ttl_days()
        cutoff = time.time() - ttl_days * 86400
        expired = 0
        deleted = 0
        with account_service._lock:
            tokens = list(account_service._accounts.keys())
        for token in tokens:
            account = account_service.get_account(token) or {}
            cid = str(account.get("text_conversation_id") or "").strip()
            if not cid:
                continue
            created_raw = str(account.get("text_conversation_created_at") or "").strip()
            created_ts = 0.0
            if created_raw:
                try:
                    created_ts = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    created_ts = 0.0
            if created_ts <= 0 or created_ts > cutoff:
                continue
            expired += 1
            if self._delete_upstream(token, cid):
                deleted += 1
            account_service.clear_text_conversation(token)
        return {"expired": expired, "deleted_upstream": deleted}

    @staticmethod
    def _delete_upstream(access_token: str, conversation_id: str) -> bool:
        try:
            from services.openai_backend_api import OpenAIBackendAPI

            backend = OpenAIBackendAPI(access_token=access_token)
            try:
                return bool(backend.delete_text_conversation(conversation_id))
            finally:
                backend.close()
        except Exception:
            logger.warning("upstream delete failed for cid=%s", conversation_id[:12], exc_info=True)
            return False


text_conversation_expiry_service = TextConversationExpiryService()
