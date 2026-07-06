from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources, read_image_sources_with_asset_ids
from api.support import require_identity, resolve_image_base_url
from services.content_filter import check_request, request_shape, request_text
from services.editable_file_task_service import editable_file_task_service
from services.image_sync_adapter import build_openai_image_response, new_client_task_id, run_edit_sync, run_generation_sync
from services.image_task_service import (
    ImageTaskQueueFullError,
    ImageTaskWaitTimeoutError,
    image_task_service,
)
from services.log_service import LoggedCall, _image_error_response
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
    openai_search,
)

PANDA_ASYNC_PROMPT_PREFIXES = ("panda-async:", "panda_async:")
PANDA_TASK_PROMPT_PREFIXES = (
    "panda status ",
    "panda-status ",
    "panda_task ",
    "panda-task ",
    "panda-task:",
    "panda_task:",
    "panda-task://",
    "panda_task://",
)


def _parse_panda_prompt_tunnel(prompt: str) -> tuple[str, bool, str]:
    text = str(prompt or "")
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in PANDA_TASK_PROMPT_PREFIXES:
        if lowered.startswith(prefix):
            return "poll panda async image task", False, stripped[len(prefix):].strip()
    for prefix in PANDA_ASYNC_PROMPT_PREFIXES:
        if lowered.startswith(prefix):
            clean_prompt = stripped[len(prefix):].strip()
            return clean_prompt or text, True, ""
    return text, False, ""


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None
    client_task_id: str | None = None
    panda_async: bool | None = None
    panda_task_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


class SearchRequest(BaseModel):
    prompt: str = Field(..., min_length=1)


class EditableFileTaskRequest(BaseModel):
    prompt: str = ""
    base64_images: list[str] = Field(default_factory=list)
    client_task_id: str | None = None


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


async def _run_image_sync_call(call: LoggedCall, runner, **kwargs):
    try:
        result = await run_in_threadpool(runner, call.identity, **kwargs)
        call.log("调用完成", result)
        return result
    except ImageTaskQueueFullError as exc:
        call.log("调用失败", status="failed", error=str(exc))
        raise HTTPException(
            status_code=429,
            detail={"error": str(exc)},
            headers={"Retry-After": "5"},
        ) from exc
    except ImageTaskWaitTimeoutError as exc:
        call.log(
            "调用超时",
            status="timeout_pending",
            error=str(exc),
        )
        return JSONResponse(
            status_code=504,
            content={
                "error": {
                    "message": str(exc),
                    "type": "image_task_timeout",
                    "code": "image_task_timeout",
                    "task_id": exc.task_id,
                }
            },
        )
    except Exception as exc:
        call.log("调用失败", status="failed", error=str(exc))
        return _image_error_response(exc)


def _task_summary(task: dict[str, object]) -> dict[str, object]:
    status = str(task.get("status") or "").strip()
    data = task.get("data")
    return {
        "id": str(task.get("id") or task.get("task_id") or ""),
        "task_id": str(task.get("id") or task.get("task_id") or ""),
        "status": status,
        "mode": str(task.get("mode") or ""),
        "progress": str(task.get("progress") or ""),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "result_count": len(data) if isinstance(data, list) else 0,
        "error": str(task.get("error") or ""),
        "running_limit": task.get("running_limit"),
        "accepted_limit": task.get("accepted_limit"),
    }


def _image_task_envelope(task: dict[str, object]) -> dict[str, object]:
    summary = _task_summary(task)
    payload: dict[str, object] = {
        "created": int(time.time()),
        "object": "image.task",
        "id": summary["id"],
        "task_id": summary["task_id"],
        "status": summary["status"],
        "mode": summary["mode"],
        "progress": summary["progress"],
        "data": [],
        "panda_task": summary,
    }
    if summary.get("error"):
        # NewAPI treats a top-level "error" field as a failed channel call even
        # when HTTP status is 200.  For async task status polling, keep the
        # transport successful and expose the task failure under panda_error so
        # pollers can stop without creating a NewAPI error-log storm.
        payload["panda_error"] = {
            "message": summary["error"],
            "type": "image_task_error",
            "code": "image_task_error",
        }
    return payload


def _image_task_status_response(identity: dict[str, object], task_id: str) -> dict[str, object]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("panda_task_id is required")
    result = image_task_service.list_tasks(identity, [task_id])
    items = result.get("items") if isinstance(result, dict) else None
    task = items[0] if isinstance(items, list) and items else None
    if not isinstance(task, dict):
        raise KeyError(f"image task not found: {task_id}")
    if str(task.get("status") or "") == "success":
        response = build_openai_image_response(task)
        response["task_id"] = task_id
        response["panda_task"] = _task_summary(task)
        return response
    return _image_task_envelope(task)


async def _run_image_task_status_call(call: LoggedCall, identity: dict[str, object], task_id: str):
    try:
        result = await run_in_threadpool(_image_task_status_response, identity, task_id)
        call.log("任务状态查询完成", result)
        return result
    except KeyError as exc:
        call.log("任务状态查询失败", status="failed", error=str(exc))
        raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
    except ValueError as exc:
        call.log("任务状态查询失败", status="failed", error=str(exc))
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc


async def _run_generation_async_submit_call(call: LoggedCall, identity: dict[str, object], **kwargs):
    try:
        task = await run_in_threadpool(image_task_service.submit_generation, identity, **kwargs)
        result = _image_task_envelope(task)
        call.log("异步任务已入队", result)
        return result
    except ValueError as exc:
        call.log("异步任务入队失败", status="failed", error=str(exc))
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except ImageTaskQueueFullError as exc:
        call.log("异步任务入队失败", status="failed", error=str(exc))
        raise HTTPException(
            status_code=429,
            detail={"error": str(exc)},
            headers={"Retry-After": "5"},
        ) from exc


async def _run_edit_async_submit_call(call: LoggedCall, identity: dict[str, object], **kwargs):
    try:
        task = await run_in_threadpool(image_task_service.submit_edit, identity, **kwargs)
        result = _image_task_envelope(task)
        call.log("异步任务已入队", result)
        return result
    except ValueError as exc:
        call.log("异步任务入队失败", status="failed", error=str(exc))
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except ImageTaskQueueFullError as exc:
        call.log("异步任务入队失败", status="failed", error=str(exc))
        raise HTTPException(
            status_code=429,
            detail={"error": str(exc)},
            headers={"Retry-After": "5"},
        ) from exc


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        prompt, prompt_async, prompt_task_id = _parse_panda_prompt_tunnel(body.prompt)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图", request_text=prompt)
        if body.panda_task_id or prompt_task_id:
            return await _run_image_task_status_call(call, identity, body.panda_task_id or prompt_task_id)
        await filter_or_log(call, prompt)
        if body.stream:
            return await call.run(openai_v1_image_generations.handle, payload)
        if body.panda_async or prompt_async:
            return await _run_generation_async_submit_call(
                call,
                identity,
                client_task_id=(body.client_task_id or new_client_task_id()),
                prompt=prompt,
                model=body.model,
                size=body.size,
                quality=body.quality,
                response_format=body.response_format,
                base_url=payload["base_url"],
                n=body.n,
            )
        return await _run_image_sync_call(
            call,
            run_generation_sync,
            prompt=prompt,
            model=body.model,
            size=body.size,
            quality=body.quality,
            response_format=body.response_format,
            base_url=payload["base_url"],
            n=body.n,
        )

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        prompt, prompt_async, prompt_task_id = _parse_panda_prompt_tunnel(str(payload["prompt"]))
        model = str(payload["model"])
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图", request_text=prompt)
        if payload.get("panda_task_id") or prompt_task_id:
            return await _run_image_task_status_call(call, identity, str(payload.get("panda_task_id") or prompt_task_id or ""))
        await filter_or_log(call, prompt)
        asset_ids = [str(item).strip() for item in (payload.get("asset_ids") or []) if str(item).strip()]
        if image_sources:
            images, asset_ids_from_images = await read_image_sources_with_asset_ids(image_sources)
            payload["images"] = images
            asset_ids = list(dict.fromkeys([*asset_ids, *asset_ids_from_images]))
        elif asset_ids:
            payload["images"] = []
        else:
            payload["images"] = await read_image_sources(image_sources)
        if mask_sources:
            payload["mask"] = await read_image_sources(mask_sources)
        payload["base_url"] = resolve_image_base_url(request)
        if payload.get("stream"):
            if asset_ids:
                raise HTTPException(status_code=400, detail={"error": "asset_ids are not supported for stream image edits"})
            return await call.run(openai_v1_image_edit.handle, payload)
        if payload.get("panda_async") or prompt_async:
            return await _run_edit_async_submit_call(
                call,
                identity,
                client_task_id=str(payload.get("client_task_id") or "").strip() or new_client_task_id(),
                prompt=prompt,
                model=model,
                size=payload.get("size"),
                quality=str(payload.get("quality") or "auto"),
                response_format=str(payload.get("response_format") or "b64_json"),
                base_url=payload["base_url"],
                images=payload["images"],
                masks=payload.get("mask") or None,
                image_asset_ids=asset_ids,
                n=int(payload.get("n") or 1),
            )
        return await _run_image_sync_call(
            call,
            run_edit_sync,
            prompt=prompt,
            model=model,
            size=payload.get("size"),
            quality=str(payload.get("quality") or "auto"),
            response_format=str(payload.get("response_format") or "b64_json"),
            base_url=payload["base_url"],
            images=payload["images"],
            masks=payload.get("mask") or None,
            image_asset_ids=asset_ids,
            n=int(payload.get("n") or 1),
        )

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        call = LoggedCall(
            identity,
            "/v1/chat/completions",
            model,
            "文本生成",
            request_text=request_preview,
            request_shape=request_shape(payload.get("messages")),
        )
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        call = LoggedCall(
            identity,
            "/v1/responses",
            model,
            "Responses",
            request_text=request_preview,
            request_shape=request_shape(payload.get("input")),
        )
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("system"), payload.get("messages"), payload.get("tools"))
        call = LoggedCall(identity, "/v1/messages", model, "Messages", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    @router.post("/v1/search")
    async def search(body: SearchRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        call = LoggedCall(identity, "/v1/search", openai_search.MODEL, "搜索", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_search.handle, body.model_dump(mode="python"))

    @router.get("/v1/editable-file-tasks")
    async def list_editable_file_tasks(ids: str = "", authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        task_ids = [item.strip() for item in ids.split(",") if item.strip()]
        return await run_in_threadpool(editable_file_task_service.list_tasks, identity, task_ids)

    @router.get("/files/{file_path:path}")
    async def download_editable_file(file_path: str):
        try:
            path = await run_in_threadpool(editable_file_task_service.public_file_path, file_path)
        except Exception as exc:
            raise HTTPException(status_code=404, detail={"error": "file not found"}) from exc
        return FileResponse(path, filename=path.name)

    @router.post("/v1/ppt/generations")
    async def create_ppt_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/ppt/generations", "gpt-5-5-thinking", "PPT生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_ppt,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.post("/v1/psd/generations")
    async def create_psd_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/psd/generations", "gpt-5-5-thinking", "PSD生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_psd,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    return router
