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

    return router
