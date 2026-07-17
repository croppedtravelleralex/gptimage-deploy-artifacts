"""Canonical ChatGPT web request builder for chat and image upstream calls.

All outbound conversation / picture payloads and sentinel headers should be
built here so shape telemetry and fail-fast conduit checks stay consistent.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4


def new_uuid() -> str:
    return str(uuid4())


def require_conduit_token(conduit_token: object) -> str:
    token = str(conduit_token or "").strip()
    if not token:
        raise RuntimeError("missing conduit_token")
    return token


def build_sentinel_headers(requirements: Any) -> dict[str, str]:
    headers = {
        "OpenAI-Sentinel-Chat-Requirements-Token": str(getattr(requirements, "token", "") or ""),
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


def build_image_start_headers(requirements: Any, conduit_token: object) -> dict[str, str]:
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
) -> dict[str, Any]:
    converted = list(convert_messages(messages) if convert_messages else messages)
    payload: dict[str, Any] = {
        "action": "next",
        "messages": converted,
        "model": model,
        "parent_message_id": new_uuid(),
        "conversation_mode": {"kind": "primary_assistant"},
        "conversation_origin": None,
        "force_paragen": False,
        "force_paragen_model_slug": "",
        "force_rate_limit": False,
        "force_use_sse": True,
        "history_and_training_disabled": True,
        "reset_rate_limits": False,
        "suggestions": [],
        "supported_encodings": [],
        "system_hints": [],
        "timezone": timezone,
        "timezone_offset_min": -480,
        "variant_purpose": "comparison_implicit",
        "websocket_request_id": new_uuid(),
        "client_contextual_info": {
            "is_dark_mode": False,
            "time_since_loaded": 120,
            "page_height": 900,
            "page_width": 1400,
            "pixel_ratio": 2,
            "screen_height": 1440,
            "screen_width": 2560,
        },
    }
    effort = _normalize_thinking_effort(thinking_effort)
    if effort:
        payload["thinking_effort"] = effort
    return payload


def build_image_prepare_body(prompt: str, model_slug: str) -> dict[str, Any]:
    return {
        "action": "next",
        "fork_from_shared_post": False,
        "parent_message_id": new_uuid(),
        "model": model_slug,
        "client_prepare_state": "success",
        "timezone_offset_min": -480,
        "timezone": "Asia/Shanghai",
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": ["picture_v2"],
        "partial_query": {
            "id": new_uuid(),
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [prompt]},
        },
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {"app_name": "chatgpt.com"},
    }


def build_image_start_body(
    prompt: str,
    model_slug: str,
    *,
    references: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    refs = list(references or [])
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
    parts.append(prompt)
    content = (
        {"content_type": "multimodal_text", "parts": parts}
        if refs
        else {"content_type": "text", "parts": [prompt]}
    )
    metadata: dict[str, Any] = {
        "developer_mode_connector_ids": [],
        "selected_github_repos": [],
        "selected_all_github_repos": False,
        "system_hints": ["picture_v2"],
        "serialization_metadata": {"custom_symbol_offsets": []},
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

    return {
        "action": "next",
        "messages": [
            {
                "id": new_uuid(),
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": content,
                "metadata": metadata,
            }
        ],
        "parent_message_id": new_uuid(),
        "model": model_slug,
        "client_prepare_state": "sent",
        "timezone_offset_min": -480,
        "timezone": "Asia/Shanghai",
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": ["picture_v2"],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {
            "is_dark_mode": False,
            "time_since_loaded": 1200,
            "page_height": 1072,
            "page_width": 1724,
            "pixel_ratio": 1.2,
            "screen_height": 1440,
            "screen_width": 2560,
            "app_name": "chatgpt.com",
        },
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
    }
