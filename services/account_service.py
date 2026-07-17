from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Lock, RLock, Thread
from typing import Any, Callable
from urllib.parse import urlencode

from services.account_identity import (
    merge_account_identity,
    normalize_account_identity,
)
from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    _NEW_ACCOUNT_INVALID_GRACE_SECONDS = 10 * 60
    _INVALID_CONFIRM_SECONDS = 30
    _ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_SECONDS = 3 * 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS = 6 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE = 3
    _TOKEN_REFRESH_ERROR_BACKOFF_SECONDS = 5 * 60
    _OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    _OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
    _OAUTH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 刷新进度追踪
    _refresh_progress: dict[str, dict] = {}
    _refresh_progress_lock = Lock()
    # 重新登录进度追踪
    _relogin_progress: dict[str, dict] = {}
    _relogin_progress_lock = Lock()

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = RLock()
        self._token_refresh_lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}
        self._image_preflight_failed_until: dict[str, float] = {}
        self._token_aliases: dict[str, str] = {}
        self._cumulative_total = self._load_cumulative_total()
        self._last_refresh_tokens: list[str] = []

    def _get_cumulative_file(self) -> Path:
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        try:
            payload = str(token or "").split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            import base64
            import json
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
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

    @classmethod
    def _day_key(cls, value: object) -> str | None:
        parsed = cls._parse_time(value)
        if parsed is None:
            return None
        return parsed.astimezone(timezone(timedelta(hours=8))).date().isoformat()

    @staticmethod
    def _timestamp_to_iso(value: object) -> str:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return ""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat()

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        normalized_accounts: dict[str, dict] = {}
        changed = False
        for item in accounts:
            normalized = self._normalize_account(item)
            if normalized is None:
                changed = True
                continue
            access_token = str(normalized.get("access_token") or "").strip()
            if not access_token:
                changed = True
                continue
            if normalized != item:
                changed = True
            if access_token in normalized_accounts:
                changed = True
            normalized_accounts[access_token] = normalized
        if changed:
            try:
                self.storage.save_accounts(list(normalized_accounts.values()))
            except Exception:
                pass
        return normalized_accounts

    def reload_from_storage(self) -> dict[str, int]:
        """重新加载持久化账号快照，并清理已不存在 token 的运行态。

        该入口用于受控的外部恢复进程完成 SQLite 原子替换后，让当前
        API 进程无需重启即可看到新 token。调用方必须确保外部写入已经结束。
        """
        with self._image_slot_condition:
            accounts = self._load_accounts()
            valid_tokens = set(accounts)
            self._accounts = accounts
            self._image_inflight = {
                token: count
                for token, count in self._image_inflight.items()
                if token in valid_tokens and int(count or 0) > 0
            }
            self._image_preflight_failed_until = {
                token: until
                for token, until in self._image_preflight_failed_until.items()
                if token in valid_tokens
            }
            self._token_aliases = {
                alias: target
                for alias, target in self._token_aliases.items()
                if alias in valid_tokens and target in valid_tokens
            }
            self._index = self._index % max(1, len(accounts))
            self._cumulative_total = max(self._cumulative_total, len(accounts))
            self._image_slot_condition.notify_all()
            return {"total": len(accounts)}

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    def _persist_upsert_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """持久化一批账号更新。

        SQLite/Postgres 后端会执行行级 upsert；JSON/Git 后端走兼容全量保存。
        """
        prepared = [dict(account) for account in accounts if isinstance(account, dict)]
        if not prepared:
            return
        upsert = getattr(self.storage, "upsert_accounts", None)
        if callable(upsert):
            upsert(prepared)
            return
        current: dict[str, dict[str, Any]] = {}
        for item in self.storage.load_accounts():
            token = str((item or {}).get("access_token") or (item or {}).get("accessToken") or "").strip()
            if token:
                current[token] = {**item, "access_token": token}
        for account in prepared:
            token = str(account.get("access_token") or account.get("accessToken") or "").strip()
            if token:
                current[token] = {**current.get(token, {}), **account, "access_token": token}
        self.storage.save_accounts(list(current.values()))

    def _persist_delete_accounts(self, tokens: list[str]) -> None:
        """持久化一批账号删除。"""
        prepared = [str(token or "").strip() for token in tokens if str(token or "").strip()]
        if not prepared:
            return
        delete = getattr(self.storage, "delete_accounts", None)
        if callable(delete):
            delete(prepared)
            return
        target = set(prepared)
        kept = [
            item
            for item in self.storage.load_accounts()
            if str((item or {}).get("access_token") or (item or {}).get("accessToken") or "").strip() not in target
        ]
        self.storage.save_accounts(kept)

    @staticmethod
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if AccountService._is_true_unlimited_image_account(account):
            return True
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    @classmethod
    def _is_true_unlimited_image_account(cls, account: dict) -> bool:
        return cls._account_matches_any_plan_type(account, ("Pro", "ProLite"))

    @classmethod
    def _is_unknown_image_quota_account(cls, account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") != "正常":
            return False
        if cls._is_true_unlimited_image_account(account):
            return False
        return bool(account.get("image_quota_unknown"))

    @staticmethod
    def _extract_image_quota_state(limits_progress: object) -> tuple[int, str | None] | None:
        if not isinstance(limits_progress, list):
            return None
        for item in limits_progress:
            if not isinstance(item, dict):
                continue
            feature_name = str(item.get("feature_name") or "").strip().lower().replace("-", "_")
            if feature_name != "image_gen":
                continue
            try:
                remaining = max(0, int(item.get("remaining")))
            except (TypeError, ValueError):
                continue
            restore_at = str(item.get("reset_after") or "").strip() or None
            return remaining, restore_at
        return None

    def _image_quota_refresh_time(self, account: dict) -> datetime | None:
        if not isinstance(account, dict):
            return None
        return self._parse_time(account.get("last_quota_refresh_at"))

    def _is_recent_image_quota(self, account: dict) -> bool:
        refreshed_at = self._image_quota_refresh_time(account)
        if refreshed_at is None:
            return False
        max_age_hours = float(config.image_quota_freshness_hours or 0)
        if max_age_hours <= 0:
            return True
        return (datetime.now(timezone.utc) - refreshed_at) <= timedelta(hours=max_age_hours)

    def _active_proxy_binding_duplicate(self, account: dict) -> bool:
        """同一活跃 proxy_binding_hash 绑定多个账号时禁止进入调度。"""
        from services.account_identity import proxy_binding_hash

        if not isinstance(account, dict):
            return True
        binding = str(account.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(account.get("proxy"))
        if not binding:
            return False
        peers = 0
        for item in self._accounts.values():
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            # 仅「可调度态」同伴计入重复；禁用/限流/异常不挡唯一 canary
            if status in {"禁用", "限流", "异常"}:
                continue
            if str(item.get("outlook_recovery_state") or "").strip().lower() == "terminal":
                continue
            receive = str(item.get("panda_receive_state") or "").strip().lower()
            # canary 窗口：共享绑定的同伴可先标 identity_isolated，不计入活跃重复
            if receive in {"identity_isolated", "rejected"}:
                continue
            other_binding = str(item.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(
                item.get("proxy")
            )
            if other_binding == binding:
                peers += 1
                if peers > 1:
                    return True
        return False

    def _enforce_shared_binding_isolation(self, account: dict, access_token: str) -> dict:
        """同 binding 已有活跃号时，禁止新号静默进调度池：强制 identity_isolated。"""
        from services.account_identity import proxy_binding_hash
        from utils.log import logger

        if not isinstance(account, dict):
            return account
        receive = str(account.get("panda_receive_state") or "").strip().lower()
        if receive in {"identity_isolated", "rejected"}:
            return account
        status = str(account.get("status") or "")
        if status in {"禁用", "限流", "异常"}:
            return account
        binding = str(account.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(account.get("proxy"))
        if not binding:
            return account
        for token, item in self._accounts.items():
            if token == access_token or not isinstance(item, dict):
                continue
            other_status = str(item.get("status") or "")
            if other_status in {"禁用", "限流", "异常"}:
                continue
            if str(item.get("outlook_recovery_state") or "").strip().lower() == "terminal":
                continue
            other_receive = str(item.get("panda_receive_state") or "").strip().lower()
            if other_receive in {"identity_isolated", "rejected"}:
                continue
            other_binding = str(item.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(item.get("proxy"))
            if other_binding != binding:
                continue
            next_account = dict(account)
            next_account["panda_receive_state"] = "identity_isolated"
            logger.warning(
                {
                    "event": "shared_binding_forced_isolation",
                    "proxy_binding_hash": binding,
                    "peer_present": True,
                }
            )
            return next_account
        return account

    def _preserve_identity_isolated(self, current: dict, updates: dict) -> dict:
        """禁止运维路径把 identity_isolated 同伴冲回 verified_ready。"""
        previous = str(current.get("panda_receive_state") or "").strip().lower()
        incoming = str(updates.get("panda_receive_state") or "").strip().lower()
        if previous == "identity_isolated" and incoming in {"verified_ready", "verified", "local_verified", "ready"}:
            next_updates = dict(updates)
            next_updates["panda_receive_state"] = "identity_isolated"
            return next_updates
        return updates

    def _is_image_account_schedulable(self, account: dict) -> bool:
        if not self._is_image_account_available(account):
            return False
        if self._has_image_account_failure_evidence(account):
            return False
        if self._requires_panda_receive_verification(account):
            return False
        if config.image_require_recent_quota_refresh and not self._is_recent_image_quota(account):
            return False
        if self._active_proxy_binding_duplicate(account):
            return False
        return True

    @staticmethod
    def _is_invalid_refresh_error_text(error: object) -> bool:
        lowered = str(error or "").strip().lower()
        if not lowered:
            return False
        markers = (
            "token invalidated",
            "invalid access token",
            "invalid_access_token",
            "unauthorized",
            "http 401",
            "account_deactivated",
        )
        return any(marker in lowered for marker in markers)

    @classmethod
    def _has_image_account_failure_evidence(cls, account: dict) -> bool:
        """账号近期有明确失败证据时，不允许进入图片调度候选。

        失败证据只能由后续成功刷新清掉；否则 transient/invalid 会把账面
        status=正常/quota>0 污染成“看起来可调度”。
        """
        if not isinstance(account, dict):
            return True
        if int(account.get("invalid_count") or 0) > 0:
            return True
        if cls._is_invalid_refresh_error_text(account.get("last_refresh_error")):
            return True
        if cls._is_invalid_refresh_error_text(account.get("last_token_refresh_error")):
            return True
        if int(account.get("quota_refresh_fail_count") or 0) > 0:
            return True
        if str(account.get("quota_refresh_failure_kind") or "").strip():
            return True
        if str(account.get("last_quota_refresh_error") or "").strip():
            return True
        return False

    @staticmethod
    def _requires_panda_receive_verification(account: dict) -> bool:
        receive_state = str(account.get("panda_receive_state") or "").strip().lower()
        if not receive_state:
            return False
        return receive_state not in {"verified_ready", "verified", "local_verified"}

    @classmethod
    def _is_panda_upload_eligible(cls, account: dict) -> bool:
        state = str((account or {}).get("panda_sync_state") or "").strip().lower()
        if state != "ready":
            return False
        if not cls._is_image_account_available(account):
            return False
        if cls._has_image_account_failure_evidence(account):
            return False
        if cls._parse_time(account.get("last_quota_refresh_at")) is None:
            return False
        if str(account.get("panda_probe_last_error") or "").strip():
            return False
        return True

    def _image_candidate_sort_key(self, account: dict) -> tuple[int, int, float, int, float]:
        refreshed_at = self._image_quota_refresh_time(account)
        last_used_at = self._parse_time(account.get("last_used_at"))
        refresh_ts = refreshed_at.timestamp() if refreshed_at is not None else 0.0
        used_ts = last_used_at.timestamp() if last_used_at is not None else 0.0
        quota = int(account.get("quota") or 0)
        # Prefer spreading load across unused / least recently used accounts.
        # Putting newest quota refresh first repeatedly hammers the account that
        # was just verified/relogged, which is exactly the wrong behavior after a
        # recovery batch where many accounts are refreshed within seconds.
        return (0 if refreshed_at is not None else 1, 0 if last_used_at is None else 1, used_ts, -quota, -refresh_ts)

    def _is_image_preflight_backed_off(self, access_token: str) -> bool:
        token = self._resolve_access_token_locked(access_token)
        until = float(self._image_preflight_failed_until.get(token) or 0)
        if until <= 0:
            return False
        if until <= time.time():
            self._image_preflight_failed_until.pop(token, None)
            return False
        return True

    def _can_skip_image_preflight(self, account: dict | None) -> bool:
        """短窗口内复用最近配额刷新结果，降低生图取号探活与写盘频率。"""
        if not isinstance(account, dict) or not self._is_image_account_schedulable(account):
            return False
        interval = float(config.image_preflight_min_interval_sec or 0.0)
        if interval <= 0:
            return False
        token = str(account.get("access_token") or "").strip()
        if token and self._token_needs_refresh(token):
            return False
        refreshed_at = self._image_quota_refresh_time(account)
        if refreshed_at is None:
            return False
        now = datetime.now(timezone.utc)
        if refreshed_at > now:
            return False
        return (now - refreshed_at).total_seconds() <= interval

    def _record_image_preflight_failure(self, access_token: str, error: object) -> None:
        backoff_sec = float(config.image_preflight_failure_backoff_sec or 0)
        if not access_token or backoff_sec <= 0:
            return
        with self._image_slot_condition:
            token = self._resolve_access_token_locked(access_token)
            self._image_preflight_failed_until[token] = time.time() + backoff_sec

    def _clear_image_preflight_failure(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            token = self._resolve_access_token_locked(access_token)
            self._image_preflight_failed_until.pop(token, None)

    def record_image_transient_backoff(self, access_token: str, error: object) -> None:
        """把上游生图瞬断账号短期移出候选面。

        这里只复用内存态 preflight backoff，不删除账号、不写库；目标是在
        ChatGPT 上游连接 timeout / reset 时避免同一个 token 立刻被重复调度。
        """
        self._record_image_preflight_failure(access_token, error)

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        return str(value or "web").strip().lower() or "web"

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key) or raw

    def _search_account_type(self, payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in ("plan_type", "account_plan", "account_type", "subscription_type", "type"):
                plan = self._normalize_account_type(payload.get(key))
                if plan:
                    return plan
            for value in payload.values():
                plan = self._search_account_type(value)
                if plan:
                    return plan
        elif isinstance(payload, list):
            for value in payload:
                plan = self._search_account_type(value)
                if plan:
                    return plan
        return None

    @staticmethod
    def _is_terminal_outlook_recovery(account: dict | None) -> bool:
        if not isinstance(account, dict):
            return False
        state = str(account.get("outlook_recovery_state") or "").strip().lower()
        reason = str(account.get("outlook_recovery_terminal_reason") or "").strip().lower()
        if state == "terminal" or reason == "account_deactivated":
            return True
        error_text = " ".join(
            str(account.get(key) or "")
            for key in (
                "outlook_recovery_last_error",
                "last_refresh_error",
                "panda_verify_last_error",
            )
        ).lower()
        return "account_deactivated" in error_text or "deleted or deactivated" in error_text

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        normalized["image_quota_unknown"] = bool(normalized.get("image_quota_unknown"))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        normalized["proxy"] = str(normalized.get("proxy") or "").strip()
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        derived_quota_state = self._extract_image_quota_state(normalized["limits_progress"])
        if (
            derived_quota_state is not None
            and normalized["status"] == "正常"
            and not self._is_true_unlimited_image_account(normalized)
        ):
            derived_quota, derived_restore_at = derived_quota_state
            normalized["quota"] = derived_quota
            normalized["restore_at"] = derived_restore_at
            normalized["image_quota_unknown"] = False
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        for traffic_key in ("traffic_uploaded_bytes", "traffic_downloaded_bytes", "traffic_total_bytes"):
            traffic_value = normalized.get(traffic_key)
            if traffic_value is None:
                normalized[traffic_key] = None
                continue
            try:
                normalized[traffic_key] = max(0, int(traffic_value))
            except (TypeError, ValueError):
                normalized[traffic_key] = None
        normalized["traffic_updated_at"] = normalized.get("traffic_updated_at") or None
        normalized["invalid_count"] = int(normalized.get("invalid_count") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
        normalized["last_refresh_error"] = normalized.get("last_refresh_error") or None
        normalized["last_refresh_error_at"] = normalized.get("last_refresh_error_at") or None
        normalized["last_token_refresh_at"] = normalized.get("last_token_refresh_at") or None
        normalized["last_token_refresh_error"] = normalized.get("last_token_refresh_error") or None
        normalized["last_token_refresh_error_at"] = normalized.get("last_token_refresh_error_at") or None
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        # 本地注册号进入 Panda 前的多阶段探活状态。老账号没有该字段时保持空，
        # 只有注册/显式 staging 流程会设置为 staging/ready/synced。
        normalized["panda_sync_state"] = str(normalized.get("panda_sync_state") or "").strip() or None
        normalized["panda_probe_count"] = max(0, int(normalized.get("panda_probe_count") or 0))
        normalized["panda_probe_next_at"] = normalized.get("panda_probe_next_at") or None
        normalized["panda_probe_last_at"] = normalized.get("panda_probe_last_at") or None
        normalized["panda_probe_last_error"] = normalized.get("panda_probe_last_error") or None
        history = normalized.get("panda_probe_history")
        normalized["panda_probe_history"] = history if isinstance(history, list) else []
        normalized["panda_ready_at"] = normalized.get("panda_ready_at") or None
        normalized["panda_synced_at"] = normalized.get("panda_synced_at") or None
        normalized["panda_receive_state"] = str(normalized.get("panda_receive_state") or "").strip() or None
        normalized["panda_imported_at"] = normalized.get("panda_imported_at") or None
        normalized["panda_verified_at"] = normalized.get("panda_verified_at") or None
        normalized["panda_rejected_at"] = normalized.get("panda_rejected_at") or None
        normalized["panda_verify_last_error"] = normalized.get("panda_verify_last_error") or None
        # 统一补齐可本地推导的 proxy binding / 持久 fp；普通 update 的写保护在 merge 层处理。
        before_fp = normalized.get("fp")
        normalized = normalize_account_identity(normalized)
        if not str(normalized.get("fp_origin") or "").strip():
            if isinstance(before_fp, dict) and before_fp:
                normalized["fp_origin"] = "imported"
            else:
                normalized["fp_origin"] = "generated_on_normalize"
                normalized.setdefault(
                    "fp_persisted_at",
                    datetime.now(timezone.utc).isoformat(),
                )
        normalized["identity_conflict_count"] = max(0, int(normalized.get("identity_conflict_count") or 0))
        conflict_fields = normalized.get("identity_last_conflict_fields")
        if not isinstance(conflict_fields, list):
            normalized["identity_last_conflict_fields"] = []
        return normalized

    @staticmethod
    def _jwt_exp(access_token: str) -> int:
        try:
            return int(AccountService._decode_jwt_payload(access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _token_expires_in(cls, access_token: str) -> int | None:
        exp = cls._jwt_exp(access_token)
        if exp <= 0:
            return None
        return exp - int(time.time())

    @classmethod
    def _token_needs_refresh(cls, access_token: str, *, force: bool = False) -> bool:
        if force:
            return True
        remaining = cls._token_expires_in(access_token)
        return remaining is not None and remaining <= cls._ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @classmethod
    def _token_issued_at(cls, access_token: str) -> datetime | None:
        try:
            iat = int(cls._decode_jwt_payload(access_token).get("iat") or 0)
        except (TypeError, ValueError):
            return None
        if iat <= 0:
            return None
        return datetime.fromtimestamp(iat, tz=timezone.utc)

    @staticmethod
    def _safe_response_text(response: object, limit: int = 300) -> str:
        try:
            return str(getattr(response, "text", "") or "")[:limit]
        except Exception:
            return ""

    def _resolve_access_token_locked(self, access_token: str) -> str:
        token = str(access_token or "").strip()
        seen: set[str] = set()
        while token and token not in self._accounts and token in self._token_aliases and token not in seen:
            seen.add(token)
            token = self._token_aliases.get(token, token)
        return token

    def resolve_access_token(self, access_token: str) -> str:
        if not access_token:
            return ""
        with self._lock:
            return self._resolve_access_token_locked(access_token)

    def _get_account_for_token(self, access_token: str) -> tuple[str, dict | None]:
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(resolved)
            return resolved, dict(account) if account else None

    def _record_token_refresh_error(self, access_token: str, event: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_token_refresh_error"] = str(error or "refresh token failed")
            next_item["last_token_refresh_error_at"] = now
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[resolved] = account
                self._persist_upsert_accounts([account])
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 刷新 access_token 失败",
            {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
        )

    def _recent_token_refresh_error(self, account: dict) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (datetime.now(timezone.utc) - last_error_at).total_seconds() < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def _recent_refresh_token_keepalive_error(self, account: dict, now: datetime) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (now - last_error_at).total_seconds() < self._REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS

    def _refresh_token_keepalive_anchor(self, account: dict) -> datetime | None:
        return (
            self._parse_time(account.get("last_token_refresh_at"))
            or self._token_issued_at(str(account.get("access_token") or ""))
            or self._parse_time(account.get("created_at"))
        )

    def _refresh_token_keepalive_due_at(self, account: dict, now: datetime) -> datetime | None:
        if not str(account.get("refresh_token") or "").strip():
            return None
        if account.get("status") == "禁用":
            return None
        if self._recent_refresh_token_keepalive_error(account, now):
            return None
        anchor = self._refresh_token_keepalive_anchor(account)
        if anchor is None:
            return now
        due_at = anchor + timedelta(seconds=self._REFRESH_TOKEN_KEEPALIVE_SECONDS)
        return due_at if due_at <= now else None

    def _request_access_token_refresh(self, refresh_token: str, account: dict | None = None) -> dict[str, str]:
        from curl_cffi import requests
        from services.proxy_service import proxy_settings

        session = requests.Session(**proxy_settings.build_session_kwargs(
            account=account,
            impersonate="chrome110",
            verify=True,
            upstream=True,
        ))
        try:
            response = session.post(
                self._OAUTH_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self._OAUTH_USER_AGENT,
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._OAUTH_CLIENT_ID,
                },
                timeout=60,
            )
            data = response.json() if response.text else {}
            if response.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
                detail = ""
                if isinstance(data, dict):
                    detail = str(data.get("error_description") or data.get("error") or data.get("message") or "")
                detail = detail or self._safe_response_text(response)
                raise RuntimeError(f"oauth_refresh_http_{response.status_code}{': ' + detail if detail else ''}")
            return {
                "access_token": str(data.get("access_token") or "").strip(),
                "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
                "id_token": str(data.get("id_token") or "").strip(),
            }
        finally:
            session.close()

    def _apply_refreshed_tokens(self, old_access_token: str, token_data: dict, event: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            old_token = self._resolve_access_token_locked(old_access_token)
            current = self._accounts.get(old_token)
            if current is None:
                return old_token
            new_token = str(token_data.get("access_token") or old_token).strip()
            if not new_token:
                return old_token

            next_item = dict(current)
            next_item["access_token"] = new_token
            if token_data.get("refresh_token"):
                next_item["refresh_token"] = str(token_data.get("refresh_token") or "").strip()
            if token_data.get("id_token"):
                next_item["id_token"] = str(token_data.get("id_token") or "").strip()
            next_item["last_token_refresh_at"] = now
            next_item["last_token_refresh_error"] = None
            next_item["last_token_refresh_error_at"] = None
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None

            account = self._normalize_account(next_item)
            if account is None:
                return old_token

            rotated = new_token != old_token
            if rotated:
                self._accounts.pop(old_token, None)
                self._token_aliases[old_token] = new_token
                old_inflight = int(self._image_inflight.pop(old_token, 0))
                if old_inflight:
                    self._image_inflight[new_token] = int(self._image_inflight.get(new_token, 0)) + old_inflight
                old_failed_until = self._image_preflight_failed_until.pop(old_token, None)
                if old_failed_until is not None:
                    self._image_preflight_failed_until[new_token] = old_failed_until
            self._accounts[new_token] = account
            if rotated:
                self._persist_delete_accounts([old_token])
            self._persist_upsert_accounts([account])
            self._image_slot_condition.notify_all()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 已刷新 access_token",
            {"source": event, "token": anonymize_token(new_token), "rotated": rotated},
        )
        return new_token

    def refresh_access_token(self, access_token: str, *, force: bool = False, event: str = "refresh_access_token") -> str:
        if not access_token:
            return ""
        with self._token_refresh_lock:
            resolved_token, account = self._get_account_for_token(access_token)
            if not account:
                return access_token
            active_token = str(account.get("access_token") or resolved_token or access_token)
            if not self._token_needs_refresh(active_token, force=force):
                return active_token
            refresh_token = str(account.get("refresh_token") or "").strip()
            if not refresh_token:
                return active_token
            if not force and self._recent_token_refresh_error(account):
                return active_token
            try:
                token_data = self._request_access_token_refresh(refresh_token, account)
            except Exception as exc:
                error_str = str(exc or "")
                self._record_token_refresh_error(active_token, event, error_str)
                # 如果是 app_session_terminated 错误，尝试密码重新登录
                if "app_session_terminated" in error_str.lower():
                    # 获取账号信息（email, password）
                    email = str(account.get("email") or "").strip()
                    password = str(account.get("password") or "").strip()
                    if email and password:
                        # 创建新线程执行密码重新登录
                        t = Thread(
                            target=self._password_re_login_thread,
                            args=(active_token, email, password, event),
                            daemon=True,
                        )
                        t.start()
                return active_token
            return self._apply_refreshed_tokens(active_token, token_data, event)

    def _password_re_login_thread(self, access_token: str, email: str, password: str, event: str, progress_id: str | None = None) -> None:
        """密码重新登录线程入口"""
        try:
            account = self.get_account(access_token) or {}
            result = self._login_with_password(email, password, account=account)
            if result.get("ok"):
                # 登录成功，更新账号
                new_access_token = result.get("access_token", "")
                new_refresh_token = result.get("refresh_token", "")
                new_id_token = result.get("id_token", "")
                new_expires_at = result.get("expires_at")

                # 构建 token_data 供 _apply_refreshed_tokens 使用
                token_data = {
                    "access_token": new_access_token,
                    "refresh_token": new_refresh_token,
                    "id_token": new_id_token,
                }

                # 使用 _apply_refreshed_tokens 更新账号（处理 token 别名）
                new_token = self._apply_refreshed_tokens(access_token, token_data, f"{event}:password_relogin")

                # 额外更新 source_type 和 status（静默，避免重复日志）
                self.update_account(new_token, {
                    "source_type": result.get("source_type", "password"),
                    "status": "正常",
                }, quiet=True)

                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "更新账号",
                    {
                        "source": event,
                        "old_token": anonymize_token(access_token),
                        "new_token": anonymize_token(new_access_token),
                        "email": email,
                        "status": "成功",
                    },
                )
                if progress_id:
                    self.update_relogin_progress(progress_id, access_token, "成功")
            else:
                # 登录失败
                error_type = result.get("error", "")
                if error_type == "password_verify_failed_403" and isinstance(result.get("detail"), dict):
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "更新账号",
                        {
                            "source": event,
                            "token": anonymize_token(access_token),
                            "email": email,
                            "status": "失败",
                            "error": error_type,
                            "detail": result.get("detail", {}),
                        },
                    )
                    detail_error = result["detail"].get("error", {})
                    if isinstance(detail_error, dict) and detail_error.get("code") == "account_deactivated":
                        # 账号已删除/停用 → 标记为禁用
                        self.update_account(access_token, {"status": "禁用", "quota": 0}, quiet=True)
                        account = self.get_account(access_token) or {}
                        log_service.add(
                            LOG_TYPE_ACCOUNT,
                            "账号已停用-标记禁用",
                            {
                                "source": event,
                                "token": anonymize_token(access_token),
                                "email": email,
                                "detail": result.get("detail", {}),
                            },
                        )
                        if progress_id:
                            self.update_relogin_progress(progress_id, access_token, "禁用")
                    else:
                        # 永久故障：将账号标记为异常（或自动移除）
                        self.remove_invalid_token(access_token, f"{event}:password_relogin_failed", quiet=True)
                        if progress_id:
                            self.update_relogin_progress(progress_id, access_token, "异常", error_type)
                else:
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "更新账号",
                        {
                            "source": event,
                            "token": anonymize_token(access_token),
                            "email": email,
                            "status": "失败",
                            "error": error_type,
                            "detail": result.get("detail", {}),
                        },
                    )
                    # 永久故障：将账号标记为异常（或自动移除）
                    self.remove_invalid_token(access_token, f"{event}:password_relogin_failed", quiet=True)
                    if progress_id:
                        self.update_relogin_progress(progress_id, access_token, "异常", error_type)
        except Exception as exc:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "更新账号",
                {
                    "source": event,
                    "token": anonymize_token(access_token),
                    "email": email,
                    "status": "异常",
                    "error": str(exc),
                },
            )
            # 将账号标记为异常（或自动移除）
            self.remove_invalid_token(access_token, f"{event}:password_relogin_exception", quiet=True)
            if progress_id:
                self.update_relogin_progress(progress_id, access_token, "异常", str(exc))

    def _login_with_password(
        self,
        email: str,
        password: str,
        otp_resolver: Callable[[], str | None] | None = None,
        *,
        account: dict | None = None,
        proxy: str = "",
    ) -> dict:
        """通过邮箱+密码登录，返回 {access_token, refresh_token, id_token, ...}"""
        from curl_cffi import requests

        # 常量
        auth_base = "https://auth.openai.com"
        platform_oauth_audience = "https://api.openai.com/v1"
        platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
        platform_oauth_client_id = self._OAUTH_CLIENT_ID
        platform_oauth_redirect_uri = "https://platform.openai.com/auth/callback"

        from services.account_fingerprint import build_aligned_chrome_fp
        from services.proxy_service import proxy_settings

        fp = build_aligned_chrome_fp()
        user_agent = fp["user-agent"]
        device_id = fp["oai-device-id"]

        # 创建 session：OpenAI 登录/换 token 链路不能直连，必须走 upstream 代理运行时。
        session = requests.Session(**proxy_settings.build_session_kwargs(
            account=account,
            proxy=proxy,
            impersonate=fp["impersonate"],
            verify=False,
            upstream=True,
        ))

        try:

            from utils.pkce import generate_pkce
            code_verifier, code_challenge = generate_pkce()

            # ② 发起 OAuth authorize 请求 (使用 Platform Client + PKCE)
            session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
            session.cookies.set("oai-did", device_id, domain="auth.openai.com")
            params = {
                "issuer": auth_base,
                "client_id": platform_oauth_client_id,
                "audience": platform_oauth_audience,
                "redirect_uri": platform_oauth_redirect_uri,
                "device_id": device_id,
                "screen_hint": "login_or_signup",
                "max_age": "0",
                "login_hint": email,
                "scope": "openid profile email offline_access",
                "response_type": "code",
                "response_mode": "query",
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "auth0Client": platform_auth0_client,
            }
            authorize_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
            resp = session.get(
                authorize_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "user-agent": user_agent,
                    "sec-ch-ua": fp["sec-ch-ua"],
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                    "referer": "https://platform.openai.com/",
                },
                allow_redirects=True,
                timeout=30,
            )

            if resp.status_code not in (200, 302):
                return {"ok": False, "error": f"authorize_failed_{resp.status_code}", "detail": {"url": resp.url, "text": resp.text[:500]}}

            # 检测最终 URL 是否指向错误页面
            final_url = str(resp.url)
            if "/error" in final_url and "payload=" in final_url:
                from urllib.parse import parse_qs, urlparse
                try:
                    parsed_query = parse_qs(urlparse(final_url).query)
                    error_payload_b64 = parsed_query.get("payload", [""])[0]
                    error_payload_b64 += "=" * ((4 - len(error_payload_b64) % 4) % 4)
                    error_payload = json.loads(base64.b64decode(error_payload_b64))
                    error_code = error_payload.get("errorCode", "")
                    if error_code == "rate_limit_exceeded":
                        return {"ok": False, "error": "rate_limit_exceeded", "detail": error_payload}
                    else:
                        return {"ok": False, "error": f"authorize_error_{error_code}", "detail": error_payload}
                except Exception as e:
                    return {"ok": False, "error": "authorize_redirect_error", "detail": {"url": final_url, "parse_error": str(e)}}

            # ③ 提交密码验证
            login_headers = {
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9",
                "content-type": "application/json",
                "origin": auth_base,
                "priority": "u=1, i",
                "user-agent": user_agent,
                "sec-ch-ua": fp["sec-ch-ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "referer": f"{auth_base}/email-verification",
                "oai-device-id": device_id,
            }

            # 添加 sentinel token
            try:
                from utils.sentinel import build_sentinel_token
                sentinel_val, oai_sc_val = build_sentinel_token(session, device_id, "password_verify")
                login_headers["openai-sentinel-token"] = sentinel_val
                if oai_sc_val:
                    session.cookies.set("oai-sc", oai_sc_val, domain=".openai.com")
            except Exception:
                pass

            login_resp = session.post(
                f"{auth_base}/api/accounts/password/verify",
                headers=login_headers,
                json={"password": password},
                timeout=30,
            )

            login_data = {}
            try:
                login_data = login_resp.json() if login_resp.text else {}
            except Exception:
                pass

            if login_resp.status_code != 200:
                error_code = login_data.get("error", {}).get("code", "")
                error_msg = login_data.get("error", {}).get("message", "")
                if error_code == "unsupported_country_region_territory":
                    return {"ok": False, "error": "unsupported_country_region_territory", "detail": login_data}
                elif error_code == "invalid_state":
                    return {"ok": False, "error": "invalid_state", "detail": login_data}
                elif "Invalid credentials" in error_msg or "wrong password" in error_msg.lower():
                    return {"ok": False, "error": "invalid_password", "detail": login_data}
                return {"ok": False, "error": f"password_verify_failed_{login_resp.status_code}", "detail": login_data}

            # 获取 authorization code
            continue_url = str(login_data.get("continue_url") or "").strip()
            auth_code = ""
            if continue_url:
                from urllib.parse import parse_qs, urlparse
                parsed_params = parse_qs(urlparse(continue_url).query)
                auth_code = str((parsed_params.get("code") or [""])[0]).strip()

            # ─── 处理邮箱 OTP 验证 ──────────────────────────
            if not auth_code:
                page_type = ""
                page_info = login_data.get("page")
                if isinstance(page_info, dict):
                    page_type = str(page_info.get("type") or "")

                if page_type == "email_otp_verification":
                    if otp_resolver is None:
                        # 旧行为保持不变：没有外部验证码读取器时只报告需要 OTP。
                        return {"ok": False, "error": "need_verification_code", "detail": login_data}

                    code = str(otp_resolver() or "").strip()
                    if not code:
                        return {"ok": False, "error": "otp_code_timeout", "detail": login_data}

                    otp_headers = {
                        "accept": "application/json",
                        "accept-language": "zh-CN,zh;q=0.9",
                        "content-type": "application/json",
                        "origin": auth_base,
                        "priority": "u=1, i",
                        "referer": f"{auth_base}/email-verification",
                        "user-agent": user_agent,
                        "sec-ch-ua": fp["sec-ch-ua"],
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Windows"',
                        "sec-fetch-dest": "empty",
                        "sec-fetch-mode": "cors",
                        "sec-fetch-site": "same-origin",
                        "oai-device-id": device_id,
                    }
                    try:
                        from utils.sentinel import build_sentinel_token
                        sentinel_val, oai_sc_val = build_sentinel_token(session, device_id, "authorize_continue")
                        otp_headers["openai-sentinel-token"] = sentinel_val
                        if oai_sc_val:
                            session.cookies.set("oai-sc", oai_sc_val, domain=".openai.com")
                    except Exception:
                        pass

                    otp_resp = session.post(
                        f"{auth_base}/api/accounts/email-otp/validate",
                        headers=otp_headers,
                        json={"code": code},
                        timeout=30,
                    )
                    otp_data = {}
                    try:
                        otp_data = otp_resp.json() if otp_resp.text else {}
                    except Exception:
                        pass
                    if otp_resp.status_code != 200:
                        return {"ok": False, "error": f"otp_validate_failed_{otp_resp.status_code}", "detail": otp_data}

                    continue_url = str(otp_data.get("continue_url") or getattr(otp_resp, "url", "") or "").strip()
                    if continue_url:
                        from urllib.parse import parse_qs, urlparse
                        parsed_params = parse_qs(urlparse(continue_url).query)
                        auth_code = str((parsed_params.get("code") or [""])[0]).strip()
                    if not auth_code:
                        return {"ok": False, "error": "no_auth_code_after_otp", "detail": otp_data}
                else:
                    return {"ok": False, "error": "no_auth_code", "detail": login_data}

            # ④ 用 code 换 token (使用 Platform Client + code_verifier，与注册流程相同)
            platform_base = "https://platform.openai.com"
            token_resp = session.post(
                f"{auth_base}/api/accounts/oauth/token",
                headers={
                    "accept": "*/*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "auth0-client": platform_auth0_client,
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": platform_base,
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": f"{platform_base}/",
                    "sec-ch-ua": fp["sec-ch-ua"],
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-site",
                    "user-agent": user_agent,
                },
                json={
                    "client_id": platform_oauth_client_id,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": platform_oauth_redirect_uri,
                },
                verify=False,
                timeout=60,
            )

            token_data = {}
            try:
                token_data = token_resp.json() if token_resp.text else {}
            except Exception:
                pass

            if token_resp.status_code != 200 or not token_data.get("access_token"):
                return {"ok": False, "error": "token_exchange_failed", "detail": token_data}

            access_token = str(token_data.get("access_token") or "").strip()
            refresh_token = str(token_data.get("refresh_token") or "").strip()
            id_token = str(token_data.get("id_token") or "").strip()

            # ⑤ 用 access_token 获取用户信息
            user_info = {}
            try:
                me_resp = session.get(
                    "https://chatgpt.com/backend-api/me",
                    headers={
                        "accept": "application/json",
                        "authorization": f"Bearer {access_token}",
                        "user-agent": user_agent,
                    },
                    timeout=30,
                )
                if me_resp.status_code == 200:
                    user_info = me_resp.json() if me_resp.text else {}
            except Exception:
                pass

            # 解析 JWT payload
            jwt_payload = self._decode_jwt_payload(access_token)

            email_from_jwt = str(jwt_payload.get("https://api.openai.com/profile", {}).get("email") or "").strip()
            account_id_from_jwt = str(
                jwt_payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or ""
            ).strip()

            account_info = user_info.get("account") if isinstance(user_info.get("account"), dict) else {}
            result = {
                "ok": True,
                "email": email_from_jwt or email,
                "account_id": account_id_from_jwt or account_info.get("account_id", ""),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expires_at": jwt_payload.get("exp"),
                "source_type": "password",
                "fp": dict(fp),
            }

            return result

        finally:
            session.close()

    def list_expiring_access_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for account in self._accounts.values()
                if str(account.get("refresh_token") or "").strip()
                and (token := str(account.get("access_token") or "").strip())
                and self._token_needs_refresh(token)
            ]

    def list_refresh_token_keepalive_tokens(self) -> list[str]:
        now = datetime.now(timezone.utc)
        due_items: list[tuple[datetime, str]] = []
        with self._lock:
            for account in self._accounts.values():
                due_at = self._refresh_token_keepalive_due_at(account, now)
                token = str(account.get("access_token") or "").strip()
                if due_at is not None and token:
                    due_items.append((due_at, token))
        due_items.sort(key=lambda item: item[0])
        return [token for _, token in due_items[: self._REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE]]

    def keepalive_refresh_tokens(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        for access_token in access_tokens:
            before = self.resolve_access_token(access_token)
            after = self.refresh_access_token(before, force=True, event="refresh_token_keepalive")
            account = self.get_account(after)
            if account and str(account.get("last_token_refresh_error") or "").strip():
                errors.append({
                    "token": anonymize_token(before),
                    "error": str(account.get("last_token_refresh_error") or "refresh token failed"),
                })
                continue
            if account:
                refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
            "relogined": 0,
        }

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        recent_candidates: list[tuple[tuple[int, float, int, float], str]] = []
        fallback_candidates: list[tuple[tuple[int, float, int, float], str]] = []
        with self._lock:
            accounts = [dict(item) for item in self._accounts.values()]
            for item in accounts:
                token = str(item.get("access_token") or "")
                if (
                        self._is_image_account_available(item)
                        and not self._has_image_account_failure_evidence(item)
                        and not self._requires_panda_receive_verification(item)
                        and self._account_matches_plan_type(item, plan_type)
                        and self._account_matches_any_plan_type(item, plan_types)
                        and self._account_matches_source_type(item, source_type)
                        and token
                        and token not in excluded
                        and not self._is_image_preflight_backed_off(token)
                ):
                    candidate = (self._image_candidate_sort_key(item), token)
                    fallback_candidates.append(candidate)
                    if self._is_recent_image_quota(item):
                        recent_candidates.append(candidate)
        candidates = (
            recent_candidates
            if config.image_require_recent_quota_refresh and recent_candidates
            else fallback_candidates
        )
        candidates.sort()
        return [token for _sort_key, token in candidates]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        with self._lock:
            return [
                token
                for token in self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
                if int(self._image_inflight.get(token, 0)) < max_concurrency
            ]

    def _total_image_inflight_locked(self) -> int:
        return sum(max(0, int(value or 0)) for value in self._image_inflight.values())

    def get_image_candidate_runtime_stats(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, int | float | bool]:
        """返回生图取号运行态候选面，避免 health 只暴露静态 quota 水位。

        - ready_candidate_count：静态可调度且未处于 preflight backoff 的候选数。
        - available_candidate_count：ready 里未达到单账号并发上限的候选数。
        - dispatchable_candidate_count：再扣除全局并发闸门后，当前真正可派发的候选数。
        """
        max_account_concurrency = max(1, int(config.image_account_concurrency or 1))
        global_limit = max(0, int(config.image_global_concurrency or 0))
        queue_timeout = float(config.image_global_queue_timeout_secs or 0.0)

        with self._image_slot_condition:
            now = time.time()
            current_tokens = set(self._accounts)
            expired_or_removed = [
                token
                for token, until in self._image_preflight_failed_until.items()
                if token not in current_tokens or float(until or 0) <= now
            ]
            for token in expired_or_removed:
                self._image_preflight_failed_until.pop(token, None)

            preflight_backoff_count = sum(
                1
                for token, until in self._image_preflight_failed_until.items()
                if token in current_tokens and float(until or 0) > now
            )
            ready_tokens = self._list_ready_candidate_tokens(
                plan_type=plan_type,
                source_type=source_type,
                plan_types=plan_types,
            )
            available_tokens = [
                token
                for token in ready_tokens
                if int(self._image_inflight.get(token, 0)) < max_account_concurrency
            ]
            image_inflight_count = self._total_image_inflight_locked()
            global_limit_reached = global_limit > 0 and image_inflight_count >= global_limit

        available_candidate_count = len(available_tokens)
        return {
            "preflight_backoff_count": preflight_backoff_count,
            "ready_candidate_count": len(ready_tokens),
            "available_candidate_count": available_candidate_count,
            "dispatchable_candidate_count": 0 if global_limit_reached else available_candidate_count,
            "image_inflight_count": image_inflight_count,
            "image_account_concurrency_limit": max_account_concurrency,
            "image_global_concurrency_limit": global_limit,
            "image_global_queue_timeout_secs": queue_timeout,
            "image_global_limit_reached": global_limit_reached,
        }

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            skip_global_limit: bool = False,
    ) -> str:
        with self._image_slot_condition:
            queue_started_at = time.monotonic()
            while True:
                global_limit = int(config.image_global_concurrency or 0)
                if (
                        not skip_global_limit
                        and global_limit > 0
                        and self._total_image_inflight_locked() >= global_limit
                ):
                    queue_timeout = float(config.image_global_queue_timeout_secs or 0.0)
                    if queue_timeout <= 0 or time.monotonic() - queue_started_at >= queue_timeout:
                        raise RuntimeError(
                            f"image service busy: global concurrency limit {global_limit} reached"
                        )
                    self._image_slot_condition.wait(timeout=min(1.0, queue_timeout))
                    continue
                if not self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types):
                    raise RuntimeError(
                        f"no available {plan_type or source_type or ''} image quota".replace("  ", " ").strip()
                        if plan_type or source_type else "no available image quota"
                    )
                tokens = self._list_available_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            skip_global_limit: bool = False,
    ) -> str:
        """从候选池中获取一个可用的图片生图 token。

        基于本地缓存做初筛，然后通过 fetch_remote_info 做远程验证（token 有效性、配额等）。
        限制最大尝试次数防止 token rotation 导致无限循环。
        """
        candidate_count = len(self._list_ready_candidate_tokens(
            plan_type=plan_type,
            source_type=source_type,
            plan_types=plan_types,
        ))
        max_attempts = min(max(1, int(config.image_token_max_attempts or 20)), max(1, candidate_count))
        attempted_tokens: set[str] = set()
        for _attempt in range(max_attempts):
            access_token = self._acquire_next_candidate_token(
                excluded_tokens=attempted_tokens,
                plan_type=plan_type,
                source_type=source_type,
                plan_types=plan_types,
                skip_global_limit=skip_global_limit,
            )
            attempted_tokens.add(access_token)
            local_account = self.get_account(access_token)
            try:
                from services.account_workload_policy_service import account_workload_policy_service

                gate = account_workload_policy_service.decide_for_account(
                    local_account or {},
                    "image",
                    access_token=access_token,
                )
                if gate.mode == "live" and not gate.admitted:
                    self.release_image_slot(access_token)
                    continue
            except Exception:
                pass
            if (
                    self._can_skip_image_preflight(local_account)
                    and self._account_matches_plan_type(local_account or {}, plan_type)
                    and self._account_matches_any_plan_type(local_account or {}, plan_types)
                    and self._account_matches_source_type(local_account or {}, source_type)
            ):
                return str((local_account or {}).get("access_token") or access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception as exc:
                self._record_image_preflight_failure(access_token, exc)
                self.release_image_slot(access_token)
                continue
            # fetch_remote_info 内部可能因 token rotation 导致 access_token 变化，
            # 把新 token 也加入排除列表，防止重复尝试
            resolved = str((account or {}).get("access_token") or "")
            if resolved and resolved != access_token:
                attempted_tokens.add(resolved)
            self._clear_image_preflight_failure(access_token)
            if resolved:
                self._clear_image_preflight_failure(resolved)
            if (
                    self._is_image_account_schedulable(account or {})
                    and self._account_matches_plan_type(account or {}, plan_type)
                    and self._account_matches_any_plan_type(account or {}, plan_types)
                    and self._account_matches_source_type(account or {}, source_type)
            ):
                return str((account or {}).get("access_token") or access_token)
            self.release_image_slot(access_token)
        raise RuntimeError(
            f"no available {plan_type or source_type or ''} image quota (tried {len(attempted_tokens)} tokens)".replace("  ", " ").strip()
            if plan_type or source_type else f"no available image quota (tried {len(attempted_tokens)} tokens)"
        )

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        excluded = set(excluded_tokens or set())
        from services.account_workload_policy_service import account_workload_policy_service

        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            ordered = list(candidates)
            start = self._index % len(ordered)
            self._index += 1
            rotated = ordered[start:] + ordered[:start]

        for access_token in rotated:
            account = self.get_account(access_token) or {}
            gate = account_workload_policy_service.decide_for_account(
                account,
                "text",
                access_token=access_token,
                force_text_demand=True,
            )
            if gate.mode == "live" and not gate.admitted:
                continue
            return self.refresh_access_token(access_token, event="get_text_access_token") or access_token

        # live 全拒时：仍尝试返回第一个候选，避免硬死；shadow 路径本就不会拒。
        if account_workload_policy_service.mode != "live" and rotated:
            access_token = rotated[0]
            return self.refresh_access_token(access_token, event="get_text_access_token") or access_token
        return ""

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._persist_upsert_accounts([account])

    def record_account_traffic(
            self,
            access_token: str,
            *,
            uploaded_bytes: int = 0,
            downloaded_bytes: int = 0,
    ) -> bool:
        """累计账号应用层传输量。

        该计数用于账号页观察文本/生图请求的应用载荷，不代表代理商计费流量；
        TLS、HTTP/2、重传和代理隧道开销仍应以代理商账单为准。
        """
        if not access_token:
            return False
        uploaded = max(0, int(uploaded_bytes or 0))
        downloaded = max(0, int(downloaded_bytes or 0))
        if uploaded <= 0 and downloaded <= 0:
            return False
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            next_item = dict(current)
            previous_uploaded = max(0, int(next_item.get("traffic_uploaded_bytes") or 0))
            previous_downloaded = max(0, int(next_item.get("traffic_downloaded_bytes") or 0))
            total_uploaded = previous_uploaded + uploaded
            total_downloaded = previous_downloaded + downloaded
            next_item["traffic_uploaded_bytes"] = total_uploaded
            next_item["traffic_downloaded_bytes"] = total_downloaded
            next_item["traffic_total_bytes"] = total_uploaded + total_downloaded
            next_item["traffic_updated_at"] = self._now()
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[access_token] = account
            self._persist_upsert_accounts([account])
            return True

    def remove_invalid_token(self, access_token: str, event: str, quiet: bool = False) -> bool:
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0}, quiet=quiet)
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0}, quiet=quiet)
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def ensure_account_identity_ready(
        self,
        access_token: str,
        *,
        purpose: str = "request",
        require_panda_reachable: bool = False,
    ) -> dict:
        """统一身份门禁：规范化 → 持久化 → 重读校验，再允许创建上游 Session。

        失败时抛出 ``ValueError('account_identity_persist_failed: ...')`` 或
        ``ValueError('account_identity_incomplete: ...')``。
        """
        from services.account_identity import missing_panda_identity_fields

        token = str(access_token or "").strip()
        if not token:
            raise ValueError("account_identity_incomplete: access_token")
        with self._lock:
            token = self._resolve_access_token_locked(token)
            current = self._accounts.get(token)
            if current is None:
                raise ValueError("account_identity_incomplete: account_missing")
            normalized = self._normalize_account(dict(current))
            if normalized is None:
                raise ValueError("account_identity_persist_failed: normalize")
            if normalized != current:
                self._accounts[token] = normalized
                try:
                    self._persist_upsert_accounts([normalized])
                except Exception as exc:
                    raise ValueError(f"account_identity_persist_failed: {exc}") from exc
            reread = self._accounts.get(token) or {}
            if normalize_account_identity(dict(reread)).get("fp") != normalized.get("fp"):
                raise ValueError("account_identity_persist_failed: fp_mismatch_after_reread")
            if require_panda_reachable:
                missing = missing_panda_identity_fields(normalized)
                if missing:
                    raise ValueError("account_identity_incomplete: " + ",".join(missing))
            ready = dict(normalized)
            ready["identity_ready_purpose"] = str(purpose or "request")
            return ready

    def list_accounts(self) -> list[dict]:
        """返回所有账号的副本，并为每个账号附加当前图片在途数 image_inflight。

        image_inflight 为内存态并发计数(账号正在生成、尚未结束的图片数)。号池空闲时
        若某账号该值持续 > 0，说明其并发槽位泄漏、已被静默排除出调度，可借此在 UI 上诊断。
        """
        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))
                result.append(account)
            return result

    def get_total_image_inflight(self) -> int:
        with self._lock:
            return self._total_image_inflight_locked()

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    def list_normal_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "正常"
                   and (token := item.get("access_token") or "")
            ]

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _prepare_account_payload(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountService._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        payload["access_token"] = access_token
        # CPA/Codex 导出文件里的 `type=codex` 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def add_account_items(self, items: list[dict], include_items: bool = True) -> dict:
        payloads = [
            payload
            for item in items
            if (payload := self._prepare_account_payload(item)) is not None
        ]
        return self._add_account_payloads(payloads, include_items=include_items)

    def import_account_items(self, items: list[dict], include_items: bool = True) -> dict:
        """Import account records without refreshing them.

        This endpoint is used for trusted cross-node sync where the source side has
        already refreshed and filtered accounts. The receiving Panda still must
        verify accounts in its own egress environment before they become image
        schedulable, so imported accounts enter a quarantine/incoming state.
        """
        imported_at = datetime.now(timezone.utc).isoformat()
        payloads = [
            {
                **payload,
                "panda_receive_state": "incoming",
                "panda_imported_at": imported_at,
                "panda_verified_at": None,
                "panda_verify_last_error": None,
                "panda_sync_state": "incoming",
            }
            for item in items
            if (payload := self._prepare_account_payload(item)) is not None
        ]
        result = self._add_account_payloads(payloads, include_items=include_items)
        received = int(result.get("added") or 0) + int(result.get("updated") or 0) + int(result.get("skipped") or 0)
        if received:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "Panda 接收账号",
                {
                    "received": received,
                    "added": int(result.get("added") or 0),
                    "updated": int(result.get("updated") or 0),
                    "skipped": int(result.get("skipped") or 0),
                },
            )
        return result

    def add_accounts(self, tokens: list[str], source_type: str = "web", include_items: bool = True) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            result = {"added": 0, "skipped": 0}
            if include_items:
                result["items"] = self.list_accounts()
            return result
        return self._add_account_payloads([
            {"access_token": token, "source_type": self._normalize_source_type(source_type)}
            for token in tokens
        ], include_items=include_items)

    def _add_account_payloads(self, payloads: list[dict], include_items: bool = True) -> dict:
        deduped: dict[str, dict] = {}
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            access_token = self._account_payload_token(payload)
            if not access_token:
                continue
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **payload, "access_token": access_token}

        if not deduped:
            result = {"added": 0, "skipped": 0}
            if include_items:
                result["items"] = self.list_accounts()
            return result

        with self._lock:
            added = 0
            skipped = 0
            updated = 0
            changed_accounts: list[dict[str, Any]] = []
            for access_token, payload in deduped.items():
                current = self._accounts.get(access_token)
                existed = current is not None
                if current is None:
                    added += 1
                    self._cumulative_total += 1
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                incoming = dict(payload)
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                if existed:
                    incoming = self._preserve_identity_isolated(current, incoming)
                    protected, conflicts = merge_account_identity(current, incoming, allow_rebind=False)
                    incoming = {**incoming, **protected}
                    if conflicts:
                        incoming["identity_conflict_count"] = int(current.get("identity_conflict_count") or 0) + 1
                        incoming["identity_last_conflict_fields"] = conflicts
                        incoming["identity_last_conflict_at"] = datetime.now(timezone.utc).isoformat()
                account = self._normalize_account(
                    {
                        **current,
                        **incoming,
                        "access_token": access_token,
                        "type": str(incoming.get("type") or current.get("type") or "free"),
                    }
                )
                if account is not None:
                    account = self._enforce_shared_binding_isolation(account, access_token)
                    if existed and account != current:
                        updated += 1
                    if (not existed) or account != current:
                        changed_accounts.append(account)
                    self._accounts[access_token] = account
            if added:
                self._save_cumulative_total()
            self._persist_upsert_accounts(changed_accounts)
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped, "updated": updated})
            items = [dict(item) for item in self._accounts.values()] if include_items else None
        result = {"added": added, "skipped": skipped, "updated": updated}
        if include_items:
            result["items"] = items or []
        return result

    def delete_accounts(self, tokens: list[str], include_items: bool = True) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            result = {"removed": 0}
            if include_items:
                result["items"] = self.list_accounts()
            return result
        with self._lock:
            target_set = {self._resolve_access_token_locked(token) for token in target_set if token}
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
                self._image_preflight_failed_until.pop(token, None)
            self._token_aliases = {
                old: new
                for old, new in self._token_aliases.items()
                if old not in target_set and new not in target_set
            }
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._persist_delete_accounts(list(target_set))
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()] if include_items else None
        result = {"removed": removed}
        if include_items:
            result["items"] = items or []
        return result

    def update_account(self, access_token: str, updates: dict, quiet: bool = False) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            incoming = dict(updates or {})
            incoming = self._preserve_identity_isolated(current, incoming)
            protected, conflicts = merge_account_identity(current, incoming, allow_rebind=False)
            merged = {**current, **incoming, **protected, "access_token": access_token}
            if conflicts:
                merged["identity_conflict_count"] = int(current.get("identity_conflict_count") or 0) + 1
                merged["identity_last_conflict_fields"] = conflicts
                merged["identity_last_conflict_at"] = datetime.now(timezone.utc).isoformat()
            account = self._normalize_account(merged)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._image_inflight.pop(access_token, None)
                self._image_preflight_failed_until.pop(access_token, None)
                self._persist_delete_accounts([access_token])
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            if account != current:
                self._persist_upsert_accounts([account])
            if not quiet:
                log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                                {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def update_account_identity(
        self,
        access_token: str,
        updates: dict,
        *,
        reason: str = "",
        quiet: bool = True,
    ) -> dict | None:
        """显式身份修复/迁移入口：允许改绑 proxy / fp，并写审计字段。"""

        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            incoming = dict(updates or {})
            incoming = self._preserve_identity_isolated(current, incoming)
            protected, _ = merge_account_identity(current, incoming, allow_rebind=True)
            merged = {
                **current,
                **incoming,
                **protected,
                "access_token": access_token,
                "identity_revision": int(current.get("identity_revision") or 0) + 1,
                "identity_update_reason": str(reason or "").strip() or "identity_update",
                "identity_updated_at": datetime.now(timezone.utc).isoformat(),
            }
            account = self._normalize_account(merged)
            if account is None:
                return None
            self._accounts[access_token] = account
            if account != current:
                self._persist_upsert_accounts([account])
            if not quiet:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "更新账号身份",
                    {
                        "token": anonymize_token(access_token),
                        "reason": account.get("identity_update_reason"),
                        "revision": account.get("identity_revision"),
                    },
                )
            return dict(account)

    def _record_refresh_success(self, access_token: str) -> None:
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            next_item["last_quota_refresh_at"] = datetime.now(timezone.utc).isoformat()
            next_item["last_quota_refresh_error"] = None
            next_item["quota_refresh_fail_count"] = 0
            next_item["quota_refresh_failure_kind"] = None
            next_item["quota_refresh_quarantined_at"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account

    def _should_defer_invalid_token(self, account: dict | None, now: datetime) -> bool:
        if not isinstance(account, dict):
            return False
        created_at = self._parse_time(account.get("created_at"))
        if created_at is not None and (now - created_at).total_seconds() < self._NEW_ACCOUNT_INVALID_GRACE_SECONDS:
            return True
        last_invalid_at = self._parse_time(account.get("last_invalid_at"))
        invalid_count = int(account.get("invalid_count") or 0)
        if invalid_count <= 1:
            return True
        if last_invalid_at is not None and (now - last_invalid_at).total_seconds() < self._INVALID_CONFIRM_SECONDS:
            return True
        return False

    def _record_invalid_token_seen(
        self,
        access_token: str,
        event: str,
        error: str,
        defer_invalid_removal: bool = True,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return True
            should_defer = defer_invalid_removal and self._should_defer_invalid_token(current, now)
            next_item = dict(current)
            next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
            next_item["last_invalid_at"] = now.isoformat()
            next_item["last_refresh_error"] = str(error or "invalid access token")
            next_item["last_refresh_error_at"] = now.isoformat()
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._persist_upsert_accounts([account])
            if should_defer:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "暂缓标记异常账号",
                    {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
                )
                return False
        return True

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            is_true_unlimited = self._is_true_unlimited_image_account(next_item)
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if not is_true_unlimited and not image_quota_unknown:
                    next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                if not is_true_unlimited and not image_quota_unknown and next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._image_inflight.pop(access_token, None)
                self._image_preflight_failed_until.pop(access_token, None)
                self._persist_delete_accounts([access_token])
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            if account != current:
                self._persist_upsert_accounts([account])
            return dict(account)
        return None

    def fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        defer_invalid_removal: bool = True,
    ) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        active_token = self.refresh_access_token(access_token, event=f"{event}:preflight") or access_token
        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            api = OpenAIBackendAPI(active_token)
            try:
                result = api.get_user_info()
            finally:
                close = getattr(api, "close", None)
                if callable(close):
                    close()
        except InvalidAccessTokenError as exc:
            refreshed_token = self.refresh_access_token(active_token, force=True, event=f"{event}:invalid_access_token")
            if refreshed_token and refreshed_token != active_token:
                try:
                    retry_api = OpenAIBackendAPI(refreshed_token)
                    try:
                        result = retry_api.get_user_info()
                    finally:
                        close = getattr(retry_api, "close", None)
                        if callable(close):
                            close()
                except InvalidAccessTokenError as retry_exc:
                    if self._record_invalid_token_seen(
                        refreshed_token,
                        event,
                        str(retry_exc),
                        defer_invalid_removal=defer_invalid_removal,
                    ):
                        self.remove_invalid_token(refreshed_token, event)
                    raise
                active_token = refreshed_token
            else:
                if self._record_invalid_token_seen(
                    active_token,
                    event,
                    str(exc),
                    defer_invalid_removal=defer_invalid_removal,
                ):
                    self.remove_invalid_token(active_token, event)
                raise
        self._record_refresh_success(active_token)
        return self.update_account(active_token, result)

    def pop_last_refresh_tokens(self) -> list[str]:
        """取出最近一次 refresh_accounts 处理过的 token 列表。"""
        with self._lock:
            tokens = list(self._last_refresh_tokens)
            self._last_refresh_tokens = []
            return tokens

    # ---- 刷新进度追踪 ----

    def init_refresh_progress(self, progress_id: str, total: int) -> None:
        """初始化刷新进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
            }

    def update_refresh_progress(self, progress_id: str, token: str) -> None:
        """刷新单个账号后，更新进度计数。"""
        account = self.get_account(token)
        status = str(account.get("status") or "正常").strip() if account else "正常"
        quota = max(0, int(account.get("quota") or 0)) if account else 0

        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["status_counts"][status] = progress["status_counts"].get(status, 0) + 1
            progress["total_quota"] += quota

    def finish_refresh_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记刷新完成。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_refresh_progress(self, progress_id: str) -> dict | None:
        """查询刷新进度。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            return dict(progress) if progress else None

    def clean_refresh_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress.pop(progress_id, None)

    # ---- 重新登录进度追踪 ----

    def init_relogin_progress(self, progress_id: str, total: int) -> None:
        """初始化重新登录进度记录。"""
        with self._relogin_progress_lock:
            self._relogin_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "results": [],
            }

    def update_relogin_progress(self, progress_id: str, token: str, status: str, error: str | None = None) -> None:
        """更新单个重新登录进度。当所有账号处理完毕时自动标记完成。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["results"].append({
                "token": anonymize_token(token),
                "status": status,
                "error": error,
            })
            if progress["processed"] >= progress["total"]:
                progress["done"] = True

    def finish_relogin_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记重新登录完成。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_relogin_progress(self, progress_id: str) -> dict | None:
        """查询重新登录进度。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            return dict(progress) if progress else None

    def clean_relogin_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._relogin_progress_lock:
            self._relogin_progress.pop(progress_id, None)

    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        defer_invalid_removal: bool = True,
        include_items: bool = True,
    ) -> dict[str, Any]:
        requested_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        access_tokens: list[str] = []
        skipped_terminal = 0
        with self._lock:
            for token in requested_tokens:
                resolved = self._resolve_access_token_locked(token)
                account = self._accounts.get(resolved)
                if self._is_terminal_outlook_recovery(account):
                    skipped_terminal += 1
                    continue
                access_tokens.append(token)
            self._last_refresh_tokens = list(access_tokens)
        if progress_id:
            self.init_refresh_progress(progress_id, len(access_tokens))
        if not access_tokens:
            result = {
                "refreshed": 0,
                "errors": [],
                "relogined": 0,
                "skipped_terminal": skipped_terminal,
            }
            if include_items:
                result["items"] = self.list_accounts()
            if progress_id:
                self.finish_refresh_progress(progress_id, result)
            return result

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(self.fetch_remote_info, token, "refresh_accounts", defer_invalid_removal): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                token = futures[future]
                try:
                    account = future.result()
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    error_str = str(exc)
                    # TLS/代理连接错误是网络问题，不计入账号失败
                    from services.protocol.conversation import is_tls_connection_error
                    if not is_tls_connection_error(error_str):
                        errors.append({"token": anonymize_token(token), "error": error_str})
                else:
                    if account is not None:
                        refreshed += 1

                if progress_id:
                    self.update_refresh_progress(progress_id, token)
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        # 自动重新登录异常账号（仅当配置开启时）
        relogined = 0
        if config.auto_relogin_after_refresh:
            for token in access_tokens:
                account = self.get_account(token)
                if not account:
                    continue
                status = str(account.get("status") or "").strip()
                if status != "异常":
                    continue
                email = str(account.get("email") or "").strip()
                password = str(account.get("password") or "").strip()
                if not email or not password:
                    continue
                t = Thread(
                    target=self._password_re_login_thread,
                    args=(token, email, password, "auto_relogin_after_refresh"),
                    daemon=True,
                )
                t.start()
                relogined += 1

        result = {
            "refreshed": refreshed,
            "errors": errors,
            "relogined": relogined,
            "skipped_terminal": skipped_terminal,
        }
        if include_items:
            result["items"] = self.list_accounts()

        if progress_id:
            self.finish_refresh_progress(progress_id, result)

        return result

    def re_login_accounts(self, access_tokens: list[str], progress_id: str | None = None) -> dict[str, Any]:
        """对选中账号执行密码重新登录流程。

        仅对包含 email + password 的账号有效。
        登录成功后自动将状态设为"正常"。
        """
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            result = {
                "relogined": 0,
                "skipped": 0,
                "skipped_terminal": 0,
                "errors": [],
                "items": self.list_accounts(),
            }
            if progress_id:
                self.finish_relogin_progress(progress_id, result)
            return result

        if progress_id:
            self.init_relogin_progress(progress_id, len(access_tokens))

        relogined = 0
        skipped = 0
        skipped_terminal = 0
        errors = []

        for token in access_tokens:
            account = self.get_account(token)
            if not account:
                errors.append({"token": anonymize_token(token), "error": "账号不存在"})
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "账号不存在")
                continue
            if self._is_terminal_outlook_recovery(account):
                skipped += 1
                skipped_terminal += 1
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "账号已删除或停用")
                continue

            email = str(account.get("email") or "").strip()
            password = str(account.get("password") or "").strip()
            if not email or not password:
                skipped += 1
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "无邮箱密码")
                continue

            # 在新线程中执行密码重新登录
            t = Thread(
                target=self._password_re_login_thread,
                args=(token, email, password, "manual_relogin", progress_id),
                daemon=True,
            )
            t.start()
            relogined += 1

        result = {
            "relogined": relogined,
            "skipped": skipped,
            "skipped_terminal": skipped_terminal,
            "errors": errors,
            "items": self.list_accounts(),
        }
        if progress_id:
            # 如果所有账号都已同步处理完毕（没有启动线程），直接标记完成
            if relogined == 0:
                self.finish_relogin_progress(progress_id, result)
            else:
                # 有线程在运行，等线程结束后再完成
                pass
        return result

    def build_export_items(self, access_tokens: list[str] | None = None) -> list[dict[str, str]]:
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            accounts = [
                dict(item)
                for item in self._accounts.values()
                if not target_tokens or str(item.get("access_token") or "") in target_tokens
            ]

        items: list[dict[str, str]] = []
        for account in accounts:
            access_token = str(account.get("access_token") or "").strip()
            refresh_token = str(account.get("refresh_token") or "").strip()
            id_token = str(account.get("id_token") or "").strip()
            if not access_token or not refresh_token or not id_token:
                continue

            access_payload = self._decode_jwt_payload(access_token)
            id_payload = self._decode_jwt_payload(id_token)
            auth_claim = access_payload.get("https://api.openai.com/auth")
            auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
            profile_claim = access_payload.get("https://api.openai.com/profile")
            profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

            email = (
                str(account.get("email") or "").strip()
                or str(profile_claim.get("email") or "").strip()
                or str(id_payload.get("email") or "").strip()
            )
            account_id = (
                str(account.get("account_id") or "").strip()
                or str(auth_claim.get("chatgpt_account_id") or "").strip()
                or str(account.get("user_id") or "").strip()
            )
            item = {
                "type": str(account.get("export_type") or "codex"),
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expired": self._timestamp_to_iso(access_payload.get("exp")),
                "last_refresh": self._timestamp_to_iso(access_payload.get("iat")),
            }
            password = str(account.get("password") or "").strip()
            if password:
                item["password"] = password
            items.append(item)
        return items

    def get_stats(self) -> dict:
        with self._lock:
            items = list(self._accounts.values())
            runtime_candidate_stats = self.get_image_candidate_runtime_stats()
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        total_quota = sum(max(0, int(a.get("quota") or 0)) for a in items if a.get("status") == "正常")
        unlimited = sum(1 for a in items if a.get("status") == "正常" and self._is_true_unlimited_image_account(a))
        unknown_quota = sum(1 for a in items if self._is_unknown_image_quota_account(a))
        panda_staging = sum(1 for a in items if str(a.get("panda_sync_state") or "").lower() == "staging")
        panda_ready = sum(1 for a in items if str(a.get("panda_sync_state") or "").lower() == "ready")
        panda_synced = sum(1 for a in items if str(a.get("panda_sync_state") or "").lower() == "synced")
        panda_upload_eligible = sum(1 for a in items if self._is_panda_upload_eligible(a))
        panda_upload_blocked = sum(
            1
            for a in items
            if str(a.get("panda_sync_state") or "").lower() == "ready"
            and not self._is_panda_upload_eligible(a)
        )
        schedulable = sum(1 for a in items if self._is_image_account_schedulable(a))
        tainted = sum(1 for a in items if self._has_image_account_failure_evidence(a))
        panda_incoming = sum(1 for a in items if str(a.get("panda_receive_state") or "").lower() == "incoming")
        panda_verified = sum(1 for a in items if str(a.get("panda_receive_state") or "").lower() in {"verified", "verified_ready", "local_verified"})
        panda_rejected = sum(1 for a in items if str(a.get("panda_receive_state") or "").lower() == "rejected")
        verified_quota_count = sum(
            1
            for a in items
            if self._is_image_account_schedulable(a) and self._image_quota_refresh_time(a) is not None
        )
        stale_quota_count = sum(
            1
            for a in items
            if self._is_image_account_available(a) and not self._is_recent_image_quota(a)
        )
        verified_total_quota = sum(
            max(0, int(a.get("quota") or 0))
            for a in items
            if self._is_image_account_schedulable(a) and self._is_recent_image_quota(a)
        )
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        by_type = {}
        for a in items:
            t = a.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "unlimited_quota_count": unlimited,
            "unknown_quota_count": unknown_quota,
            "panda_staging_count": panda_staging,
            "panda_ready_count": panda_ready,
            "panda_synced_count": panda_synced,
            "panda_upload_queue_count": panda_ready,
            "panda_upload_eligible_count": panda_upload_eligible,
            "panda_upload_unsynced_eligible_count": panda_upload_eligible,
            "panda_upload_blocked_count": panda_upload_blocked,
            "panda_upload_retained_count": panda_synced,
            "panda_upload_remote_pending_count": panda_incoming,
            "panda_upload_remote_verified_count": panda_verified,
            "panda_upload_remote_rejected_count": panda_rejected,
            "schedulable": schedulable,
            "tainted_count": tainted,
            "panda_incoming_count": panda_incoming,
            "panda_verified_count": panda_verified,
            "panda_rejected_count": panda_rejected,
            "verified_quota_count": verified_quota_count,
            "stale_quota_count": stale_quota_count,
            "verified_total_quota": verified_total_quota,
            **runtime_candidate_stats,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
        }

    def get_activity_daily(self, days: int = 14) -> dict[str, Any]:
        days = max(1, min(int(days or 14), 90))
        today = datetime.now(timezone(timedelta(hours=8))).date()
        start = today - timedelta(days=days - 1)
        buckets: dict[str, dict[str, int]] = {
            (start + timedelta(days=offset)).isoformat(): {
                "registered": 0,
                "uploaded": 0,
                "received": 0,
                "deleted": 0,
            }
            for offset in range(days)
        }

        def add(day: str | None, key: str, count: int = 1) -> None:
            if day in buckets and count > 0:
                buckets[day][key] += count

        live_uploaded: dict[str, int] = {}
        live_received: dict[str, int] = {}
        with self._lock:
            accounts = [dict(item) for item in self._accounts.values()]
        for account in accounts:
            add(self._day_key(account.get("created_at")), "registered")
            if day := self._day_key(account.get("panda_synced_at")):
                live_uploaded[day] = live_uploaded.get(day, 0) + 1
            if day := self._day_key(account.get("panda_imported_at")):
                live_received[day] = live_received.get(day, 0) + 1

        for item in log_service.list(type=LOG_TYPE_ACCOUNT, start_date=start.isoformat(), end_date=today.isoformat(), limit=10000):
            day = str(item.get("time") or "")[:10]
            summary = str(item.get("summary") or "")
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            if summary == "上传到 Panda":
                add(day, "uploaded", int(detail.get("accepted") or detail.get("uploaded") or 0))
            elif summary == "Panda 接收账号":
                # Current live accounts already carry panda_imported_at. Logs cover rows later deleted by verify.
                add(day, "received", int(detail.get("received") or 0))
            elif summary.startswith("删除 ") or summary.startswith("自动移除"):
                add(day, "deleted", int(detail.get("removed") or 1))

        for day, count in live_uploaded.items():
            if day in buckets and buckets[day]["uploaded"] == 0:
                buckets[day]["uploaded"] = count
        for day, count in live_received.items():
            if day in buckets and buckets[day]["received"] == 0:
                buckets[day]["received"] = count

        panda_settings = config.get_panda_sync_settings()
        sync_label = "上传" if str(panda_settings.get("base_url") or "").strip() else "接收"
        series = [
            {"date": day, **values}
            for day, values in sorted(buckets.items())
        ]
        return {
            "days": days,
            "sync_label": sync_label,
            "items": series,
        }

    def account_health(self) -> dict:
        stats = self.get_stats()
        return {
            "healthy": (
                stats["active"] > 0
                or stats["unlimited_quota_count"] > 0
                or stats.get("unknown_quota_count", 0) > 0
            ),
            "status": "ok" if stats["active"] > 0 else "degraded",
            **stats,
        }


account_service = AccountService(config.get_storage_backend())
