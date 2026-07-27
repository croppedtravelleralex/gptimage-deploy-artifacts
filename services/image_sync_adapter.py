from __future__ import annotations

import secrets
import time
from typing import Any

from services.config import config
from services.image_task_service import (
    ImageTaskQueueFullError,
    ImageTaskWaitTimeoutError,
    TASK_STATUS_ERROR,
    TASK_STATUS_SUCCESS,
    image_task_service,
)


def new_client_task_id() -> str:
    return f"sync-{secrets.token_urlsafe(16)}"


def _wait_timeout_secs() -> float:
    try:
        return max(60.0, min(900.0, float(config.newapi_image_sync_wait_timeout_secs)))
    except Exception:
        return 540.0


def _poll_interval_secs() -> float:
    try:
        return max(0.2, min(10.0, float(config.newapi_image_sync_poll_interval_secs)))
    except Exception:
        return 1.5


def _task_error_message(task: dict[str, Any]) -> str:
    return str(task.get("error") or "image task failed").strip() or "image task failed"


def build_openai_image_response(task: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    status = str(task.get("status") or "")
    if status == TASK_STATUS_SUCCESS:
        raw_data = task.get("data") or []
        data = [dict(item) for item in raw_data if isinstance(item, dict)]
        response: dict[str, Any] = {
            "created": int(time.time()),
            "data": data,
        }
        usage = task.get("usage")
        if isinstance(usage, dict) and usage:
            response["usage"] = usage
        resolved_task_id = str(task_id or task.get("id") or task.get("task_id") or "").strip()
        if resolved_task_id:
            response["task_id"] = resolved_task_id
        return response
    if status == TASK_STATUS_ERROR:
        raise RuntimeError(_task_error_message(task))
    raise ImageTaskWaitTimeoutError(str(task.get("id") or ""), task)


def _preferred_account_email() -> str:
    try:
        from services.request_account_context import get_preferred_account_email

        return str(get_preferred_account_email() or "").strip()
    except Exception:
        return ""


def run_generation_sync(
    identity: dict[str, object],
    *,
    prompt: str,
    model: str,
    size: str | None,
    quality: str,
    response_format: str,
    base_url: str,
    n: int = 1,
    preferred_account_email: str = "",
    prompt_enhance: bool = False,
    prompt_enhance_locale: str = "en",
    multi_image_mode: str = "fast",
) -> dict[str, Any]:
    task_id = new_client_task_id()
    prefer = str(preferred_account_email or _preferred_account_email() or "").strip()
    image_task_service.submit_generation(
        identity,
        client_task_id=task_id,
        prompt=prompt,
        model=model,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        n=n,
        preferred_account_email=prefer,
        prompt_enhance=bool(prompt_enhance),
        prompt_enhance_locale=str(prompt_enhance_locale or "en"),
        multi_image_mode=str(multi_image_mode or "fast"),
    )
    task = image_task_service.wait_for_result(
        identity,
        task_id,
        timeout_secs=_wait_timeout_secs(),
        poll_interval_secs=_poll_interval_secs(),
    )
    response = build_openai_image_response(task, task_id=task_id)
    image_task_service.compact_task_heavy_fields(identity, task_id)
    return response


def run_edit_sync(
    identity: dict[str, object],
    *,
    prompt: str,
    model: str,
    size: str | None,
    quality: str,
    response_format: str,
    base_url: str,
    images: list[tuple[bytes, str, str]] | None = None,
    masks: list[tuple[bytes, str, str]] | None = None,
    image_asset_ids: list[str] | None = None,
    mask_asset_ids: list[str] | None = None,
    n: int = 1,
    preferred_account_email: str = "",
) -> dict[str, Any]:
    task_id = new_client_task_id()
    prefer = str(preferred_account_email or _preferred_account_email() or "").strip()
    image_task_service.submit_edit(
        identity,
        client_task_id=task_id,
        prompt=prompt,
        model=model,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        images=images,
        masks=masks,
        image_asset_ids=image_asset_ids,
        mask_asset_ids=mask_asset_ids,
        n=n,
    )
    task = image_task_service.wait_for_result(
        identity,
        task_id,
        timeout_secs=_wait_timeout_secs(),
        poll_interval_secs=_poll_interval_secs(),
    )
    response = build_openai_image_response(task, task_id=task_id)
    image_task_service.compact_task_heavy_fields(identity, task_id)
    return response
