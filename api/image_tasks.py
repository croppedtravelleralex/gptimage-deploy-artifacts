from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.image_inputs import parse_image_edit_request, read_image_sources, read_image_sources_with_asset_ids
from api.support import require_identity, resolve_image_base_url
from services.config import config
from services.image_request_validation import ImageRequestValidationError, validate_generation_request, validation_error_detail
from services.content_filter import check_request
from services.image_task_service import ImageTaskQueueFullError, ImageTaskDuplicatePromptError, image_task_service
from services.log_service import LoggedCall


class ImageGenerationTaskRequest(BaseModel):
    client_task_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    size: str | None = None
    quality: str = "auto"
    n: int = Field(default=1, ge=1, le=4)
    prompt_enhance: bool = False
    prompt_enhance_locale: str = "en"
    multi_image_mode: str = "fast"
    preferred_account_email: str = ""


class ResumePollRequest(BaseModel):
    extra_timeout_secs: float = Field(default=300.0, ge=5.0, le=600.0)


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _image_generation_paused() -> bool:
    if bool(config.data.get("image_generation_paused")):
        return True
    try:
        settings = config.get_image_task_queue_settings()
        return not bool(settings.get("enabled", True))
    except Exception:
        return False


def _raise_image_generation_paused() -> None:
    raise HTTPException(
        status_code=503,
        detail={
            "error": "image generation is paused to preserve the account pool",
            "code": "image_generation_paused",
        },
        headers={"Retry-After": "300"},
    )


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-tasks")
    async def list_image_tasks(
        ids: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(image_task_service.list_tasks, identity, _parse_task_ids(ids))

    @router.get("/api/image-tasks/status")
    async def list_image_task_statuses(
        ids: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(image_task_service.list_task_statuses, identity, _parse_task_ids(ids))

    @router.post("/api/image-tasks/generations")
    async def create_generation_task(
        body: ImageGenerationTaskRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_preferred_account_email: str | None = Header(default=None, alias="X-Preferred-Account-Email"),
    ):
        identity = require_identity(authorization)
        if _image_generation_paused():
            _raise_image_generation_paused()
        try:
            validate_generation_request(body.prompt)
        except ImageRequestValidationError as exc:
            raise HTTPException(status_code=400, detail=validation_error_detail(exc)) from exc
        await filter_or_log(LoggedCall(identity, "/api/image-tasks/generations", body.model, "文生图任务", request_text=body.prompt), body.prompt)
        preferred_email = str(x_preferred_account_email or body.preferred_account_email or "").strip()
        try:
            return await run_in_threadpool(
                image_task_service.submit_generation,
                identity,
                client_task_id=body.client_task_id,
                prompt=body.prompt,
                model=body.model,
                size=body.size,
                quality=body.quality,
                base_url=resolve_image_base_url(request),
                n=body.n,
                prompt_enhance=body.prompt_enhance,
                prompt_enhance_locale=body.prompt_enhance_locale,
                multi_image_mode=body.multi_image_mode,
                preferred_account_email=preferred_email,
            )
        except ValueError as exc:
            detail = validation_error_detail(exc) if getattr(exc, "code", None) else {"error": str(exc)}
            raise HTTPException(status_code=400, detail=detail) from exc
        except ImageTaskQueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": str(exc)},
                headers={"Retry-After": "5"},
            ) from exc
        except ImageTaskDuplicatePromptError as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": str(exc), "code": "duplicate_prompt"},
                headers={"Retry-After": "30"},
            ) from exc

    @router.post("/api/image-tasks/edits")
    async def create_edit_task(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        if _image_generation_paused():
            _raise_image_generation_paused()
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        client_task_id = str(payload.get("client_task_id") or "").strip()
        if not client_task_id:
            raise HTTPException(status_code=400, detail={"error": "client_task_id is required"})
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        asset_ids = [str(item).strip() for item in (payload.get("asset_ids") or []) if str(item).strip()]
        await filter_or_log(LoggedCall(identity, "/api/image-tasks/edits", model, "图生图任务", request_text=prompt), prompt)
        if image_sources:
            images, asset_ids_from_images = await read_image_sources_with_asset_ids(image_sources)
            asset_ids = list(dict.fromkeys([*asset_ids, *asset_ids_from_images]))
        elif asset_ids:
            images = []
        else:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        masks = await read_image_sources(mask_sources) if mask_sources else None
        try:
            return await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=client_task_id,
                prompt=prompt,
                model=model,
                size=payload["size"],
                quality=payload["quality"],
                base_url=resolve_image_base_url(request),
                images=images,
                masks=masks,
                image_asset_ids=asset_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except ImageTaskQueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": str(exc)},
                headers={"Retry-After": "5"},
            ) from exc
        except ImageTaskDuplicatePromptError as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": str(exc), "code": "duplicate_prompt"},
                headers={"Retry-After": "30"},
            ) from exc

    @router.post("/api/image-tasks/{task_id}/resume-poll")
    async def resume_image_poll(
        task_id: str,
        body: ResumePollRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        if _image_generation_paused():
            _raise_image_generation_paused()
        try:
            return await run_in_threadpool(
                image_task_service.resume_poll,
                identity,
                task_id,
                body.extra_timeout_secs,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-tasks/{task_id}/cancel")
    async def cancel_image_task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(image_task_service.cancel_task, identity, task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    return router
