from __future__ import annotations

from typing import Any, Iterator

from services.protocol.conversation import (
    ConversationRequest,
    collect_image_outputs,
    count_text_tokens,
    prefer_stream_for_multi_image,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)
from services.config import config
from services.image_request_validation import validate_generation_request
from utils.image_tokens import count_image_output_items_tokens, image_usage


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    body = prefer_stream_for_multi_image(body)
    prompt = str(body.get("prompt") or "")
    validate_generation_request(
        prompt,
        images=body.get("images") if isinstance(body.get("images"), list) else None,
        image_asset_ids=body.get("image_asset_ids") if isinstance(body.get("image_asset_ids"), list) else None,
    )
    model = str(body.get("model") or "gpt-image-2")
    n = int(body.get("n") or 1)
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None
    progress_callback = body.get("progress_callback")
    poll_timeout_secs = float(body.get("poll_timeout_secs") or config.image_generation_poll_timeout_secs)
    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        message_as_error=True,
        progress_callback=progress_callback,
        cancel_event=body.get("cancel_event"),
        poll_timeout_secs=poll_timeout_secs,
        queue_coordinated=bool(body.get("queue_coordinated")),
        prompt_enhance=bool(body.get("prompt_enhance")),
        prompt_enhance_locale=str(body.get("prompt_enhance_locale") or "en"),
        multi_image_mode=str(body.get("multi_image_mode") or "fast"),
        pipeline_run=body.get("pipeline_run"),
        preferred_account_email=str(body.get("preferred_account_email") or "").strip(),
    ))
    if body.get("stream"):
        return stream_image_chunks(outputs)
    result = collect_image_outputs(outputs)
    result["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        output_tokens=count_image_output_items_tokens(result.get("data"), size, quality),
    )
    return result
