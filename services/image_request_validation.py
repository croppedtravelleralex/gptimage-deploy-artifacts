"""Pre-submit validation for image generation requests."""

from __future__ import annotations

import re


class ImageRequestValidationError(ValueError):
    """Client request is inconsistent before any upstream call."""

    def __init__(self, message: str, *, code: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.code = code


_REFERENCE_REQUIRED_PROMPT_PATTERNS = (
    re.compile(r"基于.{0,48}参考图"),
    re.compile(r"使用.{0,48}参考图"),
    re.compile(r"参考图.{0,48}(生成|拼贴|保持|身份|人物)"),
    re.compile(r"人物参考图"),
    re.compile(r"参考图.{0,24}人物"),
    re.compile(r"身份保持"),
    re.compile(r"based\s+on.{0,60}reference\s+image", re.IGNORECASE),
    re.compile(r"use\s+(the\s+)?reference\s+image", re.IGNORECASE),
    re.compile(r"reference\s+image.{0,40}(as|for|to)\b", re.IGNORECASE),
    re.compile(r"identity\s+preserv", re.IGNORECASE),
)

_MISSING_REFERENCE_MESSAGE = (
    "generate 模式需要附参考图：prompt 要求基于参考图生成，请改用 /v1/images/edits "
    "或 /api/image-tasks/edits 并上传参考图。"
)


def prompt_requires_reference_image(prompt: str) -> bool:
    """True when prompt semantics require an uploaded reference image."""
    text = str(prompt or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _REFERENCE_REQUIRED_PROMPT_PATTERNS)


def validate_generation_request(
    prompt: str,
    *,
    images: list[object] | None = None,
    image_asset_ids: list[str] | None = None,
) -> None:
    """Reject generate-mode requests that need a reference image but provide none."""
    has_images = bool(images)
    has_assets = any(str(item or "").strip() for item in (image_asset_ids or []))
    if has_images or has_assets:
        return
    if prompt_requires_reference_image(prompt):
        raise ImageRequestValidationError(
            _MISSING_REFERENCE_MESSAGE,
            code="missing_reference_image",
        )


def validation_error_detail(exc: BaseException) -> dict[str, str]:
    code = str(getattr(exc, "code", "") or "invalid_request_error").strip()
    return {
        "error": str(exc) or "invalid image request",
        "code": code,
        "type": "invalid_request_error",
    }
