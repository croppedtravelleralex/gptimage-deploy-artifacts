"""脱敏请求形状遥测：只记录字段集合与 hash，不记录正文/token/Cookie。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


_SENSITIVE_HEADER_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "oai-device-id",
    "oai-language",
    "openai-sentinel-chat-requirements-token",
    "openai-sentinel-proof-token",
    "openai-sentinel-turnstile-token",
    "x-conduit-token",
}


def header_shape(headers: dict[str, Any] | None) -> dict[str, Any]:
    items = {str(k).lower(): str(v or "") for k, v in dict(headers or {}).items()}
    names = sorted(items)
    redacted = {k: ("<redacted>" if k in _SENSITIVE_HEADER_KEYS else f"len:{len(items[k])}") for k in names}
    raw = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    return {
        "header_names": names,
        "header_count": len(names),
        "shape_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "bytes": len(raw),
    }


def body_shape(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {"keys": [], "key_count": 0, "shape_hash": "", "bytes": 0}
    if isinstance(payload, (bytes, bytearray)):
        digest = hashlib.sha256(payload).hexdigest()[:16]
        return {"keys": ["<bytes>"], "key_count": 1, "shape_hash": digest, "bytes": len(payload)}
    if isinstance(payload, str):
        data = payload.encode("utf-8")
        return {
            "keys": ["<str>"],
            "key_count": 1,
            "shape_hash": hashlib.sha256(data).hexdigest()[:16],
            "bytes": len(data),
        }
    if isinstance(payload, dict):
        keys = sorted(str(k) for k in payload.keys())
        raw = json.dumps({"keys": keys, "n": len(keys)}, ensure_ascii=False, sort_keys=True)
        return {
            "keys": keys,
            "key_count": len(keys),
            "shape_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            "bytes": len(raw),
        }
    raw = str(type(payload).__name__).encode("utf-8")
    return {
        "keys": [type(payload).__name__],
        "key_count": 1,
        "shape_hash": hashlib.sha256(raw).hexdigest()[:16],
        "bytes": len(raw),
    }
