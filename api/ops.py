from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.ip_nurture_schedule import (
    binding_schedule_from_config,
    list_presets,
    save_binding_schedule,
)
from services.llm_ops_agent import invoke_tool, list_tools, run_agent
from services.risk_audit_service import risk_audit_service
from services.risk_dashboard_service import build_calendar, build_dashboard
from services.risk_metrics_store import list_reports
from services.text_nurture_service import text_nurture_service
from services.account_warmup_service import account_warmup_service
from services.account_service import account_service
from services.webshare_cf_scan_service import webshare_cf_scan_service
from services.account_cf_refresh_service import account_cf_refresh_service
from services.image_pipeline import image_pipeline_scheduler
from services.quota_refresh_schedule_service import quota_refresh_schedule_service
from services.quota_window_prime_service import quota_window_prime_service
from services.quota_binding_calendar import engine_info as quota_calendar_engine_info


class NurtureEnableRequest(BaseModel):
    enabled: bool = False


class NurtureEnqueueRequest(BaseModel):
    prompt: str = ""
    access_token: str = ""
    email: str = ""
    source: str = "manual"


class NurtureProcessRequest(BaseModel):
    prompt: str = ""
    access_token: str = ""
    email: str = ""
    source: str = "manual"


class WarmupUnblockRequest(BaseModel):
    email: str = ""


class ChatPrewarmRequest(BaseModel):
    email: str = ""


class ChatAllocateRequest(BaseModel):
    session_id: str = ""


class ChatReleaseRequest(BaseModel):
    session_id: str = ""
    email: str = ""


class OpsToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class OpsAgentRequest(BaseModel):
    query: str = ""
    max_tools: int = 4


class IpNurtureBindingRequest(BaseModel):
    binding_key: str = ""
    preset_id: str = ""
    custom_matrix: list[list[float]] | None = None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/ops/tools")
    async def ops_list_tools(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"tools": list_tools()}

    @router.post("/api/ops/tools/{tool_name}")
    async def ops_invoke_tool(
        tool_name: str,
        body: OpsToolRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(invoke_tool, tool_name, body.arguments)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc

    @router.post("/api/ops/agent")
    async def ops_run_agent(body: OpsAgentRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        query = str(body.query or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail={"error": "query is required"})
        return await run_in_threadpool(run_agent, query, max_tools=int(body.max_tools or 4))

    @router.get("/api/ops/nurture/status")
    async def nurture_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(text_nurture_service.status)

    @router.get("/api/ops/warmup/status")
    async def warmup_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(account_warmup_service.status)

    @router.get("/api/ops/quota-schedule/status")
    async def quota_schedule_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "refresh": quota_refresh_schedule_service.get_status(),
            "prime": quota_window_prime_service.get_status(),
            "calendar_engine": quota_calendar_engine_info(),
        }

    @router.get("/api/ops/quota-schedule/preview")
    async def quota_schedule_preview(
        authorization: str | None = Header(default=None),
        local_date: str | None = None,
    ):
        require_admin(authorization)
        return await run_in_threadpool(
            quota_refresh_schedule_service.preview_slots,
            local_date=local_date,
        )

    @router.post("/api/ops/warmup/unblock")
    async def warmup_unblock(
        body: WarmupUnblockRequest,
        authorization: str | None = Header(default=None),
    ):
        """手动解除单个账号的 CF 探活封禁 + demote 冷却，使其立即恢复可派发。

        封禁状态纯内存：解封结果不落库，也不跨进程重启保留。
        """
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                account_warmup_service.clear_block,
                str(body.email or ""),
                reason="manual_api",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/ops/warmup/unblock-all")
    async def warmup_unblock_all(authorization: str | None = Header(default=None)):
        """批量解除全部 CF 探活封禁 + demote 冷却。同样不跨进程重启保留。"""
        require_admin(authorization)
        try:
            return await run_in_threadpool(account_warmup_service.clear_all_blocks, reason="manual_api")
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/ops/chat/prewarm")
    async def chat_prewarm(
        body: ChatPrewarmRequest,
        authorization: str | None = Header(default=None),
    ):
        """预热对话 sentinel/turnstile ticket，缩短首字 TTFT。"""
        require_admin(authorization)
        email = str(body.email or "").strip().lower()
        if not email:
            raise HTTPException(status_code=400, detail={"error": "email is required"})

        def _run() -> dict[str, object]:
            token = ""
            for account in account_service.list_accounts():
                if str(account.get("email") or "").strip().lower() == email:
                    token = str(account.get("access_token") or "").strip()
                    break
            if not token:
                raise ValueError(f"account not found: {email}")
            from services.openai_backend_api import OpenAIBackendAPI

            backend = OpenAIBackendAPI(access_token=token)
            try:
                backend._ensure_bootstrap(soft_fail=True)
                requirements = backend._get_chat_requirements()
                turnstile = bool(getattr(requirements, "turnstile_token", ""))
                try:
                    from services.config import config
                    from services.image_pipeline.pre_ticket_pool import PreTicketBundle, pre_ticket_pool

                    if bool(config.get_image_pipeline_settings().get("pre_ticket_pool_enabled", True)):
                        pre_ticket_pool.put(
                            token,
                            PreTicketBundle(requirements=requirements, turnstile_solved=turnstile),
                        )
                except Exception:
                    pass
                return {
                    "ok": True,
                    "email": email,
                    "turnstile": turnstile,
                }
            finally:
                backend.close()

        try:
            return await run_in_threadpool(_run)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc

    @router.post("/api/chat/sessions/allocate")
    async def chat_session_allocate(
        body: ChatAllocateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        from services.chat_session_service import chat_session_service

        def _run() -> dict[str, object]:
            result = chat_session_service.allocate(account_service, session_id=str(body.session_id or ""))
            if not result.get("ok"):
                raise ValueError(str(result.get("error") or "allocate_failed"))
            email = str(result.get("email") or "").strip()
            if email:
                from services.openai_backend_api import OpenAIBackendAPI
                from services.image_pipeline.pre_ticket_pool import PreTicketBundle, pre_ticket_pool
                from services.config import config

                token = str(result.get("access_token") or "")
                if token:
                    backend = OpenAIBackendAPI(access_token=token)
                    try:
                        backend._ensure_bootstrap(soft_fail=True)
                        requirements = backend._get_chat_requirements()
                        if bool(config.get_image_pipeline_settings().get("pre_ticket_pool_enabled", True)):
                            pre_ticket_pool.put(
                                token,
                                PreTicketBundle(
                                    requirements=requirements,
                                    turnstile_solved=bool(getattr(requirements, "turnstile_token", "")),
                                ),
                            )
                    except Exception:
                        pass
                    finally:
                        backend.close()
            return {
                "ok": True,
                "email": email,
                "mode": str(result.get("mode") or "exclusive"),
                "session_id": str(result.get("session_id") or body.session_id or ""),
            }

        try:
            return await run_in_threadpool(_run)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc

    @router.post("/api/chat/sessions/release")
    async def chat_session_release(
        body: ChatReleaseRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        from services.chat_session_service import chat_session_service

        await run_in_threadpool(
            chat_session_service.release,
            session_id=str(body.session_id or ""),
            email=str(body.email or ""),
        )
        return {"ok": True}

    @router.post("/api/ops/nurture/enable")
    async def nurture_enable(body: NurtureEnableRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(text_nurture_service.set_enabled, bool(body.enabled))

    @router.post("/api/ops/nurture/enqueue")
    async def nurture_enqueue(body: NurtureEnqueueRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                text_nurture_service.enqueue,
                prompt=str(body.prompt or ""),
                access_token=str(body.access_token or ""),
                email=str(body.email or ""),
                source=str(body.source or "manual"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/ops/nurture/process-one")
    async def nurture_process_one(
        body: NurtureProcessRequest | None = None,
        authorization: str | None = Header(default=None),
    ):
        """同步执行一条养号对话：有 body.email/prompt 则定向执行；否则从队列取一条。"""
        require_admin(authorization)
        payload: dict[str, Any] | None = None
        if body is not None:
            payload = {
                "prompt": str(body.prompt or ""),
                "access_token": str(body.access_token or ""),
                "email": str(body.email or ""),
                "source": str(body.source or "manual"),
            }
            # 空 payload 字段表示走队列 dequeue
            if not any(str(payload.get(k) or "").strip() for k in ("prompt", "access_token", "email")):
                payload = None
        try:
            return await run_in_threadpool(text_nurture_service.process_one, payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/ops/ip-nurture/presets")
    async def ip_nurture_presets(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(lambda: {"presets": list_presets()})

    @router.get("/api/ops/ip-nurture/bindings")
    async def ip_nurture_bindings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(lambda: {"bindings": binding_schedule_from_config()})

    @router.post("/api/ops/ip-nurture/bindings")
    async def ip_nurture_save_binding(
        body: IpNurtureBindingRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                save_binding_schedule,
                str(body.binding_key or ""),
                str(body.preset_id or ""),
                body.custom_matrix,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/ops/webshare-cf-scan/status")
    async def webshare_cf_scan_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(webshare_cf_scan_service.status)

    @router.get("/api/ops/webshare-cf-scan/inventory")
    async def webshare_cf_scan_inventory(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(webshare_cf_scan_service.inventory)

    @router.post("/api/ops/webshare-cf-scan/run-once")
    async def webshare_cf_scan_run_once(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(lambda: webshare_cf_scan_service.run_once(force=True))
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/ops/account-cf-refresh/status")
    async def account_cf_refresh_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(account_cf_refresh_service.status)

    @router.post("/api/ops/account-cf-refresh/run-once")
    async def account_cf_refresh_run_once(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(lambda: account_cf_refresh_service.run_once(force=True))
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/ops/humanlike-dashboard")
    async def humanlike_dashboard(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(build_dashboard)

    @router.get("/api/ops/risk-calendar")
    async def risk_calendar(days: int = 112, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        safe_days = max(7, min(200, int(days or 112)))
        return await run_in_threadpool(lambda: build_calendar(days=safe_days))

    @router.get("/api/ops/risk-checks")
    async def risk_checks(limit: int = 48, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        safe_limit = max(1, min(200, int(limit or 48)))
        items = await run_in_threadpool(lambda: list_reports(limit=safe_limit))
        status = await run_in_threadpool(risk_audit_service.status)
        return {"ok": True, "items": items, "status": status}

    @router.post("/api/ops/risk-checks/run")
    async def risk_checks_run(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(lambda: risk_audit_service.run_once(source="manual"))

    @router.get("/api/ops/risk-audit/status")
    async def risk_audit_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(risk_audit_service.status)

    @router.get("/api/ops/image-pipeline/snapshot")
    async def image_pipeline_snapshot(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(image_pipeline_scheduler.snapshot)

    return router
