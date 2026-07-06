from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from starlette.status import HTTP_404_NOT_FOUND

from api.image_inputs import parse_reference_asset_request, read_image_sources
from api.support import require_identity
from services.image_asset_service import (
    ImageAssetNotFoundError,
    ImageAssetUploadWindowFullError,
    image_asset_service,
)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/image-assets/references")
    async def create_reference_assets(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        sources = await parse_reference_asset_request(request)
        images = await read_image_sources(sources)
        try:
            items = await run_in_threadpool(image_asset_service.create_assets, identity, images)
        except ImageAssetUploadWindowFullError as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": str(exc)},
                headers={"Retry-After": str(exc.retry_after_secs)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"items": items}

    @router.get("/api/image-assets/references/{asset_id}/status")
    async def get_reference_asset_status(
        asset_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(image_asset_service.get_asset, identity, asset_id)
        except ImageAssetNotFoundError as exc:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail={"error": str(exc)}) from exc

    @router.delete("/api/image-assets/references/{asset_id}")
    async def delete_reference_asset(
        asset_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        deleted = await run_in_threadpool(image_asset_service.delete_asset, identity, asset_id)
        return {"deleted": bool(deleted)}

    return router
