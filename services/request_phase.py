"""Unified request phase timing for text and image upstream flows."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any


PHASES = (
    "preflight",
    "node_connect",
    "auth",
    "request_build",
    "upstream_submit",
    "sse_ready",
    "conversation_started",
    "poll",
    "result_resolve",
    "download",
    "downstream_write",
    "cleanup",
)


def short_hash(value: object, length: int = 12) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


class RequestPhaseTracker:
    def __init__(
        self,
        *,
        request_id: str | None = None,
        account_token: str = "",
        node_proxy: str = "",
        conversation_id: str = "",
        purpose: str = "",
    ) -> None:
        self.request_id = str(request_id or uuid.uuid4())
        self.account_hash = short_hash(account_token, 12)
        self.node_hash = short_hash(node_proxy, 12)
        self.conversation_id = str(conversation_id or "")
        self.purpose = str(purpose or "")
        self.phase = "preflight"
        self.failed_phase: str | None = None
        self._started = time.perf_counter()
        self._last = self._started
        self._marks: dict[str, float] = {"preflight": self._started}

    def mark(self, phase: str) -> dict[str, Any]:
        name = str(phase or "").strip() or "preflight"
        now = time.perf_counter()
        since_last_ms = int((now - self._last) * 1000)
        elapsed_ms = int((now - self._started) * 1000)
        self.phase = name
        self._marks[name] = now
        self._last = now
        return self.as_log_dict(since_last_ms=since_last_ms, elapsed_ms=elapsed_ms)

    def fail(self, phase: str | None = None, error_type: str = "") -> dict[str, Any]:
        if phase:
            self.mark(phase)
        self.failed_phase = self.phase
        payload = self.as_log_dict()
        if error_type:
            payload["error_type"] = str(error_type)[:120]
        return payload

    def as_log_dict(
        self,
        *,
        since_last_ms: int | None = None,
        elapsed_ms: int | None = None,
    ) -> dict[str, Any]:
        now = time.perf_counter()
        return {
            "event": "request_phase",
            "request_id": self.request_id,
            "account_hash": self.account_hash,
            "node_hash": self.node_hash,
            "conversation_id": self.conversation_id or None,
            "purpose": self.purpose or None,
            "phase": self.phase,
            "elapsed_ms": int((now - self._started) * 1000) if elapsed_ms is None else elapsed_ms,
            "since_last_ms": 0 if since_last_ms is None else since_last_ms,
            "failed_phase": self.failed_phase,
        }
