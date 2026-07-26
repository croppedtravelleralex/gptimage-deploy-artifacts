from __future__ import annotations

import os
from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, image_assets, image_tasks, ops, register, system
from api.errors import install_exception_handlers
from api.support import resolve_web_asset, start_limited_account_watcher, web_asset_cache_headers
from services.backup_service import backup_service
from services.account_maintenance_loop_service import account_maintenance_loop_service
from services.config import config
from services.image_service import start_image_cleanup_scheduler
from services.image_task_service import image_task_service
from services.image_pipeline.pipeline_watchdog import pipeline_watchdog_service
from services.outlook_auto_recovery_loop_service import outlook_auto_recovery_loop_service
from services.panda_staging_service import panda_staging_service
from services.proactive_refresh_loop_service import proactive_refresh_loop_service
from services.register_service import register_service
from services.risk_audit_service import risk_audit_service
from services.text_nurture_service import text_nurture_service
from services.account_warmup_service import account_warmup_service
from services.webshare_cf_scan_service import webshare_cf_scan_service


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        thread = start_limited_account_watcher(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        if os.getenv("CHATGPT2API_DISABLE_REGISTER", "").strip().lower() not in {"1", "true", "yes", "on"}:
            register_service.start_if_enabled()
        image_task_service.start_background()
        account_maintenance_loop_service.start_background()
        outlook_auto_recovery_loop_service.start_background()
        proactive_refresh_loop_service.start_background()
        panda_staging_service.start_background()
        text_nurture_service.start_background()
        webshare_cf_scan_service.start_background()
        risk_audit_service.start_background()
        account_warmup_service.start_background()
        pipeline_watchdog_service.start_background()
        backup_service.start()
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            image_task_service.stop()
            account_maintenance_loop_service.stop_background()
            outlook_auto_recovery_loop_service.stop_background()
            proactive_refresh_loop_service.stop_background()
            panda_staging_service.stop_background()
            text_nurture_service.stop_background()
            webshare_cf_scan_service.stop_background()
            risk_audit_service.stop_background()
            account_warmup_service.stop_background()
            pipeline_watchdog_service.stop_background()
            backup_service.stop()

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_assets.create_router())
    app.include_router(image_tasks.create_router())
    if os.getenv("CHATGPT2API_DISABLE_REGISTER", "").strip().lower() not in {"1", "true", "yes", "on"}:
        app.include_router(register.create_router())
    app.include_router(ops.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset, headers=web_asset_cache_headers(asset))
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback, headers=web_asset_cache_headers(fallback))

    return app
