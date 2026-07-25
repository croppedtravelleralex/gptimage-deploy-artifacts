"""GPT-unavailable residential proxy quarantine (host:port / host).

Persisted at data/gpt_unavailable_proxies.json so dead endpoints are not
re-assigned during unique-proxy rebind / pool picks.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME_NS: int | None = None

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "gpt_unavailable_proxies.json"


def _default_path() -> Path:
    try:
        from services.config import DATA_DIR

        return Path(DATA_DIR) / "gpt_unavailable_proxies.json"
    except Exception:
        return DEFAULT_PATH


def proxy_endpoint_key(proxy: object) -> str:
    raw = str(proxy or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if not host:
            return raw.lower()
        port = parsed.port
        if port is None:
            port = 443 if (parsed.scheme or "").lower() == "https" else 80
        return f"{host}:{port}"
    except Exception:
        return raw.lower()


def _load(path: Path | None = None) -> dict[str, Any]:
    global _CACHE, _CACHE_MTIME_NS
    target = path or _default_path()
    with _LOCK:
        mtime_ns: int | None = None
        try:
            mtime_ns = target.stat().st_mtime_ns
        except OSError:
            _CACHE = {"endpoints": [], "notes": {}}
            _CACHE_MTIME_NS = None
            return dict(_CACHE)
        if _CACHE is not None and _CACHE_MTIME_NS == mtime_ns:
            return dict(_CACHE)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            payload = {"endpoints": [], "notes": {}}
        if not isinstance(payload, dict):
            payload = {"endpoints": [], "notes": {}}
        endpoints = payload.get("endpoints")
        if not isinstance(endpoints, list):
            endpoints = []
        notes = payload.get("notes")
        if not isinstance(notes, dict):
            notes = {}
        normalized = {
            "endpoints": [str(item).strip().lower() for item in endpoints if str(item).strip()],
            "notes": notes,
        }
        _CACHE = normalized
        _CACHE_MTIME_NS = mtime_ns
        return dict(normalized)


def list_quarantine_entries(path: Path | None = None) -> list[dict[str, Any]]:
    """Return quarantined endpoint records with notes (no credentials)."""
    data = _load(path)
    notes = data.get("notes") if isinstance(data.get("notes"), dict) else {}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in data.get("endpoints") or []:
        endpoint = str(raw or "").strip().lower()
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        note = notes.get(endpoint) if isinstance(notes.get(endpoint), dict) else {}
        if not note and ":" not in endpoint:
            for key, value in notes.items():
                if str(key).split(":", 1)[0].lower() == endpoint and isinstance(value, dict):
                    note = value
                    break
        out.append(
            {
                "endpoint": endpoint,
                "host": str(note.get("host") or endpoint.split(":", 1)[0]),
                "reason": str(note.get("reason") or "gpt_unavailable"),
                "former_account": note.get("former_account"),
            }
        )
    out.sort(key=lambda item: str(item.get("endpoint") or ""))
    return out


def list_gpt_unavailable_endpoints(path: Path | None = None) -> set[str]:
    data = _load(path)
    out: set[str] = set()
    for item in data.get("endpoints") or []:
        key = str(item or "").strip().lower()
        if not key:
            continue
        out.add(key)
        if ":" in key:
            out.add(key.split(":", 1)[0])
        else:
            out.add(key)
    return out


def is_gpt_unavailable_proxy(proxy: object, path: Path | None = None) -> bool:
    key = proxy_endpoint_key(proxy)
    if not key:
        return False
    blocked = list_gpt_unavailable_endpoints(path)
    if key in blocked:
        return True
    host = key.split(":", 1)[0]
    return host in blocked


def mark_gpt_unavailable(
    proxy: object,
    *,
    reason: str = "gpt_unavailable",
    former_account: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    """Append endpoint to quarantine file. Safe to call repeatedly."""
    target = path or _default_path()
    endpoint = proxy_endpoint_key(proxy)
    if not endpoint:
        raise ValueError("empty proxy endpoint")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        payload: dict[str, Any] = {"endpoints": [], "notes": {}}
        if target.exists():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                pass
        endpoints = [str(x).strip() for x in (payload.get("endpoints") or []) if str(x).strip()]
        if endpoint not in {e.lower() for e in endpoints}:
            endpoints.append(endpoint)
        notes = payload.get("notes") if isinstance(payload.get("notes"), dict) else {}
        notes[endpoint] = {
            "reason": str(reason or "gpt_unavailable"),
            "former_account": str(former_account or "").strip().lower() or None,
            "host": endpoint.split(":", 1)[0],
        }
        payload = {"endpoints": endpoints, "notes": notes}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        global _CACHE, _CACHE_MTIME_NS
        _CACHE = None
        _CACHE_MTIME_NS = None
        return payload


def clear_gpt_unavailable(proxy: object, path: Path | None = None) -> bool:
    """Remove endpoint from quarantine after a successful CF re-probe."""
    target = path or _default_path()
    endpoint = proxy_endpoint_key(proxy).lower()
    if not endpoint or not target.is_file():
        return False
    with _LOCK:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        endpoints = [str(x).strip() for x in (payload.get("endpoints") or []) if str(x).strip()]
        kept = [e for e in endpoints if e.lower() != endpoint and e.split(":", 1)[0].lower() != endpoint.split(":", 1)[0]]
        if len(kept) == len(endpoints):
            return False
        notes = payload.get("notes") if isinstance(payload.get("notes"), dict) else {}
        notes.pop(endpoint, None)
        host = endpoint.split(":", 1)[0]
        for key in list(notes.keys()):
            if str(key).split(":", 1)[0].lower() == host:
                notes.pop(key, None)
        payload = {"endpoints": kept, "notes": notes}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        global _CACHE, _CACHE_MTIME_NS
        _CACHE = None
        _CACHE_MTIME_NS = None
        return True
