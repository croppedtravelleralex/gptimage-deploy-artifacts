from __future__ import annotations

import re

from services.image_pipeline.types import MultiImageMode

_SHORT_CHAR_LIMIT = 48
_SHORT_WORD_LIMIT = 8
_LONG_CHAR_LIMIT = 256
_LONG_TOKEN_LIMIT = 80


def _word_count(text: str) -> int:
    parts = re.findall(r"\S+", text.strip())
    return len(parts)


def should_need_ps(*, prompt_enhance: bool, prompt: str) -> bool:
    """Whether this request should enter the pS stage."""
    if not prompt_enhance:
        return False
    text = str(prompt or "").strip()
    if not text:
        return False
    if len(text) >= _LONG_CHAR_LIMIT or _word_count(text) >= _LONG_TOKEN_LIMIT:
        return False
    if len(text) < _SHORT_CHAR_LIMIT or _word_count(text) < _SHORT_WORD_LIMIT:
        return True
    return True


def normalize_multi_image_mode(value: object) -> MultiImageMode:
    text = str(value or MultiImageMode.FAST.value).strip().lower()
    if text == MultiImageMode.DIVERSE.value:
        return MultiImageMode.DIVERSE
    return MultiImageMode.FAST


def ps_rounds_for_request(*, n: int, multi_image_mode: MultiImageMode, needs_ps: bool) -> int:
    if not needs_ps:
        return 0
    count = max(1, int(n or 1))
    if count <= 1:
        return 1
    if multi_image_mode == MultiImageMode.DIVERSE:
        return count
    return 1
