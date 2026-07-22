"""Canonical ChatGPT web request builder for chat and image upstream calls.

All outbound conversation / picture payloads and sentinel headers should be
built here so shape telemetry and fail-fast conduit checks stay consistent.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]


def new_uuid() -> str:
    return str(uuid4())


def require_conduit_token(conduit_token: object) -> str:
    token = str(conduit_token or "").strip()
    if not token:
        raise RuntimeError("missing conduit_token")
    return token


def timezone_offset_min(tz_name: str, fallback: int = -480) -> int:
    """JS getTimezoneOffset() style: minutes to add to local to get UTC."""
    name = str(tz_name or "").strip() or "Asia/Shanghai"
    if ZoneInfo is None:
        return int(fallback)
    try:
        tz = ZoneInfo(name)
        delta = datetime.now(tz).utcoffset()
        if delta is None:
            return int(fallback)
        return -int(delta.total_seconds() // 60)
    except Exception:
        return int(fallback)


def oai_language_for_timezone(tz_name: str, accept_language: str = "") -> str:
    """Pick OAI-Language primary tag from Accept-Language or timezone heuristic."""
    raw = str(accept_language or "").strip()
    if raw:
        primary = raw.split(",")[0].strip().split(";")[0].strip()
        if primary:
            return primary
    tz = str(tz_name or "").strip()
    if tz.startswith("Asia/Shanghai") or tz.startswith("Asia/Chongqing"):
        return "zh-CN"
    if tz.startswith("Asia/Tokyo"):
        return "ja-JP"
    if tz.startswith("Europe/"):
        return "en-GB"
    return "en-US"


def build_client_contextual_info(
    *,
    seed: str = "",
    app_name: str | None = None,
    jitter: bool = True,
) -> dict[str, Any]:
    if jitter:
        material = str(seed or new_uuid()).encode("utf-8", "ignore")
        rng = random.Random(int(hashlib.sha256(material).hexdigest()[:16], 16))
    else:
        rng = random.Random(0)
    info: dict[str, Any] = {
        "is_dark_mode": bool(rng.random() < 0.35) if jitter else False,
        "time_since_loaded": int(rng.randint(45, 980)) if jitter else 120,
        "page_height": int(rng.choice([900, 1000, 1072, 1100, 1200])) if jitter else 900,
        "page_width": int(rng.choice([1280, 1400, 1440, 1724, 1920])) if jitter else 1400,
        "pixel_ratio": float(rng.choice([1.0, 1.25, 1.5, 2.0])) if jitter else 2,
        "screen_height": int(rng.choice([1080, 1200, 1440])) if jitter else 1440,
        "screen_width": int(rng.choice([1920, 2560, 1512])) if jitter else 2560,
        # SPA HAR 2026-07-21 always includes these.
        "app_name": str(app_name or "chatgpt.com"),
        "has_web_push_capabilities": True,
        "web_push_notification_permission": "default",
    }
    return info


def build_prepare_contextual_info() -> dict[str, Any]:
    """Minimal contextual shape emitted by SPA conversation/prepare requests."""
    return {
        "app_name": "chatgpt.com",
        "has_web_push_capabilities": True,
        "web_push_notification_permission": "default",
    }


def build_pure_http_image_contextual_info() -> dict[str, Any]:
    """Envelope used by the verified pure-HTTP image tool route.

    These legacy web-push field names are intentional.  Replacing them with
    the newer SPA/HAR shape routes the turn into a JSON tool-call that remains
    in ``async_status=STREAMING`` without producing an image.
    """
    return {
        "app_name": "chatgpt.com",
        "is_web_push_capable": True,
        "is_web_push_enabled": False,
    }


def _picture_v2_prompt(prompt: str) -> tuple[str, list[dict[str, Any]]]:
    mention = "@Create image"
    raw = str(prompt or "").strip()
    if raw.startswith(mention):
        raw = raw[len(mention):].lstrip(" \u00a0")
    text = f"{mention}\u00a0{raw}" if raw else mention
    return text, [
        {
            "id": "picture_v2",
            "symbol": "ecosystemMention",
            "startIndex": 0,
            "endIndex": len(mention),
        }
    ]


def build_sentinel_headers(requirements: Any) -> dict[str, str]:
    token = str(getattr(requirements, "token", "") or "")
    headers = {
        # Legacy finalize name (still accepted by some paths).
        "OpenAI-Sentinel-Chat-Requirements-Token": token,
        # SPA HAR 2026-07-21 uses Prepare-Token on /f/conversation.
        "OpenAI-Sentinel-Chat-Requirements-Prepare-Token": token,
    }
    proof = str(getattr(requirements, "proof_token", "") or "")
    turnstile = str(getattr(requirements, "turnstile_token", "") or "")
    so_token = str(getattr(requirements, "so_token", "") or "")
    if proof:
        headers["OpenAI-Sentinel-Proof-Token"] = proof
    if turnstile:
        headers["OpenAI-Sentinel-Turnstile-Token"] = turnstile
    if so_token:
        headers["OpenAI-Sentinel-SO-Token"] = so_token
    return headers


def build_chat_headers(requirements: Any) -> dict[str, str]:
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        **build_sentinel_headers(requirements),
    }
    return headers


def build_image_prepare_headers(requirements: Any) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        **build_sentinel_headers(requirements),
    }


def image_spa_tool_path_enabled(flag: bool | None = None) -> bool:
    """SPA 生图工具路径：system_hints=[] 且 SSE 不强制 X-Conduit-Token。"""
    if flag is not None:
        return bool(flag)
    try:
        from services.config import config

        return bool(getattr(config, "image_spa_tool_path", False))
    except Exception:
        return False


def build_image_start_headers(
    requirements: Any,
    conduit_token: object,
    *,
    spa_tool_path: bool | None = None,
) -> dict[str, str]:
    if image_spa_tool_path_enabled(spa_tool_path):
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **build_sentinel_headers(requirements),
        }
    require_conduit_token(conduit_token)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **build_sentinel_headers(requirements),
        "X-Conduit-Token": str(conduit_token).strip(),
        "X-Oai-Turn-Trace-Id": new_uuid(),
    }
    return headers


def _normalize_thinking_effort(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "none"}:
        return ""
    if normalized in {"low", "medium", "high"}:
        return normalized
    if normalized in {"xhigh", "extended"}:
        return "extended"
    return ""


def build_chat_body(
    messages: Sequence[Mapping[str, Any]] | Sequence[Any],
    model: str,
    timezone: str = "Asia/Shanghai",
    thinking_effort: str = "",
    *,
    convert_messages: Callable[[Sequence[Any]], list[dict[str, Any]]] | None = None,
    history_and_training_disabled: bool = True,
    conversation_id: str = "",
    parent_message_id: str = "",
    timezone_offset: int | None = None,
    contextual_seed: str = "",
    contextual_jitter: bool = True,
) -> dict[str, Any]:
    converted = list(convert_messages(messages) if convert_messages else messages)
    # SPA new chats use literal client-created-root (HAR 2026-07-21).
    parent_id = str(parent_message_id or "").strip() or "client-created-root"
    payload: dict[str, Any] = {
        "action": "next",
        "messages": converted,
        "model": model,
        "parent_message_id": parent_id,
        "conversation_mode": {"kind": "primary_assistant"},
        # SPA-aligned fields (HAR 2026-07-21 /f/conversation).
        "client_prepare_state": "none",
        "enable_message_followups": True,
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "system_hints": [],
        "timezone": str(timezone or "Asia/Shanghai"),
        "timezone_offset_min": (
            int(timezone_offset)
            if timezone_offset is not None
            else timezone_offset_min(str(timezone or "Asia/Shanghai"))
        ),
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
        "client_contextual_info": build_client_contextual_info(
            seed=contextual_seed or parent_id,
            jitter=contextual_jitter,
        ),
    }
    # API reverse-proxy may still force Temporary Chat; SPA omits this field.
    if history_and_training_disabled:
        payload["history_and_training_disabled"] = True
    cid = str(conversation_id or "").strip()
    if cid:
        payload["conversation_id"] = cid
    effort = _normalize_thinking_effort(thinking_effort)
    if effort:
        payload["thinking_effort"] = effort
    return payload


def build_text_prepare_body(
    prompt: str,
    model: str,
    *,
    timezone: str = "Asia/Shanghai",
    timezone_offset: int | None = None,
    parent_message_id: str = "",
    conversation_id: str = "",
    contextual_seed: str = "",
) -> dict[str, Any]:
    """SPA /backend-api/f/conversation/prepare body for plain text (HAR 2026-07-21)."""
    tz = str(timezone or "Asia/Shanghai")
    parent_id = str(parent_message_id or "").strip() or "client-created-root"
    msg_id = new_uuid()
    payload: dict[str, Any] = {
        "action": "next",
        "parent_message_id": parent_id,
        "model": model,
        "client_prepare_state": "none",
        "client_prepare_dispatch": "debounced",
        "client_prepare_source": "composer_editor_state",
        "timezone_offset_min": (
            int(timezone_offset) if timezone_offset is not None else timezone_offset_min(tz)
        ),
        "timezone": tz,
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "partial_query": {
            "id": msg_id,
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [str(prompt or "")]},
        },
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": build_prepare_contextual_info(),
    }
    cid = str(conversation_id or "").strip()
    if cid:
        payload["conversation_id"] = cid
    return payload


def build_image_prepare_body(
    prompt: str,
    model_slug: str,
    *,
    timezone: str = "Asia/Shanghai",
    timezone_offset: int | None = None,
    contextual_seed: str = "",
    spa_tool_path: bool | None = None,
) -> dict[str, Any]:
    tz = str(timezone or "Asia/Shanghai")
    spa = image_spa_tool_path_enabled(spa_tool_path)
    hints = [] if spa else ["picture_v2"]
    return {
        "action": "next",
        "parent_message_id": "client-created-root",
        "model": model_slug,
        "client_prepare_state": "none" if spa else "sent",
        "client_prepare_dispatch": "debounced" if spa else "immediate",
        "client_prepare_source": "composer_editor_state" if spa else "context_change",
        "timezone_offset_min": (
            int(timezone_offset) if timezone_offset is not None else timezone_offset_min(tz)
        ),
        "timezone": tz,
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": hints,
        "partial_query": {
            "id": new_uuid(),
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [prompt if spa else "Create image"]},
        },
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": (
            build_pure_http_image_contextual_info()
            if spa
            else build_prepare_contextual_info()
        ),
    }


def build_image_start_body(
    prompt: str,
    model_slug: str,
    *,
    references: Sequence[Mapping[str, Any]] | None = None,
    timezone: str = "Asia/Shanghai",
    timezone_offset: int | None = None,
    contextual_seed: str = "",
    spa_tool_path: bool | None = None,
) -> dict[str, Any]:
    refs = list(references or [])
    spa = image_spa_tool_path_enabled(spa_tool_path)
    hints: list[str] = [] if spa else ["picture_v2"]
    prompt_part, custom_symbol_offsets = (
        (str(prompt or ""), []) if spa else _picture_v2_prompt(prompt)
    )
    parts: list[Any] = [
        {
            "content_type": "image_asset_pointer",
            "asset_pointer": f"file-service://{item['file_id']}",
            "width": item["width"],
            "height": item["height"],
            "size_bytes": item["file_size"],
        }
        for item in refs
    ]
    parts.append(prompt_part)
    content = (
        {"content_type": "multimodal_text", "parts": parts}
        if refs
        else {"content_type": "text", "parts": [prompt_part]}
    )
    metadata: dict[str, Any] = {
        "system_hints": list(hints),
        "serialization_metadata": {"custom_symbol_offsets": custom_symbol_offsets},
    }
    if refs:
        metadata["attachments"] = [
            {
                "id": item["file_id"],
                "mimeType": item["mime_type"],
                "name": item["file_name"],
                "size": item["file_size"],
                "width": item["width"],
                "height": item["height"],
            }
            for item in refs
        ]
    import time

    tz = str(timezone or "Asia/Shanghai")
    user_message: dict[str, Any] = {
        "id": new_uuid(),
        "author": {"role": "user"},
        "content": content,
    }
    # The verified pure-HTTP tool turn has no user-message metadata at all.
    # Sending empty system_hints/custom_symbol_offsets is not equivalent: the
    # upstream stores those fields and can stop after emitting the code call
    # without ever appending the image tool result.
    if not spa:
        user_message["create_time"] = time.time()
        user_message["metadata"] = metadata

    return {
        "action": "next",
        "messages": [user_message],
        "parent_message_id": "client-created-root",
        "model": model_slug,
        "client_prepare_state": "none",
        "timezone_offset_min": (
            int(timezone_offset) if timezone_offset is not None else timezone_offset_min(tz)
        ),
        "timezone": tz,
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": list(hints),
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": (
            build_pure_http_image_contextual_info()
            if spa
            else build_client_contextual_info(
                seed=contextual_seed or prompt[:64],
                app_name="chatgpt.com",
                jitter=True,
            )
        ),
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
    }
