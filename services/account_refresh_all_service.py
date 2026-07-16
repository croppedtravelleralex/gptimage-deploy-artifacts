from __future__ import annotations

import os
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from services.account_identity import missing_panda_identity_fields, normalize_account_identity
from services.account_service import AccountService, account_service
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.proxy_url_utils import is_local_only_proxy_url
from utils.helper import anonymize_token

BASE_DIR = Path(__file__).resolve().parents[1]
PANDA_SYNC_PENDING_FILE = BASE_DIR / "data" / "panda_sync_pending.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_terminal_outlook_recovery(account: dict[str, Any]) -> bool:
    state = str(account.get("outlook_recovery_state") or "").strip().lower()
    reason = str(account.get("outlook_recovery_terminal_reason") or "").strip().lower()
    return state == "terminal" or reason == "account_deactivated"


def _clamp_int(value: object, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _clamp_float(value: object, default: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_cgroup_memory() -> tuple[int | None, int | None]:
    current_candidates = (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    )
    limit_candidates = (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    )

    current: int | None = None
    limit: int | None = None
    for path in current_candidates:
        raw = _read_text(path)
        if raw:
            try:
                current = int(raw)
                break
            except ValueError:
                pass
    for path in limit_candidates:
        raw = _read_text(path)
        if raw and raw != "max":
            try:
                candidate = int(raw)
            except ValueError:
                continue
            # Docker reports a huge sentinel when memory is unlimited.
            if 0 < candidate < 1 << 60:
                limit = candidate
                break
    return current, limit


def _read_mem_available_bytes() -> int | None:
    raw = _read_text("/proc/meminfo")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


@dataclass(frozen=True)
class AccountRefreshAllOptions:
    concurrency: int = 1
    batch_size: int = 10
    delay_between_accounts_sec: float = 0.8
    delay_between_batches_sec: float = 5.0
    stale_after_hours: int = 6
    include_recent: bool = False
    min_available_memory_mb: int = 512
    max_load_1m: float = 1.5
    resource_pause_enabled: bool = False
    resource_check_interval_sec: float = 10.0
    limit: int | None = None
    delete_invalid: bool = True
    delete_after_failures: int = 3
    expired_grace_hours: int = 1
    panda_sync_requested: bool = False
    panda_sync_enabled: bool = False
    panda_sync_base_url: str = ""
    panda_sync_auth_key: str = ""
    panda_sync_batch_size: int = 20
    panda_sync_timeout_seconds: int = 60
    panda_sync_remove_local_on_success: bool = False
    panda_sync_cooldown_seconds: float = 2.0
    panda_sync_queue_on_failure: bool = False
    token_overrides: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "AccountRefreshAllOptions":
        data = value if isinstance(value, dict) else {}
        defaults = config.get_account_refresh_all_settings()
        panda_defaults = config.get_panda_sync_settings()
        raw_limit = data.get("limit")
        limit = None
        if raw_limit is not None:
            limit = _clamp_int(raw_limit, 0, 0, 10000)
        # 本地慢刷用于大号池快速筛活时不能被低默认值卡死。
        # 旧逻辑把配置里的 max_concurrency 当绝对上限，并且硬封顶 16；
        # 因此页面填 100 时会被本地 config.json 的 4 压成 4。
        # 这里改为：配置只作为默认值；请求显式传 max_concurrency 时可提高，
        # 仅保留 512 作为防误操作保险，避免一手滑把几千账号同时打出去。
        configured_max_concurrency = _clamp_int(defaults.get("max_concurrency"), 32, 1, 512)
        requested_max_concurrency = _clamp_int(
            data.get("max_concurrency", configured_max_concurrency),
            configured_max_concurrency,
            1,
            512,
        )
        max_concurrency = requested_max_concurrency
        panda_base_url = str(data.get("panda_sync_base_url", panda_defaults.get("base_url")) or "").strip().rstrip("/")
        panda_auth_key = str(data.get("panda_sync_auth_key", panda_defaults.get("auth_key")) or "").strip()
        panda_requested = bool(data.get("panda_sync_enabled", panda_defaults.get("enabled", False))) and bool(panda_base_url)
        configured_delete_invalid = bool(defaults.get("delete_invalid", True))
        requested_delete_invalid = bool(data.get("delete_invalid", configured_delete_invalid))
        delete_invalid = configured_delete_invalid and requested_delete_invalid
        return cls(
            concurrency=_clamp_int(data.get("concurrency"), int(defaults.get("concurrency") or 1), 1, max_concurrency),
            batch_size=_clamp_int(data.get("batch_size"), int(defaults.get("batch_size") or 10), 1, 200),
            delay_between_accounts_sec=_clamp_float(data.get("delay_between_accounts_sec"), float(defaults.get("delay_between_accounts_sec") or 0.8), 0.0, 30.0),
            delay_between_batches_sec=_clamp_float(data.get("delay_between_batches_sec"), float(defaults.get("delay_between_batches_sec") or 5.0), 0.0, 300.0),
            stale_after_hours=_clamp_int(data.get("stale_after_hours"), int(defaults.get("stale_after_hours") or 6), 0, 24 * 30),
            include_recent=bool(data.get("include_recent", defaults.get("include_recent", False))),
            min_available_memory_mb=_clamp_int(data.get("min_available_memory_mb"), int(defaults.get("min_available_memory_mb") or 512), 0, 4096),
            max_load_1m=_clamp_float(data.get("max_load_1m"), float(defaults.get("max_load_1m") or 1.5), 0.0, 64.0),
            resource_pause_enabled=bool(data.get("resource_pause_enabled", defaults.get("resource_pause_enabled", False))),
            resource_check_interval_sec=_clamp_float(data.get("resource_check_interval_sec"), float(defaults.get("resource_check_interval_sec") or 10.0), 1.0, 120.0),
            limit=limit,
            delete_invalid=delete_invalid,
            delete_after_failures=_clamp_int(data.get("delete_after_failures"), int(defaults.get("delete_after_failures") or 3), 0, 20),
            expired_grace_hours=_clamp_int(data.get("expired_grace_hours"), int(defaults.get("expired_grace_hours") or 1), 0, 24 * 30),
            panda_sync_requested=panda_requested,
            panda_sync_enabled=panda_requested and bool(panda_auth_key),
            panda_sync_base_url=panda_base_url,
            panda_sync_auth_key=panda_auth_key,
            panda_sync_batch_size=_clamp_int(data.get("panda_sync_batch_size"), int(panda_defaults.get("batch_size") or 20), 1, 200),
            panda_sync_timeout_seconds=_clamp_int(data.get("panda_sync_timeout_seconds"), int(panda_defaults.get("timeout_seconds") or 60), 5, 300),
            panda_sync_remove_local_on_success=bool(data.get("panda_sync_remove_local_on_success", panda_defaults.get("remove_local_on_success", False))),
            panda_sync_cooldown_seconds=_clamp_float(data.get("panda_sync_cooldown_seconds"), float(panda_defaults.get("cooldown_seconds") or 2.0), 0.0, 60.0),
            panda_sync_queue_on_failure=bool(data.get("panda_sync_queue_on_failure", panda_defaults.get("queue_on_failure", False))),
            token_overrides=tuple(
                dict.fromkeys(
                    str(token or "").strip()
                    for token in (data.get("tokens") if isinstance(data.get("tokens"), list) else [])
                    if str(token or "").strip()
                )
            ),
        )


class AccountRefreshAllService:
    def __init__(self, service: AccountService):
        self._service = service
        self._lock = Lock()
        self._worker_lock = Lock()
        self._throttle_lock = Lock()
        self._panda_sync_lock = Lock()
        self._thread: Thread | None = None
        self._stop_event = Event()
        self._tokens: list[str] = []
        self._next_index = 0
        self._last_start_at = 0.0
        self._last_panda_sync_at = 0.0
        self._last_panda_stats_at = 0.0
        self._last_panda_stats: dict[str, Any] | None = None
        self._pending_file = PANDA_SYNC_PENDING_FILE
        self._status: dict[str, Any] = self._idle_status()

    @staticmethod
    def _idle_status() -> dict[str, Any]:
        default_options = AccountRefreshAllOptions.from_mapping({})
        return {
            "job_id": "",
            "state": "idle",
            "running": False,
            "started_at": None,
            "finished_at": None,
            "last_update_at": None,
            "total": 0,
            "processed": 0,
            "refreshed": 0,
            "available": 0,
            "became_available": 0,
            "quota_total": 0,
            "unlimited_quota": 0,
            "unknown_quota": 0,
            "failed": 0,
            "removed": 0,
            "skipped": 0,
            "expired_removed": 0,
            "synced_to_panda": 0,
            "sync_failed": 0,
            "queued_for_panda": 0,
            "pause_reason": "",
            "current_token": "",
            "recent": [],
            "options": AccountRefreshAllService._public_options(default_options),
            "resource": {},
        }

    @staticmethod
    def _public_options(options: AccountRefreshAllOptions) -> dict[str, Any]:
        data = asdict(options)
        data.pop("panda_sync_auth_key", None)
        data.pop("token_overrides", None)
        data["panda_sync_has_auth_key"] = bool(options.panda_sync_auth_key)
        return data

    def _load_pending_sync_accounts(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._pending_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        accounts = payload.get("accounts") if isinstance(payload, dict) else payload
        return [item for item in accounts if isinstance(item, dict)] if isinstance(accounts, list) else []

    def _save_pending_sync_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._pending_file.parent.mkdir(parents=True, exist_ok=True)
        deduped: dict[str, dict[str, Any]] = {}
        for account in accounts:
            token = str(account.get("access_token") or account.get("accessToken") or "").strip()
            if token:
                deduped[token] = {**deduped.get(token, {}), **account, "access_token": token}
        self._pending_file.write_text(
            json.dumps({"accounts": list(deduped.values()), "updated_at": _iso_now()}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _append_pending_sync_accounts(self, accounts: list[dict[str, Any]]) -> None:
        if not accounts:
            return
        self._save_pending_sync_accounts([*self._load_pending_sync_accounts(), *accounts])

    def _retry_pending_sync(self, options: AccountRefreshAllOptions) -> tuple[int, int, int]:
        if not options.panda_sync_enabled or not options.panda_sync_queue_on_failure:
            return 0, 0, 0
        pending = self._load_pending_sync_accounts()
        if not pending:
            return 0, 0, 0
        capacity = self._panda_upload_capacity(options, len(pending))
        if capacity <= 0:
            return 0, 0, 0
        batch = pending[: options.panda_sync_batch_size]
        batch = batch[:capacity]
        synced, failed = self._sync_accounts_to_panda(batch, options, persist_failures=False)
        remaining = pending[synced:] if synced else pending
        self._save_pending_sync_accounts(remaining)
        return synced, failed, 0

    def _fetch_panda_stats(self, options: AccountRefreshAllOptions, *, force: bool = False) -> dict[str, Any] | None:
        if not options.panda_sync_enabled:
            return None
        settings = config.get_panda_sync_settings()
        ttl = max(0, int(settings.get("remote_stats_ttl_sec") or 0))
        now = time.monotonic()
        if not force and self._last_panda_stats is not None and ttl > 0 and now - self._last_panda_stats_at <= ttl:
            return dict(self._last_panda_stats)
        url = f"{options.panda_sync_base_url.rstrip('/')}/api/accounts?offset=0&limit=0"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {options.panda_sync_auth_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=options.panda_sync_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        stats = payload.get("stats") if isinstance(payload, dict) else None
        if not isinstance(stats, dict):
            stats = {}
        total = payload.get("total") if isinstance(payload, dict) else None
        normalized = {
            "total": int(total or stats.get("total") or 0),
            "active": int(stats.get("active") or 0),
            "schedulable": int(stats.get("schedulable") or stats.get("active") or 0),
            "total_quota": int(stats.get("total_quota") or 0),
            "verified_total_quota": int(stats.get("verified_total_quota") or 0),
        }
        self._last_panda_stats = normalized
        self._last_panda_stats_at = now
        return dict(normalized)

    def _fetch_panda_account_tokens(self, options: AccountRefreshAllOptions) -> set[str] | None:
        if not options.panda_sync_enabled:
            return None
        url = f"{options.panda_sync_base_url.rstrip('/')}/api/accounts?offset=0&limit=10000"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {options.panda_sync_auth_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=options.panda_sync_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return set()
        tokens: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            token = str(item.get("access_token") or item.get("accessToken") or "").strip()
            if token:
                tokens.add(token)
        return tokens

    def _panda_upload_capacity(self, options: AccountRefreshAllOptions, candidate_count: int) -> int:
        if candidate_count <= 0:
            return 0
        settings = config.get_panda_sync_settings()
        upload_max = max(1, int(settings.get("upload_max_batch") or options.panda_sync_batch_size or 20))
        if not bool(settings.get("watermark_enabled", True)):
            return min(candidate_count, upload_max)
        stats = self._fetch_panda_stats(options)
        if not stats:
            # 查不到远端水位时保守地只放一个小批次，避免“远端已满还继续灌”。
            return min(candidate_count, upload_max)
        current = (
            int(stats.get("schedulable") or 0)
            or int(stats.get("active") or 0)
            or int(stats.get("total") or 0)
        )
        high = max(1, int(settings.get("high_watermark") or 1500))
        low = max(0, int(settings.get("low_watermark") or 500))
        if current >= high:
            return 0
        # 高低水位迟滞：低水位以上只保留本地 staging/ready，减少 Panda 探活/写盘压力。
        if current > low:
            return 0
        return min(candidate_count, max(0, high - current), upload_max)

    def _panda_remote_below_low_watermark(self, options: AccountRefreshAllOptions) -> bool:
        """远端 Panda 低水位判断。

        仅用于“刚刷新确认活号”的应急上传通道；拿不到远端水位时保持保守，不放行
        staging，避免因为网络瞬断把未成熟号批量灌进生产 Panda。
        """
        if not options.panda_sync_enabled:
            return False
        settings = config.get_panda_sync_settings()
        if not bool(settings.get("watermark_enabled", True)):
            return True
        stats = self._fetch_panda_stats(options)
        if not stats:
            return False
        current = (
            int(stats.get("schedulable") or 0)
            or int(stats.get("active") or 0)
            or int(stats.get("total") or 0)
        )
        low = max(0, int(settings.get("low_watermark") or 500))
        return current <= low

    def _maybe_emergency_promote_for_panda_sync(
        self,
        account: dict[str, Any],
        options: AccountRefreshAllOptions,
    ) -> dict[str, Any]:
        """Panda 低水位时，把“本轮刚验证活的 staging 号”临时晋级为 ready。

        这不是取消 1h/3h/6h 成熟度机制，而是解决生产 Panda 快空时的应急补池：
        - 未经过本轮 refresh_all 成功验证的 staging 号仍不会上传；
        - 远端水位不低时仍按成熟度队列慢慢上传；
        - 远端水位不可确认时不放行。
        """
        if not account or str(account.get("panda_sync_state") or "").lower() != "staging":
            return account
        if not options.panda_sync_requested or not self._panda_remote_below_low_watermark(options):
            return account
        token = str(account.get("access_token") or account.get("accessToken") or "").strip()
        if not token:
            return account
        updates = {
            "panda_sync_state": "ready",
            "panda_ready_at": _iso_now(),
            "panda_probe_last_at": _iso_now(),
            "panda_probe_last_error": None,
            "panda_emergency_promoted_at": _iso_now(),
        }
        return self._service.update_account(token, updates, quiet=True) or {**account, **updates}

    @staticmethod
    def _is_panda_sync_ready(
        account: dict[str, Any],
        remote_tokens: set[str] | None = None,
    ) -> bool:
        state = str(account.get("panda_sync_state") or "").strip().lower()
        token = str(account.get("access_token") or account.get("accessToken") or "").strip()
        if state != "ready":
            if state != "synced" or remote_tokens is None or not token or token in remote_tokens:
                return False
            # 旧策略会在本地保留 synced 账号；如果 Panda 已经删掉或没有该 token，
            # 这里允许重新上传补池。
            state = "ready"
        if state != "ready":
            return False
        if not AccountService._is_image_account_available(account):
            return False
        if AccountService._has_image_account_failure_evidence(account):
            return False
        if _parse_time(account.get("last_quota_refresh_at")) is None:
            return False
        if account.get("panda_probe_last_error"):
            return False
        return True

    @staticmethod
    def _retry_after_seconds(error: urllib.error.HTTPError) -> int:
        raw = str(error.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                return max(1, min(300, int(float(raw))))
            except ValueError:
                pass
        try:
            body = error.read().decode("utf-8", "replace")
            payload = json.loads(body)
        except Exception:
            return 0
        detail = payload.get("detail") if isinstance(payload, dict) else None
        message = ""
        if isinstance(detail, dict):
            message = str(detail.get("error") or "")
        elif detail is not None:
            message = str(detail)
        match = None
        try:
            import re

            match = re.search(r"retry after\s+(\d+)s", message, re.IGNORECASE)
        except Exception:
            match = None
        if not match:
            return 0
        return max(1, min(300, int(match.group(1))))

    def _purge_expired_tokens(self, options: AccountRefreshAllOptions) -> int:
        if not options.delete_invalid:
            return 0
        grace_seconds = max(0, int(options.expired_grace_hours or 0)) * 3600
        now_ts = int(time.time())
        expired: list[str] = []
        for account in self._service.list_accounts():
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            remaining = AccountService._token_expires_in(token)
            has_refresh_token = bool(str(account.get("refresh_token") or "").strip())
            if remaining is not None and remaining < -grace_seconds and not has_refresh_token:
                expired.append(token)
            elif remaining is not None and now_ts > 0 and remaining < -grace_seconds and str(account.get("status") or "") in {"异常", "禁用"}:
                expired.append(token)
        if not expired:
            return 0
        return int(self._service.delete_accounts(expired, include_items=False).get("removed") or 0)

    def _is_active_locked(self) -> bool:
        state = str(self._status.get("state") or "")
        return state in {"running", "paused", "stopping"} and bool(self._thread and self._thread.is_alive())

    def is_active(self) -> bool:
        with self._lock:
            return self._is_active_locked()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def start(self, options: dict[str, Any] | AccountRefreshAllOptions | None = None) -> dict[str, Any]:
        if isinstance(options, AccountRefreshAllOptions):
            normalized = options
        else:
            normalized = AccountRefreshAllOptions.from_mapping(options)

        expired_removed = self._purge_expired_tokens(normalized)
        tokens, skipped = self._build_token_queue(normalized)
        has_pending_sync = (
            normalized.panda_sync_enabled
            and normalized.panda_sync_queue_on_failure
            and bool(self._load_pending_sync_accounts())
        )
        with self._lock:
            if self._is_active_locked():
                raise RuntimeError("已有慢速刷新任务正在运行")
            job_id = str(uuid.uuid4())
            self._tokens = tokens
            self._next_index = 0
            self._last_start_at = 0.0
            self._stop_event = Event()
            self._status = {
                **self._idle_status(),
                "job_id": job_id,
                "state": "running" if tokens or has_pending_sync else "completed",
                "running": bool(tokens or has_pending_sync),
                "started_at": _iso_now(),
                "finished_at": None if tokens or has_pending_sync else _iso_now(),
                "last_update_at": _iso_now(),
                "total": len(tokens),
                "skipped": skipped,
                "expired_removed": expired_removed,
                "options": self._public_options(normalized),
            }
            if not tokens and not has_pending_sync:
                return dict(self._status)
            self._thread = Thread(
                target=self._run,
                args=(job_id, normalized),
                name="account-refresh-all",
                daemon=True,
            )
            self._thread.start()
            return dict(self._status)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if str(self._status.get("state") or "") in {"running", "paused"}:
                self._status["state"] = "stopping"
                self._status["last_update_at"] = _iso_now()
            self._stop_event.set()
            return dict(self._status)

    def _build_token_queue(self, options: AccountRefreshAllOptions) -> tuple[list[str], int]:
        if options.token_overrides:
            accounts_by_token = {
                str(account.get("access_token") or ""): account
                for account in self._service.list_accounts()
                if str(account.get("access_token") or "")
            }
            tokens: list[str] = []
            skipped = 0
            for token in options.token_overrides:
                account = accounts_by_token.get(token)
                if account is None:
                    continue
                if _is_terminal_outlook_recovery(account):
                    skipped += 1
                    continue
                tokens.append(token)
            if options.limit is not None:
                skipped += max(0, len(tokens) - options.limit)
                tokens = tokens[: options.limit]
            return tokens, skipped
        now = _utc_now()
        stale_cutoff = now - timedelta(hours=options.stale_after_hours)
        candidates: list[tuple[tuple[int, int, float], str]] = []
        skipped = 0
        status_priority = {"限流": 0, "异常": 1, "正常": 2}

        for account in self._service.list_accounts():
            token = str(account.get("access_token") or "").strip()
            if not token:
                skipped += 1
                continue
            if _is_terminal_outlook_recovery(account):
                skipped += 1
                continue
            status = str(account.get("status") or "正常").strip()
            if status == "禁用":
                skipped += 1
                continue
            last_refresh = _parse_time(account.get("last_quota_refresh_at"))
            is_problematic_status = status in {"限流", "异常"}
            if (
                not is_problematic_status
                and
                not options.include_recent
                and options.stale_after_hours > 0
                and last_refresh is not None
                and last_refresh >= stale_cutoff
            ):
                skipped += 1
                continue
            quota = max(0, int(account.get("quota") or 0))
            missing_quota = 0 if quota <= 0 or bool(account.get("image_quota_unknown")) else 1
            last_ts = last_refresh.timestamp() if last_refresh is not None else 0.0
            key = (status_priority.get(status, 2), missing_quota, last_ts)
            candidates.append((key, token))

        candidates.sort(key=lambda item: item[0])
        tokens = [token for _, token in candidates]
        if options.limit is not None:
            skipped += max(0, len(tokens) - options.limit)
            tokens = tokens[: options.limit]
        return tokens, skipped

    def _sample_resource(self) -> tuple[bool, str, dict[str, Any]]:
        current_bytes, limit_bytes = _read_cgroup_memory()
        available_bytes: int | None = None
        if current_bytes is not None and limit_bytes is not None:
            available_bytes = max(0, limit_bytes - current_bytes)
        else:
            available_bytes = _read_mem_available_bytes()

        load_1m: float | None = None
        if hasattr(os, "getloadavg"):
            try:
                load_1m = float(os.getloadavg()[0])
            except OSError:
                load_1m = None

        resource = {
            "available_memory_mb": round(available_bytes / 1024 / 1024, 1) if available_bytes is not None else None,
            "memory_current_mb": round(current_bytes / 1024 / 1024, 1) if current_bytes is not None else None,
            "memory_limit_mb": round(limit_bytes / 1024 / 1024, 1) if limit_bytes is not None else None,
            "load_1m": round(load_1m, 2) if load_1m is not None else None,
        }
        return True, "", resource

    def _resource_ok(self, options: AccountRefreshAllOptions) -> tuple[bool, str, dict[str, Any]]:
        _base_ok, _base_reason, resource = self._sample_resource()
        reasons: list[str] = []
        available_mb = resource.get("available_memory_mb")
        load_1m = resource.get("load_1m")
        if (
            options.min_available_memory_mb > 0
            and isinstance(available_mb, (int, float))
            and available_mb < options.min_available_memory_mb
        ):
            reasons.append(f"available memory {available_mb}MB < {options.min_available_memory_mb}MB")
        if (
            options.max_load_1m > 0
            and isinstance(load_1m, (int, float))
            and load_1m > options.max_load_1m
        ):
            reasons.append(f"load {load_1m} > {options.max_load_1m}")
        return not reasons, "; ".join(reasons), resource

    def _wait_for_resource(self, options: AccountRefreshAllOptions) -> bool:
        while not self._stop_event.is_set():
            ok, reason, resource = self._resource_ok(options)
            with self._lock:
                self._status["resource"] = resource
                self._status["last_update_at"] = _iso_now()
                if ok:
                    if self._status.get("state") == "paused":
                        self._status["state"] = "running"
                    if str(self._status.get("pause_reason") or "").startswith("resource degraded"):
                        self._status["pause_reason"] = ""
                    return True
                if not options.resource_pause_enabled:
                    self._status["pause_reason"] = f"resource degraded, continuing slowly: {reason}"
                    return True
                self._status["state"] = "paused"
                self._status["pause_reason"] = reason
            if self._stop_event.wait(options.resource_check_interval_sec):
                return False
        return False

    def _wait_for_throttle(self, options: AccountRefreshAllOptions) -> bool:
        with self._throttle_lock:
            wait_sec = max(0.0, self._last_start_at + options.delay_between_accounts_sec - time.monotonic())
            if wait_sec > 0 and self._stop_event.wait(wait_sec):
                return False
            self._last_start_at = time.monotonic()
        return True

    def _next_token(self) -> tuple[int, str] | None:
        with self._worker_lock:
            if self._stop_event.is_set():
                return None
            if self._next_index >= len(self._tokens):
                return None
            index = self._next_index
            self._next_index += 1
            return index, self._tokens[index]

    def _append_recent(self, item: dict[str, Any]) -> None:
        recent = list(self._status.get("recent") or [])
        recent.append(item)
        self._status["recent"] = recent[-20:]

    def _sync_accounts_to_panda(
        self,
        accounts: list[dict[str, Any]],
        options: AccountRefreshAllOptions,
        *,
        persist_failures: bool = True,
    ) -> tuple[int, int]:
        if not options.panda_sync_enabled or not accounts:
            return 0, 0
        with self._panda_sync_lock:
            cooldown = max(0.0, float(options.panda_sync_cooldown_seconds or 0.0))
            wait_sec = max(0.0, self._last_panda_sync_at + cooldown - time.monotonic())
            if wait_sec > 0 and self._stop_event.wait(wait_sec):
                if persist_failures and options.panda_sync_queue_on_failure:
                    self._append_pending_sync_accounts(accounts)
                return 0, len(accounts)

            url = f"{options.panda_sync_base_url.rstrip('/')}/api/accounts/import-batch?include_items=false"
            body = json.dumps({"accounts": accounts}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {options.panda_sync_auth_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            payload: dict[str, Any] | None = None
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=options.panda_sync_timeout_seconds) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    self._last_panda_sync_at = time.monotonic()
                    if exc.code == 429:
                        retry_after = self._retry_after_seconds(exc) or int(cooldown) or 30
                        if attempt < 2 and not self._stop_event.wait(retry_after):
                            continue
                    if persist_failures and options.panda_sync_queue_on_failure:
                        self._append_pending_sync_accounts(accounts)
                    return 0, len(accounts)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                    self._last_panda_sync_at = time.monotonic()
                    remote_tokens = self._fetch_panda_account_tokens(options)
                    if remote_tokens:
                        accepted_accounts = [
                            account
                            for account in accounts
                            if str(account.get("access_token") or "").strip() in remote_tokens
                        ]
                        if accepted_accounts and options.panda_sync_remove_local_on_success:
                            log_service.add(
                                LOG_TYPE_ACCOUNT,
                                "上传到 Panda",
                                {
                                    "accepted": len(accepted_accounts),
                                    "added": 0,
                                    "updated": 0,
                                    "skipped": len(accepted_accounts),
                                    "deleted_local": len(accepted_accounts),
                                    "retained_local": 0,
                                    "reconciled_after_error": True,
                                },
                            )
                            self._service.delete_accounts([
                                str(account.get("access_token") or "")
                                for account in accepted_accounts
                                if str(account.get("access_token") or "").strip()
                            ], include_items=False)
                        accepted = len(accepted_accounts)
                        if accepted:
                            return accepted, max(0, len(accounts) - accepted)
                    if persist_failures and options.panda_sync_queue_on_failure:
                        self._append_pending_sync_accounts(accounts)
                    return 0, len(accounts)
            self._last_panda_sync_at = time.monotonic()
        if payload is None:
            return 0, len(accounts)
        added = int(payload.get("added") or 0)
        skipped = int(payload.get("skipped") or 0)
        updated = int(payload.get("updated") or 0)
        accepted = max(0, min(len(accounts), added + skipped + updated))
        if accepted:
            accepted_accounts = accounts[:accepted]
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "上传到 Panda",
                {
                    "accepted": accepted,
                    "added": added,
                    "updated": updated,
                    "skipped": skipped,
                    "deleted_local": accepted if options.panda_sync_remove_local_on_success else 0,
                    "retained_local": 0 if options.panda_sync_remove_local_on_success else accepted,
                },
            )
            if options.panda_sync_remove_local_on_success:
                self._service.delete_accounts([
                    str(account.get("access_token") or "")
                    for account in accepted_accounts
                    if str(account.get("access_token") or "").strip()
                ], include_items=False)
            else:
                synced_at = _iso_now()
                for account in accepted_accounts:
                    token = str(account.get("access_token") or "").strip()
                    if token:
                        self._service.update_account(
                            token,
                            {"panda_sync_state": "synced", "panda_synced_at": synced_at},
                            quiet=True,
                        )
        return accepted, max(0, len(accounts) - accepted)

    def _queue_or_sync_accounts_to_panda(
        self,
        accounts: list[dict[str, Any]],
        options: AccountRefreshAllOptions,
        *,
        remote_tokens: set[str] | None = None,
    ) -> tuple[int, int, int]:
        prepared: list[dict[str, Any]] = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if not self._is_panda_sync_ready(account, remote_tokens):
                continue
            token = str(account.get("access_token") or account.get("accessToken") or "").strip()
            if not token:
                continue
            try:
                prepared.append(
                    self._prepare_account_for_panda_upload({**account, "access_token": token})
                )
            except ValueError as exc:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "Panda 上传跳过身份不完整账号",
                    {"token": anonymize_token(token), "error": str(exc)[:240]},
                )
        if not prepared or not options.panda_sync_requested:
            return 0, 0, 0
        if not options.panda_sync_enabled:
            if options.panda_sync_queue_on_failure:
                self._append_pending_sync_accounts(prepared)
                return 0, 0, len(prepared)
            return 0, len(prepared), 0
        synced, failed = self._sync_accounts_to_panda(prepared, options)
        return synced, failed, 0

    @staticmethod
    def _prepare_account_for_panda_upload(account: dict[str, Any]) -> dict[str, Any]:
        prepared = normalize_account_identity(dict(account))
        lifecycle = str(prepared.get("lifecycle_ip_mode") or "").strip().lower()
        proxy_provider = str(prepared.get("proxy_provider") or "").strip().lower()
        sticky = lifecycle in {"sticky_one_ip_full", "account_sticky"} or proxy_provider in {
            "webshare",
            "udeal",
        }
        has_proxy = bool(str(prepared.get("proxy") or "").strip())

        # 本地环回代理对 Panda 不可达：非 sticky 仍可清空后进入 incoming；
        # sticky 路径必须在上传前失败，避免假 ready。
        if is_local_only_proxy_url(prepared.get("proxy")):
            if sticky:
                raise ValueError(
                    "account_identity_incomplete: proxy_reachable_from_panda"
                )
            prepared.update(
                {
                    "proxy": "",
                    "proxy_scope": "panda_runtime_default",
                    "proxy_egress_hash": None,
                    "panda_receive_state": "incoming",
                    "panda_sync_last_error": None,
                }
            )
            return prepared

        # 账号级 sticky/住宅节点：上传前必须身份字段完整。
        if sticky and has_proxy:
            missing = missing_panda_identity_fields(prepared)
            if missing:
                raise ValueError("account_identity_incomplete: " + ",".join(missing))
            prepared["proxy_scope"] = "account_sticky"
            return prepared

        # 无账号代理或非 sticky：允许走 Panda 运行时默认出口（legacy），不强制 egress/fp。
        if has_proxy:
            prepared.setdefault("proxy_scope", "account_proxy")
            return prepared
        prepared.setdefault("proxy_scope", "panda_runtime_default")
        return prepared

    @staticmethod
    def _panda_sync_candidate_reason(
        account: dict[str, Any],
        remote_tokens: set[str] | None = None,
    ) -> str:
        state = str((account or {}).get("panda_sync_state") or "").strip().lower()
        token = str((account or {}).get("access_token") or (account or {}).get("accessToken") or "").strip()
        if state == "synced":
            if remote_tokens is not None and token and token not in remote_tokens:
                state = "ready"
            else:
                return "already_remote"
        if state != "ready":
            return "state_not_ready"
        if not AccountService._is_image_account_available(account):
            return "quota_or_status"
        if AccountService._has_image_account_failure_evidence(account):
            return "failure_evidence"
        if _parse_time((account or {}).get("last_quota_refresh_at")) is None:
            return "missing_quota_refresh"
        if str((account or {}).get("panda_probe_last_error") or "").strip():
            return "probe_error"
        return "remote_missing_reupload" if str((account or {}).get("panda_sync_state") or "").strip().lower() == "synced" else "eligible"

    def queue_available_accounts_for_panda(self, accounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        candidates = accounts if accounts is not None else self._service.list_accounts()
        options = AccountRefreshAllOptions.from_mapping({})
        remote_tokens = self._fetch_panda_account_tokens(options) if options.panda_sync_enabled else None
        details: dict[str, Any] = {
            "scanned": len(candidates),
            "eligible": 0,
            "remote_missing_reupload": 0,
            "already_remote": 0,
            "blocked_by_config": 0,
            "blocked_by_watermark": 0,
            "blocked_by_state": 0,
            "blocked_by_quota_or_status": 0,
            "blocked_by_failure_evidence": 0,
            "blocked_by_missing_quota_refresh": 0,
            "blocked_by_probe_error": 0,
            "remote_token_snapshot": "unavailable" if options.panda_sync_enabled and remote_tokens is None else "ok",
            "deleted_local": 0,
        }
        available: list[dict[str, Any]] = []
        for account in candidates:
            if not AccountService._is_image_account_available(account):
                details["blocked_by_quota_or_status"] += 1
                continue
            reason = self._panda_sync_candidate_reason(account, remote_tokens)
            if reason in {"eligible", "remote_missing_reupload"} and self._is_panda_sync_ready(account, remote_tokens):
                if reason != "eligible":
                    details[reason] += 1
                details["eligible"] += 1
                available.append(account)
            elif reason == "already_remote":
                details["already_remote"] += 1
            elif reason == "failure_evidence":
                details["blocked_by_failure_evidence"] += 1
            elif reason == "missing_quota_refresh":
                details["blocked_by_missing_quota_refresh"] += 1
            elif reason == "probe_error":
                details["blocked_by_probe_error"] += 1
            elif reason == "quota_or_status":
                details["blocked_by_quota_or_status"] += 1
            else:
                details["blocked_by_state"] += 1
        if not available:
            if not options.panda_sync_requested or not options.panda_sync_enabled:
                details["blocked_by_config"] = details["eligible"]
            return {"synced": 0, "failed": 0, "queued": 0, "details": details}
        capacity = self._panda_upload_capacity(options, len(available)) if options.panda_sync_enabled else len(available)
        if capacity <= 0:
            details["blocked_by_watermark"] = len(available)
            return {"synced": 0, "failed": 0, "queued": 0, "details": details}
        available = available[:capacity]

        synced = failed = queued = 0
        settings = config.get_panda_sync_settings()
        batch_size = max(1, min(int(options.panda_sync_batch_size or 20), int(settings.get("upload_max_batch") or 20)))
        for offset in range(0, len(available), batch_size):
            batch = available[offset: offset + batch_size]
            batch_synced, batch_failed, batch_queued = self._queue_or_sync_accounts_to_panda(
                batch,
                options,
                remote_tokens=remote_tokens,
            )
            synced += batch_synced
            failed += batch_failed
            queued += batch_queued
        if options.panda_sync_remove_local_on_success:
            details["deleted_local"] = synced
        return {"synced": synced, "failed": failed, "queued": queued, "details": details}

    def queue_refreshed_tokens_for_panda(self, tokens: list[str] | None = None) -> dict[str, Any]:
        """把刚刷新过的 token 对应的可用账号，立即进入 Panda 增量同步。

        这个入口给手动刷新和定时 watcher 用：只同步本次刚处理的账号，
        避免把整池账号都扫一遍，更不会改变 Panda 上传接口的行为。
        """
        target_tokens = [str(token or "").strip() for token in (tokens or []) if str(token or "").strip()]
        if not target_tokens:
            return {"synced": 0, "failed": 0, "queued": 0}

        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for token in target_tokens:
            resolved = self._service.resolve_access_token(token)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            account = self._service.get_account(resolved)
            if account is not None:
                accounts.append(account)
        return self.queue_available_accounts_for_panda(accounts)

    @staticmethod
    def _is_invalid_token_error(error: str) -> bool:
        lowered = str(error or "").lower()
        invalid_markers = (
            "token invalidated",
            "invalid access token",
            "invalid_access_token",
            "unauthorized",
            "http 401",
            "account_deactivated",
        )
        return any(marker in lowered for marker in invalid_markers)

    @staticmethod
    def _is_transient_refresh_error(error: str) -> bool:
        lowered = str(error or "").lower()
        transient_markers = (
            "connect tunnel failed",
            "response 502",
            "response 503",
            "response 504",
            "http 408",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "too many requests",
            "rate limit",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "timeout",
            "timed out",
            "cloudflare",
            "cf_clearance",
            "challenge",
            "tls",
            "ssl",
            "connection",
            "proxy",
            "network",
            "curl: (28)",
            "curl: (35)",
            "curl: (56)",
        )
        return any(marker in lowered for marker in transient_markers)

    @classmethod
    def _has_recent_transient_token_error(cls, account: dict[str, Any]) -> bool:
        error = str(account.get("last_token_refresh_error") or account.get("last_refresh_error") or "")
        if not cls._is_transient_refresh_error(error):
            return False
        happened_at = _parse_time(account.get("last_token_refresh_error_at") or account.get("last_refresh_error_at"))
        if happened_at is None:
            return False
        return (_utc_now() - happened_at).total_seconds() <= 600

    def _record_failure(self, token: str, error: str, options: AccountRefreshAllOptions) -> bool:
        account = self._service.get_account(token)
        if not account:
            return False

        fail_count = int(account.get("quota_refresh_fail_count") or 0) + 1
        invalid_error = self._is_invalid_token_error(error)
        if invalid_error:
            delete_threshold = 1
            should_delete = options.delete_invalid and fail_count >= delete_threshold
            if should_delete:
                return bool(self._service.delete_accounts([token], include_items=False).get("removed"))
            self._service.update_account(
                token,
                {
                    "last_quota_refresh_error": error[:500],
                    "quota_refresh_fail_count": fail_count,
                    "quota_refresh_failure_kind": "invalid",
                    "status": "异常",
                    "quota": 0,
                    "image_quota_unknown": False,
                    "panda_receive_state": "rejected",
                    "panda_rejected_at": _iso_now(),
                    "panda_verify_last_error": error[:500],
                },
                quiet=True,
            )
            return False

        is_transient = self._is_transient_refresh_error(error) or self._has_recent_transient_token_error(account)
        if is_transient:
            self._service.update_account(
                token,
                {
                    "last_quota_refresh_error": error[:500],
                    "quota_refresh_fail_count": fail_count,
                    "quota_refresh_failure_kind": "transient",
                    "quota_refresh_quarantined_at": _iso_now(),
                    "panda_verify_last_error": error[:500],
                },
                quiet=True,
            )
            return False

        delete_threshold = max(1, int(options.delete_after_failures or 1))
        should_delete = options.delete_invalid and fail_count >= delete_threshold
        if should_delete:
            return bool(self._service.delete_accounts([token], include_items=False).get("removed"))

        updates: dict[str, Any] = {
            "last_quota_refresh_error": error[:500],
            "quota_refresh_fail_count": fail_count,
            "quota_refresh_failure_kind": "failed",
            "panda_verify_last_error": error[:500],
        }
        self._service.update_account(
            token,
            updates,
            quiet=True,
        )
        return False

    def _process_token(self, index: int, token: str, options: AccountRefreshAllOptions) -> None:
        before = self._service.get_account(token)
        was_available = AccountService._is_image_account_available(before or {})
        with self._lock:
            self._status["current_token"] = anonymize_token(token)
            self._status["last_update_at"] = _iso_now()

        try:
            account = self._service.fetch_remote_info(token, "refresh_all_accounts", True)
        except Exception as exc:
            error = str(exc)
            removed = self._record_failure(token, error, options)
            with self._lock:
                self._status["processed"] += 1
                self._status["failed"] += 1
                if removed:
                    self._status["removed"] += 1
                self._append_recent({
                    "index": index + 1,
                    "token": anonymize_token(token),
                    "status": "removed" if removed else "failed",
                    "error": error[:200],
                })
                self._status["last_update_at"] = _iso_now()
            return

        refreshed_token = str((account or {}).get("access_token") or token)
        previous_token = str((before or {}).get("access_token") or token)
        previous_sync_state = str((before or {}).get("panda_sync_state") or "").strip().lower()
        if previous_sync_state == "local_proxy_only":
            next_sync_state = "local_proxy_only"
        elif previous_sync_state == "synced" and refreshed_token == previous_token:
            next_sync_state = "synced"
        else:
            next_sync_state = "ready"
        updated = self._service.update_account(
            refreshed_token,
            {
                "last_quota_refresh_at": _iso_now(),
                "last_quota_refresh_error": None,
                "quota_refresh_fail_count": 0,
                "quota_refresh_failure_kind": None,
                "quota_refresh_quarantined_at": None,
                "panda_receive_state": "verified_ready",
                "panda_verified_at": _iso_now(),
                "panda_verify_last_error": None,
                "panda_sync_state": next_sync_state,
            },
            quiet=True,
        ) or account
        is_available = AccountService._is_image_account_available(updated or {})
        quota = max(0, int((updated or {}).get("quota") or 0))
        is_true_unlimited = AccountService._is_true_unlimited_image_account(updated or {})
        quota_unknown = AccountService._is_unknown_image_quota_account(updated or {})
        status = str((updated or {}).get("status") or "正常")

        with self._lock:
            self._status["processed"] += 1
            self._status["refreshed"] += 1
            if is_available:
                self._status["available"] += 1
                if is_true_unlimited:
                    self._status["unlimited_quota"] += 1
                elif quota_unknown:
                    self._status["unknown_quota"] += 1
                else:
                    self._status["quota_total"] += quota
            if is_available and not was_available:
                self._status["became_available"] += 1
            self._append_recent({
                "index": index + 1,
                "token": anonymize_token(refreshed_token),
                "status": status,
                "quota": quota,
                "quota_unknown": quota_unknown,
                "available": is_available,
            })
        self._status["last_update_at"] = _iso_now()

    def _worker(self, options: AccountRefreshAllOptions) -> None:
        sync_buffer: list[dict[str, Any]] = []
        def flush_sync_buffer(*, final: bool = False) -> None:
            nonlocal sync_buffer
            if not sync_buffer:
                return
            if not final and len(sync_buffer) < options.panda_sync_batch_size:
                return
            synced, failed, queued = self._queue_or_sync_accounts_to_panda(sync_buffer, options)
            with self._lock:
                self._status["synced_to_panda"] += synced
                self._status["sync_failed"] += failed
                self._status["queued_for_panda"] += queued
                self._status["last_update_at"] = _iso_now()
            sync_buffer = []

        while not self._stop_event.is_set():
            item = self._next_token()
            if item is None:
                flush_sync_buffer(final=True)
                return
            index, token = item
            if not self._wait_for_resource(options):
                flush_sync_buffer(final=True)
                return
            if not self._wait_for_throttle(options):
                flush_sync_buffer(final=True)
                return
            self._process_token(index, token, options)
            resolved_token = self._service.resolve_access_token(token)
            account = self._service.get_account(resolved_token)
            if (
                options.panda_sync_requested
                and account
                and AccountService._is_image_account_available(account)
            ):
                account = self._maybe_emergency_promote_for_panda_sync(account, options)
                sync_buffer.append(account)
                if len(sync_buffer) >= options.panda_sync_batch_size:
                    flush_sync_buffer()
            if (
                options.batch_size > 0
                and options.delay_between_batches_sec > 0
                and (index + 1) % options.batch_size == 0
            ):
                flush_sync_buffer(final=True)
                if self._stop_event.wait(options.delay_between_batches_sec):
                    return
        flush_sync_buffer(final=True)

    def _run(self, job_id: str, options: AccountRefreshAllOptions) -> None:
        try:
            if options.panda_sync_enabled:
                synced, failed, queued = self._retry_pending_sync(options)
                if synced or failed or queued:
                    with self._lock:
                        self._status["synced_to_panda"] += synced
                        self._status["sync_failed"] += failed
                        self._status["queued_for_panda"] += queued
                        self._status["last_update_at"] = _iso_now()
            workers = [
                Thread(target=self._worker, args=(options,), name=f"account-refresh-all-{i + 1}", daemon=True)
                for i in range(options.concurrency)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            with self._lock:
                if self._status.get("job_id") != job_id:
                    return
                stopped = self._stop_event.is_set()
                self._status["state"] = "stopped" if stopped else "completed"
                self._status["running"] = False
                self._status["finished_at"] = _iso_now()
                self._status["last_update_at"] = _iso_now()
                self._status["current_token"] = ""
                if stopped and not self._status.get("pause_reason"):
                    self._status["pause_reason"] = "stopped by user"

    def sync_last_refreshed_accounts_to_panda(self) -> dict[str, Any]:
        """把最近一次 refresh_accounts 实际处理过的账号，直接交给 Panda 增量同步。"""
        tokens = self._service.pop_last_refresh_tokens()
        if not tokens:
            return {"synced": 0, "failed": 0, "queued": 0}
        return self.queue_refreshed_tokens_for_panda(tokens)


account_refresh_all_service = AccountRefreshAllService(account_service)
