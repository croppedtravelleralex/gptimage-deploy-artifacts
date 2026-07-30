from __future__ import annotations

import re

from services.openai_backend_api import _is_content_policy_error

# Operator / scheduler diagnostics that must never reach NewAPI end users.
_INTERNAL_MARKERS: tuple[str, ...] = (
    "续轮询",
    "同步调用方",
    "墙钟预算",
    "config.json",
    "image_poll_timeout_secs",
    "image_task_queue",
    "newapi_image_sync",
    "sync_ladder",
    "timeout_pending",
    "resume_polling",
    "sS stage wall",
    "ss stage wall",
    "阶梯",
    "号池",
    "调度",
    "槽位",
    "conversation get 预算",
    "image_poll_max_upstream",
    "image_poll_retry",
    "image_poll_rate_limited",
    "image_poll_cf_edge",
    "退出原因",
    "轮询在上游错误重试后提前退出",
)

_USER_TIMEOUT_MESSAGE = (
    "Image generation is taking longer than expected. "
    "Retry later, or poll task status with the returned task_id."
)
_USER_BUSY_MESSAGE = "Image service is busy. Please retry in a few seconds."
_USER_GENERIC_MESSAGE = "The image generation request failed. Please try again later."
_USER_QUOTA_MESSAGE = "No image generation quota is available right now. Please try again later."


def is_internal_image_error(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    lower = text.lower()
    if any(marker in text for marker in _INTERNAL_MARKERS):
        return True
    if re.search(r"elapsed\s+\d", lower) and "stage wall timeout" in lower:
        return True
    if "image task wait timeout" in lower:
        return True
    return False


def map_user_facing_image_error(message: str) -> str:
    """Map operator-facing scheduler errors to stable user-visible text."""
    text = str(message or "").strip()
    if not text:
        return _USER_GENERIC_MESSAGE
    if _is_content_policy_error(text):
        return text
    lower = text.lower()
    if "no available image quota" in lower or "insufficient_quota" in lower:
        return _USER_QUOTA_MESSAGE
    if "duplicate prompt" in lower:
        return text
    if "instant limit" in lower or "limit resets" in lower:
        return text
    if "image service busy" in lower or "image_service_busy" in lower:
        return _USER_BUSY_MESSAGE
    if is_internal_image_error(text):
        if any(
            token in text
            for token in ("timeout", "timed out", "超时", "wall", "续轮询", "预算已耗尽", "预算不足")
        ):
            return _USER_TIMEOUT_MESSAGE
        return _USER_GENERIC_MESSAGE
    if any(token in lower for token in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return _USER_GENERIC_MESSAGE
    return text
