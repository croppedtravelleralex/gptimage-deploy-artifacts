"""Cache successful text /f/conversation/prepare per account + conversation."""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from services.config import config


@dataclass
class _PrepareEntry:
    expires_at: float


class ChatPrepareCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, _PrepareEntry] = {}

    def _ttl_secs(self) -> float:
        try:
            settings = config.get_image_pipeline_settings()
            return max(30.0, float(settings.get("chat_prepare_cache_ttl_secs") or 120))
        except Exception:
            return 120.0

    @staticmethod
    def _key(access_token: str, conversation_id: str, model: str) -> str:
        cid = str(conversation_id or "").strip() or "_new_"
        model_slug = str(model or "auto").strip().lower()
        raw = f"{access_token}\n{cid}\n{model_slug}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, access_token: str, *, conversation_id: str = "", model: str = "auto") -> bool:
        token = str(access_token or "").strip()
        if not token:
            return False
        key = self._key(token, conversation_id, model)
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or entry.expires_at <= now:
                if entry is not None:
                    self._cache.pop(key, None)
                return False
            return True

    def put(self, access_token: str, *, conversation_id: str = "", model: str = "auto") -> None:
        token = str(access_token or "").strip()
        if not token:
            return
        key = self._key(token, conversation_id, model)
        ttl = self._ttl_secs()
        with self._lock:
            self._cache[key] = _PrepareEntry(expires_at=time.time() + ttl)

    def invalidate_token(self, access_token: str) -> None:
        token = str(access_token or "").strip()
        if not token:
            return
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if not k.startswith(token)}

    def invalidate_all(self) -> None:
        with self._lock:
            self._cache.clear()


chat_prepare_cache = ChatPrepareCache()
