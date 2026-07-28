from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services.auth_service import auth_service

from api.support import (
    require_admin,
    sanitize_cpa_pool,
    sanitize_cpa_pools,
    sanitize_sub2api_server,
    sanitize_sub2api_servers,
)
from services.account_service import account_service
from services.account_refresh_all_service import account_refresh_all_service
from services.account_maintenance_loop_service import account_maintenance_loop_service
from services.config import config
from services.panda_staging_service import panda_staging_service
from services.cpa_service import cpa_config, cpa_import_service, list_remote_files
from services.oauth_login_service import OAuthLoginError, oauth_login_service
from services.outlook_account_recovery_service import outlook_account_recovery_service
from services.outlook_auto_recovery_loop_service import outlook_auto_recovery_loop_service
from services.proactive_refresh_loop_service import proactive_refresh_loop_service
from services.quota_window_prime_service import quota_window_prime_service
from services.sub2api_service import (
    list_remote_accounts as sub2api_list_remote_accounts,
    list_remote_groups as sub2api_list_remote_groups,
    sub2api_config,
    sub2api_import_service,
)


BASE_DIR = Path(__file__).resolve().parents[1]
PANDA_SYNC_SCRIPT = BASE_DIR / "scripts" / "sync_accounts_delta_to_panda.ps1"
PANDA_SYNC_BATCH_SIZE = 20
PANDA_SYNC_MAX_ACCOUNTS_PER_RUN = 20
PANDA_SYNC_TIMEOUT_SECONDS = 120
MAX_ACCOUNT_REFRESH_TOKENS = 50
_panda_sync_lock = Lock()
_import_batch_lock = Lock()
_last_import_batch_at = 0.0


class PandaSyncAlreadyRunning(RuntimeError):
    pass


class UserKeyCreateRequest(BaseModel):
    name: str = ""


class UserKeyUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    key: str | None = None


class AccountCreateRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)
    accounts: list[dict[str, Any]] = Field(default_factory=list)
    skip_refresh: bool = False


class AccountImportBatchRequest(BaseModel):
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class AccountDeleteRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class AccountRefreshRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


class QuotaWindowPrimeRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    preferred_account_email: str = ""
    force: bool = False


class AccountOutlookRecoveryRequest(BaseModel):
    access_token: str = ""


class AccountRefreshAllStartRequest(BaseModel):
    concurrency: int | None = None
    max_concurrency: int | None = None
    batch_size: int | None = None
    delay_between_accounts_sec: float | None = None
    delay_between_batches_sec: float | None = None
    stale_after_hours: int | None = None
    include_recent: bool | None = None
    min_available_memory_mb: int | None = None
    max_load_1m: float | None = None
    resource_pause_enabled: bool | None = None
    resource_check_interval_sec: float | None = None
    limit: int | None = None
    delete_invalid: bool | None = None
    delete_after_failures: int | None = None
    expired_grace_hours: int | None = None
    panda_sync_enabled: bool | None = None
    panda_sync_base_url: str | None = None
    panda_sync_auth_key: str | None = None
    panda_sync_batch_size: int | None = None
    panda_sync_timeout_seconds: int | None = None
    panda_sync_remove_local_on_success: bool | None = None


class AccountMaintenanceLoopUpdateRequest(BaseModel):
    enabled: bool | None = None
    batch_limit: int | None = None
    concurrency: int | None = None
    batch_size: int | None = None
    delay_between_accounts_sec: float | None = None
    delay_between_batches_sec: float | None = None
    cooldown_sec: float | None = None
    stale_after_hours: int | None = None
    include_recent: bool | None = None
    min_available_memory_mb: int | None = None
    slow_min_available_memory_mb: int | None = None
    max_load_1m: float | None = None
    resource_pause_enabled: bool | None = None
    resource_check_interval_sec: float | None = None
    slow_when_image_inflight: int | None = None
    pause_when_image_inflight: int | None = None
    slow_batch_limit: int | None = None
    slow_delay_between_accounts_sec: float | None = None
    slow_cooldown_sec: float | None = None
    startup_delay_sec: float | None = None
    delete_invalid: bool | None = None
    delete_after_failures: int | None = None
    expired_grace_hours: int | None = None


class OutlookAutoRecoveryUpdateRequest(BaseModel):
    enabled: bool | None = None
    interval_sec: int | None = None
    max_per_cycle: int | None = None
    startup_delay_sec: float | None = None
    progress_poll_sec: float | None = None


class PandaSyncUpdateRequest(BaseModel):
    enabled: bool | None = None


class AccountExportRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    format: Literal["json", "zip"] = "json"


class AccountUpdateRequest(BaseModel):
    access_token: str = ""
    type: str | None = None
    status: str | None = None
    quota: int | None = None
    proxy: str | None = None


class AccountSoftBandRequest(BaseModel):
    access_token: str = ""
    percent: float | None = None
    clear: bool = False


class AccountSchedulingRequest(BaseModel):
    access_token: str = ""
    enabled: bool = True
    reason: str = ""


class AccountSchedulingBulkRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    enabled: bool = True
    reason: str = ""


class CPAPoolCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    secret_key: str = ""


class CPAPoolUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    secret_key: str | None = None


class CPAImportRequest(BaseModel):
    names: list[str] = Field(default_factory=list)


class Sub2APIServerCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    email: str = ""
    password: str = ""
    api_key: str = ""
    group_id: str = ""


class Sub2APIServerUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    email: str | None = None
    password: str | None = None
    api_key: str | None = None
    group_id: str | None = None


class Sub2APIImportRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)


class OAuthLoginStartRequest(BaseModel):
    """起始 OAuth 桥。email_hint 可选，仅用于让 OpenAI 登录页预填邮箱。"""
    email_hint: str = ""


class OAuthLoginFinishRequest(BaseModel):
    """提交 callback。callback 既可以是完整 URL 也可以只填 code。"""
    session_id: str = ""
    callback: str = ""


def _account_payload_token(item: dict[str, Any]) -> str:
    return str(item.get("access_token") or item.get("accessToken") or "").strip()


def _unique_tokens(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip()))


def _download_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_export_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return (clean or fallback)[:80]


def _account_zip_bytes(items: list[dict[str, str]]) -> bytes:
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(items, start=1):
            raw_name = item.get("email") or item.get("account_id") or f"account-{index:03d}"
            base_name = _safe_export_name(raw_name, f"account-{index:03d}")
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}-{suffix}"
                suffix += 1
            used_names.add(name)
            archive.writestr(
                f"{name}.json",
                json.dumps(item, ensure_ascii=False, indent=2) + "\n",
            )
    return buf.getvalue()


def _tail_text(value: str | None, max_lines: int = 40, max_chars: int = 4000) -> str:
    text = str(value or "")
    if not text:
        return ""
    lines = text.splitlines()[-max_lines:]
    tail = "\n".join(lines)
    if len(tail) > max_chars:
        return tail[-max_chars:]
    return tail


def _panda_sync_summary(stdout: str) -> str:
    prefixes = (
        "local_count=",
        "pending_new_accounts=",
        "throttled_this_run=",
        "api_added=",
        "api_updated=",
        "skip=",
        "sync_complete=",
    )
    lines = [line.strip() for line in stdout.splitlines() if line.strip().startswith(prefixes)]
    return "；".join(lines[-5:])


def _run_panda_account_sync() -> dict[str, object]:
    if not PANDA_SYNC_SCRIPT.is_file():
        raise FileNotFoundError(f"同步脚本不存在：{PANDA_SYNC_SCRIPT}")

    shell = shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise RuntimeError("找不到 PowerShell，无法执行本地同步脚本")

    if not _panda_sync_lock.acquire(blocking=False):
        raise PandaSyncAlreadyRunning("已有同步任务正在执行，请等待它结束后再试")

    command = [
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
    ]
    if os.name == "nt":
        command.extend(["-WindowStyle", "Hidden"])
    command.extend([
        "-File",
        str(PANDA_SYNC_SCRIPT),
        "-BatchSize",
        str(PANDA_SYNC_BATCH_SIZE),
        "-MaxAccountsPerRun",
        str(PANDA_SYNC_MAX_ACCOUNTS_PER_RUN),
    ])
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    try:
        try:
            completed = subprocess.run(
                command,
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PANDA_SYNC_TIMEOUT_SECONDS,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_tail = _tail_text(exc.stdout)
            stderr_tail = _tail_text(exc.stderr)
            return {
                "ok": False,
                "exit_code": None,
                "error": f"同步超过 {PANDA_SYNC_TIMEOUT_SECONDS} 秒，已停止等待",
                "summary": _panda_sync_summary(stdout_tail),
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "batch_size": PANDA_SYNC_BATCH_SIZE,
                "max_accounts_per_run": PANDA_SYNC_MAX_ACCOUNTS_PER_RUN,
                "timeout_seconds": PANDA_SYNC_TIMEOUT_SECONDS,
            }

        stdout_tail = _tail_text(completed.stdout)
        stderr_tail = _tail_text(completed.stderr)
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "error": "" if completed.returncode == 0 else "同步脚本执行失败",
            "summary": _panda_sync_summary(completed.stdout),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "batch_size": PANDA_SYNC_BATCH_SIZE,
            "max_accounts_per_run": PANDA_SYNC_MAX_ACCOUNTS_PER_RUN,
            "timeout_seconds": PANDA_SYNC_TIMEOUT_SECONDS,
        }
    finally:
        _panda_sync_lock.release()


def _accounts_list_payload(offset: int, limit: int) -> dict:
    items = account_service.list_accounts()
    total = len(items)
    page_items = items[offset: offset + limit] if limit > 0 else []
    stats = account_service.get_stats(enriched_accounts=items)
    return {
        "items": page_items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "stats": stats,
    }


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/users")
    async def list_user_keys(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users")
    async def create_user_key(body: UserKeyCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            item, raw_key = auth_service.create_key(role="user", name=body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"item": item, "key": raw_key, "items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users/{key_id}")
    async def update_user_key(
            key_id: str,
            body: UserKeyUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in {
                "name": body.name,
                "enabled": body.enabled,
                "key": body.key,
            }.items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        try:
            item = auth_service.update_key(key_id, updates, role="user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"item": item, "items": auth_service.list_keys(role="user")}

    @router.delete("/api/auth/users/{key_id}")
    async def delete_user_key(key_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not auth_service.delete_key(key_id, role="user"):
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"items": auth_service.list_keys(role="user")}

    @router.get("/api/accounts")
    async def get_accounts(
            authorization: str | None = Header(default=None),
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=200, ge=0, le=10000),
    ):
        require_admin(authorization)
        return await run_in_threadpool(_accounts_list_payload, offset, limit)

    @router.post("/api/accounts/reload-from-storage")
    async def reload_accounts_from_storage(authorization: str | None = Header(default=None)):
        """管理员受控热加载外部进程已提交的账号持久化快照。"""
        require_admin(authorization)
        result = await run_in_threadpool(account_service.reload_from_storage)
        return {
            "ok": True,
            "total": int(result.get("total") or 0),
            "stats": account_service.get_stats(),
        }

    @router.post("/api/accounts/sync/panda")
    async def sync_accounts_to_panda(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not _panda_sync_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail={"error": "panda 同步正在运行，请稍后再试"})
        try:
            result = await run_in_threadpool(account_refresh_all_service.queue_available_accounts_for_panda)
            synced = int(result.get("synced") or 0)
            failed = int(result.get("failed") or 0)
            queued = int(result.get("queued") or 0)
            details = result.get("details") if isinstance(result.get("details"), dict) else {}
            settings = config.get_panda_sync_settings()
            summary = f"synced={synced} failed={failed} queued={queued}"
            return {
                "ok": failed == 0,
                "exit_code": 0 if failed == 0 else 1,
                "error": "" if failed == 0 else "部分账号同步到 panda 失败",
                "summary": summary,
                "stdout_tail": summary,
                "stderr_tail": "",
                "batch_size": int(settings.get("batch_size") or 20),
                "max_accounts_per_run": int(settings.get("upload_max_batch") or 20),
                "timeout_seconds": int(settings.get("timeout_seconds") or 60),
                "synced": synced,
                "failed": failed,
                "queued": queued,
                "details": details,
                "stats": account_service.get_stats(),
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc
        finally:
            _panda_sync_lock.release()

    @router.get("/api/accounts/activity/daily")
    async def get_account_activity_daily(
            authorization: str | None = Header(default=None),
            days: int = Query(default=14, ge=1, le=90),
    ):
        require_admin(authorization)
        return account_service.get_activity_daily(days=days)

    @router.get("/api/accounts/usage/recent")
    async def get_accounts_usage_recent(
            authorization: str | None = Header(default=None),
            days: int = Query(default=6, ge=1, le=14),
    ):
        """号池「记录」列：今日 + 过去若干日的生图/对话次数。"""
        require_admin(authorization)
        return await run_in_threadpool(account_service.get_accounts_usage_recent, days)

    @router.get("/api/accounts/usage/binding-slots")
    async def get_binding_usage_slots(
            authorization: str | None = Header(default=None),
            week_offset: int = Query(default=0),
            timezone: str = Query(default="Asia/Shanghai"),
            days: int | None = Query(default=None, ge=7, le=90),
    ):
        """IP 绑定组 7×12 活动热力图（按自然周 Mon–Sun 聚合）。"""
        require_admin(authorization)
        from services.usage_event_metrics import get_binding_usage_slots

        return await run_in_threadpool(
            lambda: get_binding_usage_slots(
                week_offset=week_offset,
                timezone=timezone,
                days=days,
                account_service=account_service,
            )
        )

    @router.post("/api/accounts/soft-band")
    async def set_account_soft_band(
            body: AccountSoftBandRequest,
            authorization: str | None = Header(default=None),
            include_items: bool = Query(default=False),
    ):
        """手动指定软限流%（5–99）；clear=true 清除覆盖。"""
        require_admin(authorization)
        access_token = str(body.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        percent: float | None
        if body.clear:
            percent = None
        elif body.percent is None:
            raise HTTPException(status_code=400, detail={"error": "percent is required unless clear=true"})
        else:
            percent = float(body.percent)
            if percent < 5 or percent > 99:
                raise HTTPException(status_code=400, detail={"error": "percent must be between 5 and 99"})
        account = await run_in_threadpool(
            account_service.set_soft_band_override,
            access_token,
            percent=percent,
            quiet=False,
        )
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        response: dict = {"item": account}
        if include_items:
            response["items"] = account_service.list_accounts()
        else:
            response["stats"] = account_service.get_stats()
        return response

    @router.get("/api/accounts/panda-sync")
    async def get_panda_sync_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"panda_sync": config.get_public_panda_sync_settings()}

    @router.post("/api/accounts/panda-sync")
    async def update_panda_sync_settings(body: PandaSyncUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        current = config.get_panda_sync_settings()
        updates = body.model_dump(mode="python", exclude_none=True)
        if not updates:
            return {"panda_sync": config.get_public_panda_sync_settings()}
        next_settings = {**current, **updates}
        updated = config.update({"panda_sync": next_settings})
        return {"panda_sync": updated.get("panda_sync", {})}

    @router.post("/api/accounts")
    async def create_accounts(
            body: AccountCreateRequest,
            authorization: str | None = Header(default=None),
            include_items: bool = Query(default=True),
    ):
        require_admin(authorization)
        account_payloads = [item for item in body.accounts if isinstance(item, dict)]
        payload_tokens = [_account_payload_token(item) for item in account_payloads]
        tokens = _unique_tokens([*body.tokens, *payload_tokens])
        if not tokens and body.skip_refresh:
            response = {
                "added": 0,
                "skipped": 0,
                "refreshed": 0,
                "errors": [],
            }
            if include_items:
                response["items"] = account_service.list_accounts()
            return response
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        if account_payloads:
            result = account_service.add_account_items(account_payloads, include_items=include_items)
            payload_token_set = set(_unique_tokens(payload_tokens))
            extra_tokens = [token for token in tokens if token not in payload_token_set]
            if extra_tokens:
                extra_result = account_service.add_accounts(extra_tokens, include_items=include_items)
                result["added"] = int(result.get("added") or 0) + int(extra_result.get("added") or 0)
                result["skipped"] = int(result.get("skipped") or 0) + int(extra_result.get("skipped") or 0)
        else:
            result = account_service.add_accounts(tokens, include_items=include_items)
        if body.skip_refresh:
            response = {
                **result,
                "refreshed": 0,
                "errors": [],
            }
            if not include_items:
                response.pop("items", None)
            return response
        refresh_result = account_service.refresh_accounts(tokens, include_items=include_items)
        response = {
            **result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
        }
        if include_items:
            response["items"] = refresh_result.get("items", result.get("items", []))
        else:
            response.pop("items", None)
        return response

    @router.post("/api/accounts/import-batch")
    async def import_account_batch(
            body: AccountImportBatchRequest,
            authorization: str | None = Header(default=None),
            include_items: bool = Query(default=False),
    ):
        global _last_import_batch_at
        require_admin(authorization)
        account_payloads = [item for item in body.accounts if isinstance(item, dict)]
        panda_settings = config.get_panda_sync_settings()
        max_batch = max(1, int(panda_settings.get("public_import_max_batch_size") or 20))
        min_interval = max(0, int(panda_settings.get("public_import_min_interval_sec") or 0))
        if len(account_payloads) > max_batch:
            raise HTTPException(
                status_code=413,
                detail={"error": f"import batch too large: {len(account_payloads)} > {max_batch}"},
            )
        now = time.monotonic()
        if min_interval > 0 and _last_import_batch_at > 0:
            wait = _last_import_batch_at + min_interval - now
            if wait > 0:
                raise HTTPException(
                    status_code=429,
                    detail={"error": f"import batch rate limited, retry after {int(wait) + 1}s"},
                    headers={"Retry-After": str(int(wait) + 1)},
                )
        if not _import_batch_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail={"error": "another import batch is running"})
        try:
            _last_import_batch_at = now
            return await run_in_threadpool(
                _import_account_batch_locked,
                account_payloads,
                include_items,
            )
        finally:
            _import_batch_lock.release()

    def _import_account_batch_locked(account_payloads: list[dict[str, Any]], include_items: bool) -> dict[str, Any]:
        if not account_payloads:
            response = {
                "added": 0,
                "skipped": 0,
                "stats": account_service.get_stats(),
            }
            if include_items:
                response["items"] = account_service.list_accounts()
            return response
        result = account_service.import_account_items(account_payloads, include_items=include_items)
        result["stats"] = account_service.get_stats()
        if not include_items:
            result.pop("items", None)
        return result

    @router.delete("/api/accounts")
    async def delete_accounts(
            body: AccountDeleteRequest,
            authorization: str | None = Header(default=None),
            include_items: bool = Query(default=True),
    ):
        require_admin(authorization)
        tokens = [str(token or "").strip() for token in body.tokens if str(token or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        result = account_service.delete_accounts(tokens, include_items=include_items)
        if not include_items:
            result.pop("items", None)
            result["stats"] = account_service.get_stats()
        return result

    @router.post("/api/accounts/refresh")
    async def refresh_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            access_tokens = account_service.list_tokens()
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        if len(access_tokens) > MAX_ACCOUNT_REFRESH_TOKENS:
            raise HTTPException(
                status_code=400,
                detail={"error": f"单次最多刷新 {MAX_ACCOUNT_REFRESH_TOKENS} 个账号，请先筛选或选择少量账号"},
            )

        progress_id = str(uuid.uuid4())
        account_service.init_refresh_progress(progress_id, len(access_tokens))

        async def _do_refresh():
            try:
                await run_in_threadpool(account_service.refresh_accounts, access_tokens, progress_id, False, False)
                await run_in_threadpool(account_refresh_all_service.sync_last_refreshed_accounts_to_panda)
            except Exception as e:
                account_service.finish_refresh_progress(progress_id, error=str(e))

        asyncio.create_task(_do_refresh())

        return {"progress_id": progress_id}

    @router.get("/api/accounts/refresh/progress/{progress_id}")
    async def get_refresh_progress(progress_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        progress = account_service.get_refresh_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return progress

    @router.post("/api/accounts/refresh-all/start")
    async def start_refresh_all_accounts(
            body: AccountRefreshAllStartRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        maintenance = account_maintenance_loop_service.get_status()
        if maintenance.get("state") == "running_batch":
            raise HTTPException(status_code=409, detail={"error": "panda 轻量保活正在运行当前批次，请稍后再启动手动慢刷"})
        options = {
            key: value
            for key, value in body.model_dump(mode="python").items()
            if value is not None
        }
        if not bool(config.get_account_refresh_all_settings().get("delete_invalid", True)):
            options["delete_invalid"] = False
        try:
            return account_refresh_all_service.start(options)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc

    @router.get("/api/accounts/refresh-all/status")
    async def get_refresh_all_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return account_refresh_all_service.get_status()

    @router.post("/api/accounts/refresh-all/stop")
    async def stop_refresh_all_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return account_refresh_all_service.stop()

    @router.get("/api/accounts/maintenance-loop/status")
    async def get_account_maintenance_loop_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return account_maintenance_loop_service.get_status()

    @router.get("/api/accounts/panda-staging/status")
    async def get_panda_staging_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return panda_staging_service.get_status()

    @router.post("/api/accounts/maintenance-loop")
    async def update_account_maintenance_loop(
            body: AccountMaintenanceLoopUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in body.model_dump(mode="python").items()
            if value is not None
        }
        if (
            "delete_invalid" in updates
            and bool(updates["delete_invalid"])
            and not bool(config.get_account_maintenance_loop_settings().get("delete_invalid", True))
        ):
            updates["delete_invalid"] = False
        return account_maintenance_loop_service.update_settings(updates)

    @router.get("/api/accounts/outlook-auto-recovery/status")
    async def get_outlook_auto_recovery_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return outlook_auto_recovery_loop_service.get_status()

    @router.get("/api/accounts/proactive-refresh/status")
    async def get_proactive_refresh_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proactive_refresh_loop_service.get_status()

    @router.post("/api/accounts/quota-window/prime")
    async def quota_window_prime(
        body: QuotaWindowPrimeRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        email = str(body.preferred_account_email or "").strip()
        tokens = [str(token).strip() for token in (body.access_tokens or []) if str(token).strip()]
        force = bool(body.force)
        try:
            if email and not tokens:
                return await run_in_threadpool(quota_window_prime_service.enqueue_by_email, email, force=force)
            if not tokens:
                raise ValueError("access_tokens or preferred_account_email is required")
            if len(tokens) == 1:
                return await run_in_threadpool(quota_window_prime_service.enqueue, tokens[0], force=force)
            return await run_in_threadpool(quota_window_prime_service.enqueue_many, tokens, force=force)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc

    @router.get("/api/accounts/quota-window/prime/status")
    async def quota_window_prime_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return quota_window_prime_service.get_status()

    @router.post("/api/accounts/outlook-auto-recovery")
    async def update_outlook_auto_recovery(
            body: OutlookAutoRecoveryUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in body.model_dump(mode="python").items()
            if value is not None
        }
        return outlook_auto_recovery_loop_service.update_settings(updates)

    @router.post("/api/accounts/recover-outlook")
    async def recover_outlook_account(
            body: AccountOutlookRecoveryRequest,
            authorization: str | None = Header(default=None),
    ):
        """从账号页触发单个异常 Outlook 的完整 OTP 恢复链。"""
        require_admin(authorization)
        try:
            progress_id = outlook_account_recovery_service.start(body.access_token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except (FileNotFoundError, PermissionError) as exc:
            raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        return {"progress_id": progress_id}

    @router.get("/api/accounts/recover-outlook/progress/{progress_id}")
    async def get_outlook_recovery_progress(
            progress_id: str,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        progress = outlook_account_recovery_service.get_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return progress

    @router.post("/api/accounts/re-login")
    async def re_login_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        """对选中账号执行密码重新登录流程（密码登录→验证码登录→刷新token）。"""
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})

        progress_id = str(uuid.uuid4())
        account_service.init_relogin_progress(progress_id, len(access_tokens))

        async def _do_relogin():
            try:
                await run_in_threadpool(account_service.re_login_accounts, access_tokens, progress_id)
            except Exception as e:
                account_service.finish_relogin_progress(progress_id, error=str(e))

        asyncio.create_task(_do_relogin())

        return {"progress_id": progress_id}

    @router.get("/api/accounts/re-login/progress/{progress_id}")
    async def get_relogin_progress(progress_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        progress = account_service.get_relogin_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return progress

    @router.post("/api/accounts/export")
    async def export_accounts(body: AccountExportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = _unique_tokens(body.access_tokens)
        items = account_service.build_export_items(access_tokens)
        if not items:
            raise HTTPException(
                status_code=400,
                detail={"error": "没有可导出的完整账号，需要同时有 access_token、refresh_token 和 id_token"},
            )

        timestamp = _download_timestamp()
        if body.format == "zip":
            content = _account_zip_bytes(items)
            return Response(
                content,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.zip"'},
            )

        payload: dict[str, str] | list[dict[str, str]] = items[0] if len(items) == 1 else items
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.json"'},
        )

    @router.post("/api/accounts/update")
    async def update_account(
            body: AccountUpdateRequest,
            authorization: str | None = Header(default=None),
            include_items: bool = Query(default=True),
    ):
        require_admin(authorization)
        access_token = str(body.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        updates = {key: value for key, value in {"type": body.type, "status": body.status, "quota": body.quota, "proxy": body.proxy}.items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        account = account_service.update_account(access_token, updates)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        response = {"item": account}
        if include_items:
            response["items"] = account_service.list_accounts()
        else:
            response["stats"] = account_service.get_stats()
        return response

    @router.get("/api/accounts/schedulable-breakdown")
    async def get_schedulable_breakdown(
            authorization: str | None = Header(default=None),
    ):
        """SCHED-001: structured exclusion buckets for image scheduling."""
        require_admin(authorization)
        return await run_in_threadpool(account_service.get_schedulable_breakdown)

    @router.post("/api/accounts/scheduling")
    async def set_account_scheduling(
            body: AccountSchedulingRequest,
            authorization: str | None = Header(default=None),
    ):
        """人工进/出调度：enabled=true → verified_ready；false → identity_isolated。"""
        require_admin(authorization)
        access_token = str(body.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        account = await run_in_threadpool(
            account_service.set_account_scheduling,
            access_token,
            enabled=bool(body.enabled),
            reason=str(body.reason or "").strip(),
            quiet=False,
        )
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {
            "item": account,
            "enabled": account_service.is_manual_scheduling_enabled(account),
            "stats": account_service.get_stats(),
        }

    @router.post("/api/accounts/scheduling/bulk")
    async def set_accounts_scheduling_bulk(
            body: AccountSchedulingBulkRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        tokens = [str(token or "").strip() for token in (body.access_tokens or []) if str(token or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        if len(tokens) > MAX_ACCOUNT_REFRESH_TOKENS:
            raise HTTPException(
                status_code=400,
                detail={"error": f"单次最多操作 {MAX_ACCOUNT_REFRESH_TOKENS} 个账号"},
            )
        result = await run_in_threadpool(
            account_service.set_accounts_scheduling,
            tokens,
            enabled=bool(body.enabled),
            reason=str(body.reason or "").strip(),
        )
        return result

    @router.post("/api/accounts/oauth/start")
    async def start_oauth_login(
            body: OAuthLoginStartRequest,
            authorization: str | None = Header(default=None),
    ):
        """登记一次 PKCE 会话，返回可让用户浏览器打开的 authorize URL。"""
        require_admin(authorization)
        try:
            return await run_in_threadpool(oauth_login_service.start, body.email_hint)
        except OAuthLoginError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/accounts/oauth/finish")
    async def finish_oauth_login(
            body: OAuthLoginFinishRequest,
            authorization: str | None = Header(default=None),
    ):
        """收用户从浏览器抓回的 callback URL / code，换出 token 三件套并落盘。"""
        require_admin(authorization)
        # 入参日志：截断敏感字段，仅保留前几位，方便排错而不泄密
        cb_preview = (body.callback or "")[:80]
        sid_preview = (body.session_id or "")[:8]
        print(
            f"[oauth-login] finish called: session_id={sid_preview}..., callback_preview={cb_preview!r}",
            flush=True,
        )
        try:
            tokens = await run_in_threadpool(oauth_login_service.finish, body.session_id, body.callback)
        except OAuthLoginError as exc:
            print(f"[oauth-login] finish rejected: {exc}", flush=True)
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        payload = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "source_type": "oauth_login",
        }
        add_result = await run_in_threadpool(account_service.add_account_items, [payload])
        refresh_result = await run_in_threadpool(
            account_service.refresh_accounts, [tokens["access_token"]]
        )
        return {
            **add_result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
            "items": refresh_result.get("items", add_result.get("items", [])),
        }

    @router.get("/api/cpa/pools")
    async def list_cpa_pools(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools")
    async def create_cpa_pool(body: CPAPoolCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        if not body.secret_key.strip():
            raise HTTPException(status_code=400, detail={"error": "secret_key is required"})
        pool = cpa_config.add_pool(name=body.name, base_url=body.base_url, secret_key=body.secret_key)
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools/{pool_id}")
    async def update_cpa_pool(pool_id: str, body: CPAPoolUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.update_pool(pool_id, body.model_dump(exclude_none=True))
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.delete("/api/cpa/pools/{pool_id}")
    async def delete_cpa_pool(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not cpa_config.delete_pool(pool_id):
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.get("/api/cpa/pools/{pool_id}/files")
    async def cpa_pool_files(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool_id": pool_id, "files": await run_in_threadpool(list_remote_files, pool)}

    @router.post("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import(pool_id: str, body: CPAImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        try:
            job = cpa_import_service.start_import(pool, body.names)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import_progress(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"import_job": pool.get("import_job")}

    @router.get("/api/sub2api/servers")
    async def list_sub2api_servers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers")
    async def create_sub2api_server(body: Sub2APIServerCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        has_login = body.email.strip() and body.password.strip()
        has_api_key = bool(body.api_key.strip())
        if not has_login and not has_api_key:
            raise HTTPException(status_code=400, detail={"error": "email+password or api_key is required"})
        server = sub2api_config.add_server(
            name=body.name,
            base_url=body.base_url,
            email=body.email,
            password=body.password,
            api_key=body.api_key,
            group_id=body.group_id,
        )
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers/{server_id}")
    async def update_sub2api_server(server_id: str, body: Sub2APIServerUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.update_server(server_id, body.model_dump(exclude_none=True))
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.delete("/api/sub2api/servers/{server_id}")
    async def delete_sub2api_server(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not sub2api_config.delete_server(server_id):
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.get("/api/sub2api/servers/{server_id}/groups")
    async def sub2api_server_groups(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            groups = await run_in_threadpool(sub2api_list_remote_groups, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "groups": groups}

    @router.get("/api/sub2api/servers/{server_id}/accounts")
    async def sub2api_server_accounts(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            accounts = await run_in_threadpool(sub2api_list_remote_accounts, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "accounts": accounts}

    @router.post("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import(server_id: str, body: Sub2APIImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            job = sub2api_import_service.start_import(server, body.account_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import_progress(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"import_job": server.get("import_job")}

    return router
