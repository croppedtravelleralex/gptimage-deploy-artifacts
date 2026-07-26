from __future__ import annotations

import base64
import hashlib
import json
import math
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
    LOG_TYPE_CALL,
    LOG_TYPE_LLM_OPS,
    log_service,
)
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


def inflight_token_fingerprint(token: object) -> str:
    """漂移上报用的稳定、不可逆 token 标识。

    与 `services/image_pipeline/pipeline_watchdog._token_fingerprint`
    **算法完全一致**（blake2b digest_size=6 hexdigest，与 slot_ledger.`_token_hash`
    同属 blake2b 家族；那边取 digest_size=8 的整数是为了 Rust FFI，做 JSON key
    需要的是十六进制串）。逐字符相等是刻意的：watchdog 的 `_over_counted` key 与
    `reconcile_inflight` 的 drift key 因此可以直接交叉引用。

    原实现用 `token[:12] + "..."`：生产 access token 是 JWT，全池共享同一段
    base64 header 前缀（`eyJhbGciOiJS...`），于是**所有账号塌成同一个 key**，
    drift / drift_count 系统性少报 —— 正好废掉它唯一要暴露的那个泄漏。

    刻意做成模块级函数而非 AccountService 方法：`reconcile_inflight` 会被测试
    替身以 `reconcile_inflight = AccountService.reconcile_inflight` 的方式借用，
    走 `self.` 查找会要求每个替身都补绑这个辅助函数。
    """
    return hashlib.blake2b(str(token or "").encode("utf-8"), digest_size=6).hexdigest()


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
        self._cohort_pause_until: dict[str, float] = {}
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

    OBSERVE_IMPORT_REFRESH_GRACE_SEC = 420

    @classmethod
    def observe_import_refresh_grace_seconds(cls) -> int:
        return cls.OBSERVE_IMPORT_REFRESH_GRACE_SEC

    @classmethod
    def _observe_refresh_after(cls, account: dict, *, now: datetime | None = None) -> datetime | None:
        if not isinstance(account, dict):
            return None
        explicit = cls._parse_time(account.get("panda_observe_refresh_after"))
        if explicit is not None:
            return explicit
        receive_state = str(account.get("panda_receive_state") or "").strip().lower()
        if receive_state != "identity_isolated":
            return None
        imported = cls._parse_time(account.get("panda_imported_at"))
        if imported is None:
            return None
        return imported + timedelta(seconds=cls.observe_import_refresh_grace_seconds())

    @classmethod
    def _observe_import_refresh_grace_active(cls, account: dict, *, now: datetime | None = None) -> bool:
        if not isinstance(account, dict):
            return False
        after = cls._observe_refresh_after(account, now=now)
        if after is None:
            return False
        current = now or datetime.now(timezone.utc)
        return current < after

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
        if bool(account.get("image_soft_capped")) and not AccountService._scheduler_unrestricted():
            restore_at = AccountService._parse_time(account.get("restore_at") or account.get("image_gen_window_reset_at"))
            if restore_at is None:
                return False
            now = datetime.now(timezone.utc)
            if restore_at.tzinfo is None:
                restore_at = restore_at.replace(tzinfo=timezone.utc)
            if now < restore_at:
                return False
            # 窗口已过：软熔断失效，允许调度（不依赖 status 自愈）
        if AccountService._is_true_unlimited_image_account(account):
            return True
        if bool(account.get("image_quota_unknown")):
            return False
        if int(account.get("quota") or 0) > 0:
            return True
        # restore_at 已过但本地仍是 0：允许进候选，取号时懒刷新上游
        return AccountService._quota_window_due_for_lazy_refresh(account)

    @classmethod
    def _has_confirmed_image_quota(cls, account: dict) -> bool:
        """账面已确认有生图额度：非 unknown，且 quota>0 / 无限额 / 懒刷新窗口已到。"""
        if not isinstance(account, dict):
            return False
        if account.get("status") != "正常":
            return False
        if cls._is_true_unlimited_image_account(account):
            return True
        if bool(account.get("image_quota_unknown")):
            return False
        if int(account.get("quota") or 0) > 0:
            return True
        return cls._quota_window_due_for_lazy_refresh(account)

    def _image_quota_freshness_required(self) -> bool:
        if config.image_require_recent_quota_refresh:
            return True
        try:
            pipe = config.get_image_pipeline_settings()
            if not pipe.get("enabled"):
                return False
            return bool(pipe.get("require_quota_freshness", True))
        except Exception:
            return False

    def image_quota_state(self, account: dict) -> str:
        """UI/运维用额度状态：unlimited/unknown/ready/unverified/stale/blocked/refresh_pending/exhausted."""
        if not isinstance(account, dict):
            return "exhausted"
        if self._is_true_unlimited_image_account(account):
            return "unlimited"
        if bool(account.get("image_quota_unknown")):
            return "unknown"
        quota = int(account.get("quota") or 0)
        if self._is_image_account_schedulable(account) and quota > 0:
            refreshed_at = self._image_quota_refresh_time(account)
            if refreshed_at is None:
                return "unverified"
            if self._image_quota_freshness_required() and not self._is_recent_image_quota(account):
                return "stale"
            return "ready"
        if quota > 0:
            return "blocked"
        if self._quota_window_due_for_lazy_refresh(account):
            return "refresh_pending"
        return "exhausted"

    def available_image_quota_for_account(self, account: dict) -> int:
        """单账号当前可参与生图调度的账面额度（0=不可用，-1=无限额）。"""
        if not isinstance(account, dict):
            return 0
        if self._is_true_unlimited_image_account(account):
            return -1
        if not self._is_image_account_schedulable(account):
            return 0
        if not self._has_confirmed_image_quota(account):
            return 0
        quota = int(account.get("quota") or 0)
        if quota <= 0:
            return 0
        refreshed_at = self._image_quota_refresh_time(account)
        if refreshed_at is not None and self._image_quota_freshness_required() and not self._is_recent_image_quota(account):
            return 0
        return quota

    @staticmethod
    def _lazy_refresh_jitter_seconds(account: dict) -> float:
        """Stable per-account stagger after restore_at so free-tier windows don't wake together."""
        try:
            hours = float((config.get_scheduler_settings() or {}).get("lazy_refresh_jitter_hours") or 0)
        except Exception:
            hours = 6.0
        hours = max(0.0, min(24.0, hours))
        if hours <= 0:
            return 0.0
        key = str(account.get("email") or account.get("user_id") or account.get("access_token") or "").strip().lower()
        if not key:
            return 0.0
        digest = hashlib.sha256(f"lazy-jitter-v1:{key}".encode("utf-8")).hexdigest()
        u = int(digest[:8], 16) / float(0xFFFFFFFF)
        return u * hours * 3600.0

    @classmethod
    def _lazy_refresh_eligible_at(cls, account: dict) -> datetime | None:
        restore_at = cls._parse_time(account.get("restore_at") or account.get("image_gen_window_reset_at"))
        if restore_at is None:
            return None
        if restore_at.tzinfo is None:
            restore_at = restore_at.replace(tzinfo=timezone.utc)
        return restore_at + timedelta(seconds=cls._lazy_refresh_jitter_seconds(account))

    @staticmethod
    def _quota_window_due_for_lazy_refresh(account: dict) -> bool:
        """账面额度耗尽且已过 restore_at（再加账号级错峰）：应在真实取号时再拉一次 limits。"""
        if not isinstance(account, dict):
            return False
        if account.get("status") != "正常":
            return False
        if AccountService._is_true_unlimited_image_account(account):
            return False
        if bool(account.get("image_quota_unknown")):
            return False
        if int(account.get("quota") or 0) > 0:
            return False
        eligible_at = AccountService._lazy_refresh_eligible_at(account)
        if eligible_at is None:
            return False
        return datetime.now(timezone.utc) >= eligible_at

    @staticmethod
    def _heal_hard_quota_limited_status(account: dict) -> dict:
        """把「硬额度归零」写死的 status=限流 翻译回 flag 语义（A2-4 单向门）。

        限流 是 quota==0 的**派生态**，不是终态；但它经 _persist_upsert_accounts
        落库，而 _quota_window_due_for_lazy_refresh() 第一条就要求 status==正常，
        于是专门为救这类账号写的懒刷新逃生口被自己关死：账号耗尽一次即永久退池，
        重启也不恢复。此处与 _apply_humanlike_quota_fields 的软熔断同规
        （「软熔断只用 flag，禁止改 status=限流」）：改用 image_soft_capped +
        restore_at 表达「当前不可派发」，restore_at 过期后由懒刷新自然复活。

        只处理**计量**账号（有明确数值额度）：
        - 真无限额（Pro/ProLite）与 image_quota_unknown 不参与硬额度扣减，
          不属于本条的漂移链路，状态保持原样以免扩大改动面。
        - 运维显式开启 auto_remove_rate_limited_accounts 时保持原语义，
          让调用方的删除分支仍能识别（该模式下账号被删除而非搁死）。
        """
        if not isinstance(account, dict):
            return account
        if str(account.get("status") or "") != "限流":
            return account
        try:
            if config.auto_remove_rate_limited_accounts:
                return account
        except Exception:
            pass
        if AccountService._is_true_unlimited_image_account(account):
            return account
        if bool(account.get("image_quota_unknown")):
            return account
        account["status"] = "正常"
        try:
            quota = int(account.get("quota") or 0)
        except (TypeError, ValueError):
            quota = 0
        if quota <= 0 and AccountService._has_quota_window_anchor(account):
            # 仍然耗尽：用 flag 维持「不可派发」，由 restore_at 决定何时放行，
            # 绝不在额度窗口真正重置前把账号放回派发面（否则换来上游 429 风暴）。
            account["image_soft_capped"] = True
        return account

    @staticmethod
    def _has_quota_window_anchor(account: dict) -> bool:
        """是否存在可解析的额度窗口时间锚点（restore_at / 窗口重置时间）。

        没有锚点时**禁止**打 image_soft_capped：`_is_image_account_available()`
        对「soft_capped 且 restore_at 不可解析」直接 return False，而该 flag 只能
        由 remaining>0 的 limits_progress 清掉 —— 账号没有 limits_progress 时
        就换来另一个永久沉底（用饥饿换饥饿）。此时 quota<=0 本身已经足够挡住派发
        （懒刷新同样需要 restore_at），把 status 治好就能让它重新被刷新链路覆盖到。
        """
        return AccountService._parse_time(
            (account or {}).get("restore_at") or (account or {}).get("image_gen_window_reset_at")
        ) is not None

    @staticmethod
    def _scheduler_unrestricted() -> bool:
        try:
            return bool(config.get_scheduler_settings().get("unrestricted"))
        except Exception:
            return False

    def _is_image_interval_ready(self, account: dict) -> bool:
        if self._scheduler_unrestricted():
            return True
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return True
        # fail-streak / 429 cooldown
        try:
            cool_until = float(account.get("image_fail_cooldown_until") or 0)
        except (TypeError, ValueError):
            cool_until = 0.0
        if cool_until > time.time():
            return False
        next_ok = account.get("image_next_ok_ts")
        try:
            next_ok_ts = float(next_ok)
        except (TypeError, ValueError):
            return True
        if next_ok_ts <= 0:
            return True
        return time.time() >= next_ok_ts

    def _is_text_interval_ready(self, account: dict) -> bool:
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return True
        next_ok = account.get("text_next_ok_ts")
        try:
            next_ok_ts = float(next_ok)
        except (TypeError, ValueError):
            return True
        if next_ok_ts <= 0:
            return True
        return time.time() >= next_ok_ts

    def _cohort_paused(self, account: dict) -> bool:
        if self._scheduler_unrestricted():
            return False
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return False
        cohort = str(account.get("cohort_id") or "").strip()
        if not cohort:
            return False
        until = float(self._cohort_pause_until.get(cohort) or 0)
        return until > time.time()

    def _note_cohort_terminal(self, account: dict) -> None:
        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return
        cohort = str(account.get("cohort_id") or "").strip()
        if not cohort:
            return
        threshold = int(settings.get("cohort_terminal_threshold") or 2)
        pause_hours = float(settings.get("cohort_pause_hours") or 24)
        if not hasattr(self, "_cohort_terminal_hits"):
            self._cohort_terminal_hits = {}
        self._cohort_terminal_hits[cohort] = int(self._cohort_terminal_hits.get(cohort) or 0) + 1
        if self._cohort_terminal_hits[cohort] >= threshold:
            self._cohort_pause_until[cohort] = time.time() + pause_hours * 3600.0

    def _apply_humanlike_quota_fields(self, account: dict) -> dict:
        """根据 limits_progress 更新 peak/soft band；scheduler 关闭时仅同步 peak 字段。"""
        from services.humanlike_scheduler import effective_daily_soft, update_quota_peak_state

        settings = config.get_scheduler_settings()
        limits = account.get("limits_progress")
        derived = self._extract_image_quota_state(limits)
        if derived is None:
            return account
        remaining, restore_at = derived
        soft = effective_daily_soft(
            float(settings.get("daily_usage_ratio") or 0.7),
            account,
            new_account_cap=float(settings.get("new_account_usage_cap") or 0.4),
        )
        override_raw = account.get("image_soft_band_override")
        soft_band_override: float | None = None
        try:
            if override_raw is not None and str(override_raw).strip() != "":
                soft_band_override = float(override_raw)
        except (TypeError, ValueError):
            soft_band_override = None
        state = update_quota_peak_state(
            remaining=remaining,
            reset_after=restore_at,
            prev_peak=int(account.get("image_gen_window_peak") or 0),
            prev_reset_at=str(account.get("image_gen_window_reset_at") or "") or None,
            prev_soft_band=float(account["image_soft_band"]) if account.get("image_soft_band") is not None else None,
            soft=soft,
            soft_band_override=soft_band_override,
        )
        account["image_gen_window_peak"] = state.peak
        account["image_gen_window_reset_at"] = state.reset_at
        account["image_soft_band"] = state.soft_band
        account["image_soft_used_ratio"] = state.used_ratio
        if settings.get("enabled") and state.soft_capped and not self._scheduler_unrestricted():
            # 软熔断只用 flag，禁止改 status=限流（否则 restore 后仍被 status 门禁卡死）
            account["image_soft_capped"] = True
            if state.reset_at:
                account["restore_at"] = state.reset_at
        elif remaining > 0 and account.get("image_soft_capped") and not state.soft_capped:
            account["image_soft_capped"] = False
            # 愈合历史误写：软熔断曾把 status 打成限流
            if account.get("status") == "限流":
                account["status"] = "正常"
        return account

    def _stamp_image_next_ok(self, account: dict) -> dict:
        from services.humanlike_scheduler import compute_submit_gap_seconds

        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return account
        gap = compute_submit_gap_seconds(
            base_sec=float(settings.get("image_min_interval_sec") or 60),
            jitter_lo=float(settings.get("jitter_lo") or 0.65),
            jitter_hi=float(settings.get("jitter_hi") or 1.45),
            poisson_lambda_sec=float(settings.get("extra_poisson_lambda_sec") or 8),
        )
        account["image_next_ok_ts"] = time.time() + gap
        account["image_last_gap_sec"] = gap
        return account

    def _stamp_text_next_ok(self, account: dict) -> dict:
        from services.humanlike_scheduler import compute_submit_gap_seconds

        settings = config.get_scheduler_settings()
        if not settings.get("enabled"):
            return account
        gap = compute_submit_gap_seconds(
            base_sec=float(settings.get("text_min_interval_sec") or 30),
            jitter_lo=float(settings.get("jitter_lo") or 0.65),
            jitter_hi=float(settings.get("jitter_hi") or 1.45),
            poisson_lambda_sec=float(settings.get("text_poisson_lambda_sec") or 5),
        )
        account["text_next_ok_ts"] = time.time() + gap
        account["text_last_gap_sec"] = gap
        return account

    def apply_429_cooldown(self, access_token: str, error: object = None) -> None:
        """上游 429 时按 scheduler.cooldown_429_sec 冷却账号（不改 status，避免永久卡死）。"""
        settings = config.get_scheduler_settings()
        if not settings.get("enabled") or not access_token:
            return
        cool = float(settings.get("cooldown_429_sec") or 900)
        with self._lock:
            token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(token)
            if current is None:
                return
            next_item = dict(current)
            next_item["image_fail_cooldown_until"] = time.time() + cool
            next_item["last_refresh_error"] = str(error or "http_429")[:500]
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[token] = account
            self._persist_upsert_accounts([account])

    def _effective_image_global_concurrency(self, ready_count: int | None = None) -> int:
        configured = max(0, int(config.image_global_concurrency or 0))
        settings = config.get_scheduler_settings()
        if not settings.get("enabled") or not settings.get("auto_scale_global_concurrency"):
            return configured
        if ready_count is None:
            ready_count = len(self._list_ready_candidate_tokens())
        floor = configured if configured > 0 else 10
        return max(floor, int(ready_count or 0))

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
                remaining = int(item.get("remaining"))
            except (TypeError, ValueError):
                continue
            if remaining < 0:
                return None
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

    def _proxy_binding_max_accounts(self) -> int:
        try:
            return max(1, int(getattr(config, "proxy_binding_max_accounts", 5) or 5))
        except (TypeError, ValueError):
            return 5

    def _image_binding_inflight_max(self) -> int:
        """同一 proxy_binding 同时生图路上限，降低共享出口 CF 暴死。"""
        try:
            return max(1, int(getattr(config, "image_binding_inflight_max", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def _is_warmup_dispatch_blocked(self, account: dict | None) -> bool:
        try:
            from services.account_warmup_service import account_warmup_service

            email = str((account or {}).get("email") or "").strip().lower()
            return bool(email) and account_warmup_service.is_dispatch_blocked(email)
        except Exception:
            return False

    def _order_tokens_warmup_hot_first(self, tokens: list[str]) -> list[str]:
        try:
            from services.account_warmup_service import account_warmup_service

            hot = {str(e).strip().lower() for e in account_warmup_service.hot_emails()}
        except Exception:
            return tokens
        if not hot:
            return tokens
        hot_tokens: list[str] = []
        cold_tokens: list[str] = []
        for token in tokens:
            account = self.get_account(token) or {}
            email = str(account.get("email") or "").strip().lower()
            (hot_tokens if email in hot else cold_tokens).append(token)
        return hot_tokens + cold_tokens if hot_tokens else tokens

    def _account_binding_hash(self, account: dict | None) -> str:
        from services.account_identity import proxy_binding_hash

        item = account or {}
        return str(item.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(item.get("proxy"))

    def _binding_image_inflight_locked(self, binding: str) -> int:
        if not binding:
            return 0
        total = 0
        for token, count in self._image_inflight.items():
            if int(count or 0) <= 0:
                continue
            account = self._accounts.get(token) or {}
            if self._account_binding_hash(account) == binding:
                total += int(count or 0)
        return total

    def _image_slot_available_locked(
            self,
            token: str,
            *,
            skip_global_limit: bool = False,
    ) -> bool:
        """单 token 是否还能再占一路生图在途（账号并发 + binding + 全局闸门）。"""
        if not token:
            return False
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        binding_limit = self._image_binding_inflight_max()
        current = int(self._image_inflight.get(token, 0))
        if current >= max_concurrency:
            return False
        account = self._accounts.get(token) or {}
        binding = self._account_binding_hash(account)
        if binding and self._binding_image_inflight_locked(binding) >= binding_limit:
            return False
        if not skip_global_limit:
            ready_count = len(self._list_ready_candidate_tokens())
            global_limit = self._effective_image_global_concurrency(ready_count)
            if global_limit > 0 and self._total_image_inflight_locked() >= global_limit:
                return False
        return True

    def _count_active_binding_peers(
        self,
        binding: str,
        *,
        exclude_token: str = "",
    ) -> int:
        """统计同一 binding 上计入容量的活跃账号数（不含禁用/隔离等）。"""
        from services.account_identity import proxy_binding_hash

        key = str(binding or "").strip()
        if not key:
            return 0
        exclude = str(exclude_token or "").strip()
        peers = 0
        for token, item in self._accounts.items():
            if exclude and token == exclude:
                continue
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status in {"禁用", "限流", "异常"}:
                continue
            if str(item.get("outlook_recovery_state") or "").strip().lower() == "terminal":
                continue
            receive = str(item.get("panda_receive_state") or "").strip().lower()
            if receive in {"identity_isolated", "rejected"}:
                continue
            other_binding = str(item.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(
                item.get("proxy")
            )
            if other_binding == key:
                peers += 1
        return peers

    def _active_proxy_binding_duplicate(self, account: dict) -> bool:
        """同一活跃 proxy_binding 超过承载上限时禁止进入生图调度。"""
        from services.account_identity import proxy_binding_hash

        if not isinstance(account, dict):
            return True
        binding = str(account.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(account.get("proxy"))
        if not binding:
            return False
        peers = self._count_active_binding_peers(binding)
        return peers > self._proxy_binding_max_accounts()

    def _account_egress_key(self, account: dict | None) -> str:
        item = account or {}
        return str(item.get("proxy_egress_ip") or item.get("proxy_egress_hash") or "").strip()

    def _count_active_egress_peers(
        self,
        egress_key: str,
        *,
        exclude_token: str = "",
    ) -> int:
        key = str(egress_key or "").strip()
        if not key:
            return 0
        exclude = str(exclude_token or "").strip()
        peers = 0
        for token, item in self._accounts.items():
            if exclude and token == exclude:
                continue
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status in {"禁用", "限流", "异常"}:
                continue
            if str(item.get("outlook_recovery_state") or "").strip().lower() == "terminal":
                continue
            receive = str(item.get("panda_receive_state") or "").strip().lower()
            if receive in {"identity_isolated", "rejected"}:
                continue
            other = self._account_egress_key(item)
            if other == key:
                peers += 1
        return peers

    def _active_proxy_egress_duplicate(self, account: dict) -> bool:
        if not isinstance(account, dict):
            return True
        egress = self._account_egress_key(account)
        if not egress:
            return False
        return self._count_active_egress_peers(egress) > self._proxy_binding_max_accounts()

    def _enforce_shared_binding_isolation(self, account: dict, access_token: str) -> dict:
        """同 binding 已达承载上限时，禁止新号静默进调度池：强制 identity_isolated。"""
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
        max_accounts = self._proxy_binding_max_accounts()
        peers = self._count_active_binding_peers(binding, exclude_token=access_token)
        if peers < max_accounts:
            return account
        next_account = dict(account)
        next_account["panda_receive_state"] = "identity_isolated"
        logger.warning(
            {
                "event": "shared_binding_forced_isolation",
                "proxy_binding_hash": binding,
                "peer_count": peers,
                "max_accounts": max_accounts,
            }
        )
        return next_account

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
        if not self._has_confirmed_image_quota(account):
            return False
        if self._has_image_account_failure_evidence(account):
            return False
        if self._requires_panda_receive_verification(account):
            return False
        if self._image_quota_freshness_required():
            refreshed_at = self._image_quota_refresh_time(account)
            if refreshed_at is not None and not self._is_recent_image_quota(account):
                if not self._quota_window_due_for_lazy_refresh(account):
                    return False
        elif config.image_require_recent_quota_refresh and not self._is_recent_image_quota(account):
            return False
        if self._active_proxy_binding_duplicate(account):
            return False
        if self._active_proxy_egress_duplicate(account):
            return False
        proxy = str(account.get("proxy") or "").strip()
        if proxy:
            try:
                from services.proxy_cf_eligibility import is_proxy_cf_ok_for_image, require_cf_ok_for_image

                if require_cf_ok_for_image() and not is_proxy_cf_ok_for_image(proxy, account=account):
                    return False
            except Exception:
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

    def _image_candidate_sort_key(self, account: dict) -> tuple[float, int, int, float, int, float]:
        from services.humanlike_scheduler import night_or_lunch_soft_weight, resolve_account_tz_name

        refreshed_at = self._image_quota_refresh_time(account)
        last_used_at = self._parse_time(account.get("last_used_at"))
        refresh_ts = refreshed_at.timestamp() if refreshed_at is not None else 0.0
        used_ts = last_used_at.timestamp() if last_used_at is not None else 0.0
        quota = int(account.get("quota") or 0)
        settings = config.get_scheduler_settings()
        weight = 1.0
        if settings.get("enabled"):
            pro = config.get_proactive_refresh_settings()
            tz = resolve_account_tz_name(
                account,
                timezone_from_egress=bool(pro.get("timezone_from_egress")),
                default_tz=str(pro.get("timezone") or "Asia/Singapore"),
            )
            weight = night_or_lunch_soft_weight(
                datetime.now(timezone.utc),
                tz,
                night_weight=float(settings.get("night_soft_weight") or 0.4),
                lunch_weight=float(settings.get("lunch_soft_weight") or 0.85),
            )
        # Lower weight → sort later: encode as (1/weight) ascending preference for higher weight first.
        weight_rank = 1.0 / max(0.05, float(weight))
        return (
            weight_rank,
            0 if refreshed_at is not None else 1,
            0 if last_used_at is None else 1,
            used_ts,
            -quota,
            -refresh_ts,
        )

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
        if not self._has_confirmed_image_quota(account):
            return False
        if self._image_quota_freshness_required() and not self._is_recent_image_quota(account):
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
        try:
            raw_quota = normalized.get("quota")
            quota_int = int(raw_quota if raw_quota is not None else 0)
        except (TypeError, ValueError):
            quota_int = 0
        if quota_int < 0:
            normalized["quota"] = 0
            normalized["image_quota_unknown"] = True
        else:
            normalized["quota"] = quota_int
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
        # A2-4：先把历史/远端写来的硬额度 限流 翻译成 flag，再让下面的 limits_progress
        # 覆盖分支生效。否则 status=限流 且 remaining==0 时既不写回 quota/restore_at，
        # 也不清 status，restore_at 会永远停在过期值上（活体已抓到该状态）。
        normalized = self._heal_hard_quota_limited_status(normalized)
        derived_quota_state = self._extract_image_quota_state(normalized["limits_progress"])
        if (
            derived_quota_state is not None
            and not self._is_true_unlimited_image_account(normalized)
            and (
                normalized["status"] == "正常"
                or bool(normalized.get("image_soft_capped"))
                or (
                    normalized["status"] == "限流"
                    and int(derived_quota_state[0] or 0) > 0
                )
            )
        ):
            derived_quota, derived_restore_at = derived_quota_state
            normalized["quota"] = derived_quota
            normalized["restore_at"] = derived_restore_at
            normalized["image_quota_unknown"] = False
            normalized = self._apply_humanlike_quota_fields(normalized)
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
        normalized["maturity_stage"] = str(normalized.get("maturity_stage") or "").strip() or None
        normalized["maturity_checked_at"] = normalized.get("maturity_checked_at") or None
        normalized["cohort_id"] = str(normalized.get("cohort_id") or "").strip() or None
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
        normalized["panda_observe_refresh_after"] = normalized.get("panda_observe_refresh_after") or None
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
        egress_daily = normalized.get("egress_daily")
        normalized["egress_daily"] = egress_daily if isinstance(egress_daily, list) else []
        cf_daily = normalized.get("cf_daily")
        if isinstance(cf_daily, list):
            cleaned_cf: list[dict] = []
            for row in cf_daily:
                if not isinstance(row, dict):
                    continue
                date = str(row.get("date") or "").strip()[:10]
                if not date:
                    continue
                cleaned_cf.append(
                    {
                        "date": date,
                        "ok": max(0, int(row.get("ok") or 0)),
                        "cf": max(0, int(row.get("cf") or 0)),
                        "image_fail": max(0, int(row.get("image_fail") or 0)),
                    }
                )
            cleaned_cf.sort(key=lambda r: str(r.get("date") or ""))
            normalized["cf_daily"] = cleaned_cf[-7:]
        else:
            normalized["cf_daily"] = []
        normalized["proxy_cf_ok"] = bool(normalized.get("proxy_cf_ok"))
        try:
            normalized["proxy_cf_ok_at"] = float(normalized.get("proxy_cf_ok_at") or 0)
        except (TypeError, ValueError):
            normalized["proxy_cf_ok_at"] = 0.0
        normalized["proxy_cf_probe_endpoint"] = str(normalized.get("proxy_cf_probe_endpoint") or "").strip().lower()
        normalized["proxy_cf_classification"] = str(normalized.get("proxy_cf_classification") or "").strip()
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
            if not force and self._observe_import_refresh_grace_active(account):
                return active_token
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

        from services.account_fingerprint import build_diversified_fp
        from services.proxy_service import proxy_settings

        seed = str((account or {}).get("email") or proxy or "login")
        fp = build_diversified_fp(seed)
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
        recent_candidates: list[tuple[tuple, str]] = []
        fallback_candidates: list[tuple[tuple, str]] = []
        hot_emails: set[str] = set()
        try:
            from services.account_warmup_service import account_warmup_service

            hot_emails = {str(e).strip().lower() for e in account_warmup_service.hot_emails()}
        except Exception:
            hot_emails = set()
        with self._lock:
            accounts = [dict(item) for item in self._accounts.values()]
            for item in accounts:
                token = str(item.get("access_token") or "")
                email_key = str(item.get("email") or "").strip().lower()
                if config.dispatch_hot_only and hot_emails and email_key not in hot_emails:
                    continue
                if (
                        self._is_image_account_schedulable(item)
                        and self._is_image_interval_ready(item)
                        and not self._cohort_paused(item)
                        and not self._is_warmup_dispatch_blocked(item)
                        and self._account_matches_plan_type(item, plan_type)
                        and self._account_matches_any_plan_type(item, plan_types)
                        and self._account_matches_source_type(item, source_type)
                        and token
                        and token not in excluded
                        and not self._is_image_preflight_backed_off(token)
                ):
                    candidate = (self._image_candidate_sort_key(item), token)
                    fallback_candidates.append(candidate)
                    if self._is_recent_image_quota(item) or self._quota_window_due_for_lazy_refresh(item):
                        recent_candidates.append(candidate)
        candidates = (
            recent_candidates
            if config.image_require_recent_quota_refresh and recent_candidates
            else fallback_candidates
        )
        candidates.sort()
        tokens = [token for _sort_key, token in candidates]
        try:
            if config.get_image_pipeline_settings().get("enabled"):
                from services.image_pipeline.aci_ranker import sort_tokens_by_aci

                tokens = sort_tokens_by_aci(lambda token: self.get_account(token) or {}, tokens)
        except Exception:
            pass
        return tokens

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        binding_limit = self._image_binding_inflight_max()
        with self._lock:
            available: list[str] = []
            for token in self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types):
                if int(self._image_inflight.get(token, 0)) >= max_concurrency:
                    continue
                account = self._accounts.get(token) or {}
                binding = self._account_binding_hash(account)
                if binding and self._binding_image_inflight_locked(binding) >= binding_limit:
                    continue
                available.append(token)
            return self._order_tokens_warmup_hot_first(available)

    def _total_image_inflight_locked(self) -> int:
        return sum(max(0, int(value or 0)) for value in self._image_inflight.values())

    def get_image_candidate_runtime_stats(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, int | float | bool]:
        """返回生图取号运行态候选面，避免 health 只暴露静态 quota 水位。

        - ready_candidate_count：通过 schedulable 闸门且未处于 preflight backoff 的候选数。
        - schedulable_candidate_count：与 ready 对齐（ready 池已按 schedulable 构建）。
        - available_candidate_count：ready 里未达到单账号并发上限的候选数。
        - dispatchable_candidate_count：再扣除全局并发闸门后，当前真正可派发的候选数。
        """
        max_account_concurrency = max(1, int(config.image_account_concurrency or 1))
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
            global_limit = self._effective_image_global_concurrency(len(ready_tokens))
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
            "schedulable_candidate_count": len(ready_tokens),
            "available_candidate_count": available_candidate_count,
            "dispatchable_candidate_count": 0 if global_limit_reached else available_candidate_count,
            "image_inflight_count": image_inflight_count,
            "image_account_concurrency_limit": max_account_concurrency,
            "image_global_concurrency_limit": global_limit,
            "image_global_queue_timeout_secs": queue_timeout,
            "image_global_limit_reached": global_limit_reached,
        }

    def get_schedulable_breakdown(self) -> dict[str, Any]:
        """Explain why accounts are excluded from image scheduling (SCHED-001).

        Counts are mutually prioritized for `primary_reason` per account:
        status → failure_evidence → receive_state → soft_cap/quota →
        quota_freshness → dup_binding → interval → backoff → inflight.
        An account may still appear in multiple raw buckets when useful.
        """
        max_account_concurrency = max(1, int(config.image_account_concurrency or 1))
        now = time.time()
        with self._lock:
            items = list(self._accounts.values())
            preflight_until = dict(self._image_preflight_failed_until)
            inflight = dict(self._image_inflight)

        buckets = {
            "excluded_by_status": 0,
            "excluded_by_failure_evidence": 0,
            "excluded_by_receive_state": 0,
            "excluded_by_quota": 0,
            "excluded_by_quota_freshness": 0,
            "excluded_by_dup_binding": 0,
            "excluded_by_dup_egress": 0,
            "excluded_by_interval": 0,
            "excluded_by_backoff": 0,
            "excluded_by_inflight": 0,
            "schedulable": 0,
            "ready_not_dispatchable": 0,
        }
        primary_counts: dict[str, int] = {}
        samples: dict[str, list[str]] = {key: [] for key in buckets}

        def _sample(bucket: str, account: dict) -> None:
            if len(samples[bucket]) >= 5:
                return
            email = str(account.get("email") or account.get("id") or "")[:64]
            if email and email not in samples[bucket]:
                samples[bucket].append(email)

        for account in items:
            if not isinstance(account, dict):
                continue
            token = str(account.get("access_token") or "")
            primary = ""

            status = str(account.get("status") or "")
            if status in {"禁用", "限流", "异常"}:
                buckets["excluded_by_status"] += 1
                primary = primary or "status"
                _sample("excluded_by_status", account)
            if self._has_image_account_failure_evidence(account):
                buckets["excluded_by_failure_evidence"] += 1
                primary = primary or "failure_evidence"
                _sample("excluded_by_failure_evidence", account)
            if self._requires_panda_receive_verification(account):
                buckets["excluded_by_receive_state"] += 1
                primary = primary or "receive_state"
                _sample("excluded_by_receive_state", account)
            if not self._is_image_account_available(account):
                # available already covers status/soft-cap/quota; avoid double-count status-only.
                if status not in {"禁用", "限流", "异常"}:
                    buckets["excluded_by_quota"] += 1
                    primary = primary or "quota"
                    _sample("excluded_by_quota", account)
            elif self._image_quota_freshness_required() and not self._is_recent_image_quota(account):
                if self._image_quota_refresh_time(account) is not None:
                    buckets["excluded_by_quota_freshness"] += 1
                    primary = primary or "quota_freshness"
                    _sample("excluded_by_quota_freshness", account)
            elif config.image_require_recent_quota_refresh and not self._is_recent_image_quota(account):
                buckets["excluded_by_quota_freshness"] += 1
                primary = primary or "quota_freshness"
                _sample("excluded_by_quota_freshness", account)
            if self._active_proxy_binding_duplicate(account):
                buckets["excluded_by_dup_binding"] += 1
                primary = primary or "dup_binding"
                _sample("excluded_by_dup_binding", account)
            if self._active_proxy_egress_duplicate(account):
                buckets["excluded_by_dup_egress"] += 1
                primary = primary or "dup_egress"
                _sample("excluded_by_dup_egress", account)

            schedulable = self._is_image_account_schedulable(account)
            if schedulable:
                buckets["schedulable"] += 1
                _sample("schedulable", account)
                if not self._is_image_interval_ready(account):
                    buckets["excluded_by_interval"] += 1
                    primary = primary or "interval"
                    _sample("excluded_by_interval", account)
                until = float(preflight_until.get(token) or 0)
                if until > now:
                    buckets["excluded_by_backoff"] += 1
                    primary = primary or "backoff"
                    _sample("excluded_by_backoff", account)
                if int(inflight.get(token) or 0) >= max_account_concurrency:
                    buckets["excluded_by_inflight"] += 1
                    primary = primary or "inflight"
                    _sample("excluded_by_inflight", account)
                    buckets["ready_not_dispatchable"] += 1
                elif until > now or not self._is_image_interval_ready(account):
                    buckets["ready_not_dispatchable"] += 1
                else:
                    primary = primary or "ok"
            else:
                primary = primary or "other"

            primary_counts[primary] = int(primary_counts.get(primary) or 0) + 1

        runtime = self.get_image_candidate_runtime_stats()
        return {
            "total": len(items),
            "buckets": buckets,
            "primary_reason_counts": primary_counts,
            "samples": samples,
            "runtime": runtime,
            "generated_at": datetime.now(timezone.utc).isoformat(),
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
                ready = self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
                global_limit = self._effective_image_global_concurrency(len(ready))
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
                if not ready:
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
            preferred_email: str = "",
            excluded_tokens: set[str] | None = None,
    ) -> str:
        """从候选池中获取一个可用的图片生图 token。

        基于本地缓存做初筛，然后通过 fetch_remote_info 做远程验证（token 有效性、配额等）。
        限制最大尝试次数防止 token rotation 导致无限循环。
        """
        prefer = str(preferred_email or "").strip().lower()
        if prefer:
            prefer_token = ""
            with self._lock:
                for account in self._accounts.values():
                    if str(account.get("email") or "").strip().lower() != prefer:
                        continue
                    token = str(account.get("access_token") or "")
                    if not token:
                        continue
                    if account.get("status") in {"禁用", "异常"}:
                        continue
                    prefer_token = token
                    break
            # Sticky：有 preferred 时先等该号 slot，避免 conc 场景抢错号再重试。
            if prefer_token:
                acquired = False
                queue_started_at = time.monotonic()
                queue_timeout = float(config.image_global_queue_timeout_secs or 0.0)
                with self._image_slot_condition:
                    while not acquired:
                        if self._image_slot_available_locked(prefer_token, skip_global_limit=skip_global_limit):
                            self._image_inflight[prefer_token] = int(self._image_inflight.get(prefer_token, 0)) + 1
                            acquired = True
                            break
                        if not skip_global_limit:
                            global_limit = self._effective_image_global_concurrency(
                                len(self._list_ready_candidate_tokens())
                            )
                            if global_limit > 0 and self._total_image_inflight_locked() >= global_limit:
                                if queue_timeout <= 0 or time.monotonic() - queue_started_at >= queue_timeout:
                                    raise RuntimeError("image global concurrency limit reached")
                                self._image_slot_condition.wait(timeout=min(1.0, queue_timeout))
                                continue
                        if queue_timeout <= 0:
                            break
                        if time.monotonic() - queue_started_at >= queue_timeout:
                            break
                        self._image_slot_condition.wait(timeout=min(1.0, queue_timeout))
                if acquired:
                    try:
                        from services.account_workload_policy_service import account_workload_policy_service

                        local_account = self.get_account(prefer_token) or {}
                        gate = account_workload_policy_service.decide_for_account(
                            local_account,
                            "image",
                            access_token=prefer_token,
                        )
                        if gate.mode == "live" and not gate.admitted:
                            self.release_image_slot(prefer_token)
                        elif (
                            self._can_skip_image_preflight(local_account)
                            and self._is_image_account_schedulable(local_account)
                        ):
                            return prefer_token
                        else:
                            account = self.fetch_remote_info(prefer_token, "get_available_access_token_preferred")
                            resolved = str((account or {}).get("access_token") or prefer_token)
                            if resolved != prefer_token:
                                self.release_image_slot(prefer_token)
                                with self._image_slot_condition:
                                    self._image_inflight[resolved] = int(self._image_inflight.get(resolved, 0)) + 1
                                prefer_token = resolved
                            if self._is_image_account_schedulable(account or {}):
                                return prefer_token
                            self.release_image_slot(prefer_token)
                    except RuntimeError:
                        raise
                    except Exception:
                        self.release_image_slot(prefer_token)

        candidate_count = len(self._list_ready_candidate_tokens(
            plan_type=plan_type,
            source_type=source_type,
            plan_types=plan_types,
        ))
        max_attempts = min(max(1, int(config.image_token_max_attempts or 20)), max(1, candidate_count))
        attempted_tokens: set[str] = set(excluded_tokens or set())
        try:
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
                        and not self._quota_window_due_for_lazy_refresh(local_account or {})
                        and self._is_image_account_schedulable(local_account or {})
                        and self._account_matches_plan_type(local_account or {}, plan_type)
                        and self._account_matches_any_plan_type(local_account or {}, plan_types)
                        and self._account_matches_source_type(local_account or {}, source_type)
                ):
                    return str((local_account or {}).get("access_token") or access_token)
                refresh_event = (
                    "lazy_quota_window_refresh"
                    if self._quota_window_due_for_lazy_refresh(local_account or {})
                    else "get_available_access_token"
                )
                try:
                    account = self.fetch_remote_info(access_token, refresh_event)
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
            err = RuntimeError(
                f"no available {plan_type or source_type or ''} image quota (tried {len(attempted_tokens)} tokens)".replace("  ", " ").strip()
                if plan_type or source_type else f"no available image quota (tried {len(attempted_tokens)} tokens)"
            )
        except RuntimeError as exc:
            if "no available" in str(exc).lower() and "image quota" in str(exc).lower():
                err = exc
            else:
                raise
        try:
            setattr(err, "breakdown", self.get_schedulable_breakdown())
        except Exception:
            setattr(err, "breakdown", None)
        raise err

    def get_text_access_token(
        self,
        excluded_tokens: set[str] | None = None,
        *,
        preferred_email: str = "",
    ) -> str:
        excluded = set(excluded_tokens or set())
        prefer = str(preferred_email or "").strip().lower()
        from services.account_workload_policy_service import account_workload_policy_service

        if prefer:
            prefer_token = ""
            with self._lock:
                for account in self._accounts.values():
                    if str(account.get("email") or "").strip().lower() != prefer:
                        continue
                    token = str(account.get("access_token") or "")
                    if not token or token in excluded:
                        continue
                    if account.get("status") in {"禁用", "异常"}:
                        continue
                    prefer_token = token
                    break
            if prefer_token:
                return self.refresh_access_token(prefer_token, event="get_text_access_token") or prefer_token

        # Snapshot hot emails outside account lock to avoid AB-BA with warmup._lock.
        try:
            from services.account_warmup_service import account_warmup_service

            hot = {str(e).strip().lower() for e in account_warmup_service.hot_emails()}
        except Exception:
            hot = set()

        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (token := account.get("access_token") or "")
                   and token not in excluded
                   and self._is_text_interval_ready(account)
                   and not self._cohort_paused(account)
            ]
            if not candidates:
                return ""
            if hot:
                hot_tokens: list[str] = []
                cold_tokens: list[str] = []
                for token in candidates:
                    acc = self._accounts.get(token) or {}
                    email = str(acc.get("email") or "").strip().lower()
                    (hot_tokens if email in hot else cold_tokens).append(token)
                ordered = hot_tokens + cold_tokens if hot_tokens else list(candidates)
            else:
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
            next_item = self._stamp_text_next_ok(next_item)
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._persist_upsert_accounts([account])

    def remember_text_conversation(
        self,
        access_token: str,
        *,
        conversation_id: str = "",
        parent_message_id: str = "",
    ) -> None:
        """Persist independent text-session ids for nurture continue (never image cid)."""
        if not access_token:
            return
        cid = str(conversation_id or "").strip()
        parent = str(parent_message_id or "").strip()
        if not cid and not parent:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            persist = bool(current.get("chat_persist_history")) or bool(
                getattr(config, "text_chat_persist_history", False)
            )
            reuse = bool(current.get("chat_reuse_conversation")) or bool(
                getattr(config, "text_chat_reuse_conversation", False)
            )
            if not (persist and reuse):
                return
            image_cid = str(current.get("last_image_conversation_id") or "").strip()
            if cid and image_cid and cid == image_cid:
                return
            next_item = dict(current)
            if cid:
                next_item["text_conversation_id"] = cid
            if parent:
                next_item["text_parent_message_id"] = parent
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
            # 文本/生图流量路径顺带采样当日出口，供号池 IP 漂移 7 灯
            try:
                self.record_egress_sample(access_token, status="ok")
            except Exception:
                pass
            # 有真实业务流量即记一次被动成功样本（CF 灯）
            try:
                self.record_cf_sample(access_token, kind="ok")
            except Exception:
                pass
            return True

    @staticmethod
    def _looks_like_cf_edge_message(message: object) -> bool:
        text = str(message or "").lower()
        return (
            "cloudflare_or_edge_html_block" in text
            or "cf_edge_block" in text
            or "cloudflare_or_edge" in text
        )

    def record_cf_sample(self, access_token: str, *, kind: str) -> bool:
        """被动累计 Asia/Shanghai 当日 CF/成功/生图失败（最多保留 7 天）。不做主动探测。"""
        if not access_token:
            return False
        key = str(kind or "").strip().lower()
        if key not in {"ok", "cf", "image_fail"}:
            return False
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            next_item = dict(current)
            day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            history = list(next_item.get("cf_daily") or [])
            if not isinstance(history, list):
                history = []
            row: dict | None = None
            for item in history:
                if isinstance(item, dict) and str(item.get("date") or "") == day:
                    row = dict(item)
                    break
            if row is None:
                row = {"date": day, "ok": 0, "cf": 0, "image_fail": 0}
            row["ok"] = max(0, int(row.get("ok") or 0))
            row["cf"] = max(0, int(row.get("cf") or 0))
            row["image_fail"] = max(0, int(row.get("image_fail") or 0))
            row[key] = int(row.get(key) or 0) + 1
            history = [item for item in history if isinstance(item, dict) and str(item.get("date") or "") != day]
            history.append(row)
            history.sort(key=lambda r: str(r.get("date") or ""))
            next_item["cf_daily"] = history[-7:]
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[access_token] = account
            self._persist_upsert_accounts([account])
            return True

    def reset_observability_lights(self, access_token: str) -> bool:
        """清空 cf_daily / egress_daily，用于换绑干净代理后重置指示灯。"""
        if not access_token:
            return False
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            next_item = dict(current)
            next_item["cf_daily"] = []
            next_item["egress_daily"] = []
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[access_token] = account
            self._persist_upsert_accounts([account])
            return True

    def record_egress_sample(
        self,
        access_token: str,
        *,
        ip: str = "",
        hash_value: str = "",
        status: str = "ok",
    ) -> bool:
        """写入/更新 Asia/Shanghai 当日出口样本（最多保留 7 天）。"""
        if not access_token:
            return False
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            next_item = dict(current)
            sample_ip = str(ip or next_item.get("proxy_egress_ip") or "").strip()
            sample_hash = str(hash_value or next_item.get("proxy_egress_hash") or "").strip()
            day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            st = str(status or "ok").strip().lower() or "ok"
            if st not in {"ok", "warn", "error"}:
                st = "ok"
            if not sample_ip and not sample_hash:
                if st != "error":
                    return False
            reg = str(next_item.get("registration_egress_hash") or "").strip()
            history = list(next_item.get("egress_daily") or [])
            if not isinstance(history, list):
                history = []
            prev_hash = ""
            for row in history:
                if isinstance(row, dict) and str(row.get("date") or "") < day:
                    prev_hash = str(row.get("hash") or "").strip() or prev_hash
            if st == "ok" and sample_hash:
                if reg and sample_hash != reg:
                    st = "warn"
                elif prev_hash and sample_hash != prev_hash:
                    st = "warn"
            sample = {"date": day, "ip": sample_ip, "hash": sample_hash[:16], "status": st}
            history = [row for row in history if isinstance(row, dict) and str(row.get("date") or "") != day]
            history.append(sample)
            history.sort(key=lambda r: str(r.get("date") or ""))
            next_item["egress_daily"] = history[-7:]
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
        now = time.time()
        now_dt = datetime.now(timezone.utc)

        def _iso_utc(ts: float | None) -> str | None:
            if ts is None or ts <= 0:
                return None
            try:
                return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError, OverflowError):
                return None

        def _secs_until(ts: float | None) -> int | None:
            if ts is None or ts <= 0:
                return None
            return max(0, int(math.ceil(float(ts) - now)))

        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))

                lazy_at = self._lazy_refresh_eligible_at(account)
                if lazy_at is not None:
                    if lazy_at.tzinfo is None:
                        lazy_at = lazy_at.replace(tzinfo=timezone.utc)
                    account["lazy_refresh_eligible_at"] = lazy_at.isoformat()
                    account["lazy_refresh_in_sec"] = max(0, int(math.ceil((lazy_at - now_dt).total_seconds())))
                else:
                    account["lazy_refresh_eligible_at"] = None
                    account["lazy_refresh_in_sec"] = None

                try:
                    image_next = float(account.get("image_next_ok_ts") or 0)
                except (TypeError, ValueError):
                    image_next = 0.0
                account["image_next_ok_at"] = _iso_utc(image_next if image_next > 0 else None)
                account["image_next_ok_in_sec"] = _secs_until(image_next if image_next > 0 else None)

                try:
                    text_next = float(account.get("text_next_ok_ts") or 0)
                except (TypeError, ValueError):
                    text_next = 0.0
                account["text_next_ok_at"] = _iso_utc(text_next if text_next > 0 else None)
                account["text_next_ok_in_sec"] = _secs_until(text_next if text_next > 0 else None)

                account["image_schedulable"] = self._is_image_account_schedulable(account)
                account["image_quota_state"] = self.image_quota_state(account)
                account["available_image_quota"] = self.available_image_quota_for_account(account)

                result.append(account)
            return result

    def get_total_image_inflight(self) -> int:
        with self._lock:
            return self._total_image_inflight_locked()

    def reconcile_inflight(
        self,
        *,
        expected_by_token: dict[str, int],
        force: bool = False,
        tokens: set[str] | tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, object]:
        """Compare memory ``_image_inflight`` vs tasks actually holding account slots.

        `force` 打开纠正权；`tokens` 省略时保持原「全池纠正」语义，给出时只纠正
        白名单内的 token（调用方已逐个确认过的 stale 子集）。漂移**观测**始终覆盖
        全池，否则 /health 会跟着瞎掉 —— 白名单只收窄写，不收窄看。
        """
        drift: dict[str, dict[str, object]] = {}
        corrected = 0
        allow: set[str] | None = None if tokens is None else {str(t) for t in tokens}
        with self._image_slot_condition:
            reported = {k: int(v or 0) for k, v in self._image_inflight.items()}
            all_tokens = set(reported) | set(expected_by_token)
            for token in all_tokens:
                memory = int(reported.get(token, 0))
                expected = int(expected_by_token.get(token, 0))
                if memory != expected:
                    account = self._accounts.get(token) or {}
                    drift[inflight_token_fingerprint(token)] = {
                        "memory": memory,
                        "expected": expected,
                        # 只有指纹的话运维无法定位到账号；email 与 watchdog
                        # `_over_counted` / schedulable_breakdown samples 的口径一致。
                        "email": str(account.get("email") or "") or None,
                    }
                    if force and memory > expected and (allow is None or token in allow):
                        if expected <= 0:
                            self._image_inflight.pop(token, None)
                        else:
                            self._image_inflight[token] = expected
                        corrected += 1
            total_memory = self._total_image_inflight_locked()
            total_expected = sum(max(0, int(v)) for v in expected_by_token.values())
            if corrected:
                # 回收的槽位必须立刻唤醒 _acquire_next_candidate_token() 里 wait 的
                # 取号线程，否则要等下一个无关的 release/notify 才醒 —— 等于白回收。
                self._image_slot_condition.notify_all()
        return {
            "drift_count": len(drift),
            "drift": drift,
            "total_memory": total_memory,
            "total_expected": total_expected,
            "corrected": corrected,
        }

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
            if any(
                key in incoming
                for key in ("quota", "limits_progress", "image_quota_unknown", "restore_at", "status")
            ):
                merged["last_quota_refresh_at"] = datetime.now(timezone.utc).isoformat()
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

    def set_soft_band_override(
        self,
        access_token: str,
        *,
        percent: float | None,
        quiet: bool = True,
    ) -> dict | None:
        """手动指定软限流阈值（百分比 5–99）；传 None 清除覆盖，恢复自动抽带。"""
        token = str(access_token or "").strip()
        if not token:
            return None
        updates: dict[str, object] = {}
        if percent is None:
            updates["image_soft_band_override"] = None
        else:
            value = max(5.0, min(99.0, float(percent))) / 100.0
            updates["image_soft_band_override"] = round(value, 4)
            updates["image_soft_band"] = round(value, 4)
        account = self.update_account(token, updates, quiet=True)
        if account is None:
            return None
        # 立刻按覆盖阈值重算熔断态
        with self._lock:
            live = self._accounts.get(account["access_token"])
            if live is not None:
                refreshed = self._apply_humanlike_quota_fields(dict(live))
                self._accounts[account["access_token"]] = refreshed
                self._persist_upsert_accounts([refreshed])
                account = dict(refreshed)
        if not quiet:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "手动软限流%",
                {
                    "token": anonymize_token(account.get("access_token")),
                    "override": account.get("image_soft_band_override"),
                    "band": account.get("image_soft_band"),
                },
            )
        return account

    def get_accounts_usage_recent(self, days: int = 6) -> dict[str, Any]:
        """按邮箱聚合今日+过去 N-1 日的生图/对话次数（供号池「记录」列）。"""
        days = max(1, min(int(days or 6), 14))
        today = datetime.now(timezone(timedelta(hours=8))).date()
        dates = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
        empty_day = {
            "images": 0,
            "images_api": 0,
            "images_chat": 0,
            "dialogues": 0,
            "dialogues_real": 0,
            "dialogues_nurture": 0,
        }
        buckets: dict[str, dict[str, dict[str, int]]] = {}

        def touch(email: str, day: str, key: str) -> None:
            mail = str(email or "").strip().lower()
            if not mail or day not in dates:
                return
            daymap = buckets.setdefault(mail, {d: dict(empty_day) for d in dates})
            daymap[day][key] = int(daymap[day].get(key) or 0) + 1
            if key in {"images_api", "images_chat"}:
                daymap[day]["images"] = int(daymap[day].get("images") or 0) + 1
            if key in {"dialogues_real", "dialogues_nurture"}:
                daymap[day]["dialogues"] = int(daymap[day].get("dialogues") or 0) + 1

        start = dates[0]
        end = dates[-1]
        # 号池「记录」列只需近期成功 CALL；缩小扫描量加快强刷
        for item in log_service.list(type=LOG_TYPE_CALL, start_date=start, end_date=end, limit=8000):
            day = str(item.get("time") or "")[:10]
            summary = str(item.get("summary") or "")
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            status = str(detail.get("status") or "").lower()
            ok = status in {"success", "ok", ""} and "失败" not in summary and "超时" not in summary
            if not ok:
                continue
            email = str(detail.get("account_email") or "")
            if summary.startswith("对话生图"):
                touch(email, day, "images_chat")
            elif summary.startswith("文生图") or summary.startswith("图生图"):
                touch(email, day, "images_api")
            elif summary.startswith("文本生成"):
                # 对话页真实聊天：以 CALL 为准（流式结束带 account_email），避免漏计
                touch(email, day, "dialogues_real")

        hash_to_email: dict[str, str] = {}
        with self._lock:
            for account in self._accounts.values():
                token = str(account.get("access_token") or "")
                email = str(account.get("email") or "").strip().lower()
                if token and email:
                    from services.log_service import _account_hash

                    hash_to_email[_account_hash(token)] = email

        for item in log_service.list(type=LOG_TYPE_LLM_OPS, start_date=start, end_date=end, limit=4000):
            day = str(item.get("time") or "")[:10]
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            kind = str(detail.get("kind") or "").lower()
            outcome = str(detail.get("outcome") or "").lower()
            if outcome not in {"ok", "success", ""}:
                continue
            mail = str(detail.get("account_email") or detail.get("email") or "").strip()
            if not mail:
                mail = hash_to_email.get(str(detail.get("account_hash") or "").strip(), "")
            # 真实对话已由 CALL「文本生成」计入；chat_image 已由 CALL「对话生图」计入
            if kind == "nurture":
                touch(mail, day, "dialogues_nurture")

        by_email = {
            email: [{"date": day, **daymap[day]} for day in dates]
            for email, daymap in buckets.items()
        }
        return {"days": days, "dates": dates, "by_email": by_email}

    @classmethod
    def is_manual_scheduling_enabled(cls, account: dict | None) -> bool:
        """人工调度开关：空 receive_state 视为可调度；仅 verified* 正式入池。"""
        if not isinstance(account, dict):
            return False
        receive_state = str(account.get("panda_receive_state") or "").strip().lower()
        if not receive_state:
            return True
        return receive_state in {"verified_ready", "verified", "local_verified"}

    def set_account_scheduling(
        self,
        access_token: str,
        *,
        enabled: bool,
        reason: str = "",
        quiet: bool = True,
    ) -> dict | None:
        """人工进/出调度：进=verified_ready（clear_isolation），出=identity_isolated。"""
        target_state = "verified_ready" if enabled else "identity_isolated"
        default_reason = "manual_enter_schedule" if enabled else "manual_exit_schedule"
        return self.update_account_identity(
            access_token,
            {"panda_receive_state": target_state},
            reason=str(reason or "").strip() or default_reason,
            quiet=quiet,
            clear_isolation=bool(enabled),
        )

    def set_accounts_scheduling(
        self,
        access_tokens: list[str],
        *,
        enabled: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        updated = 0
        skipped = 0
        errors: list[dict[str, str]] = []
        last_item: dict | None = None
        for raw in access_tokens:
            token = str(raw or "").strip()
            if not token:
                skipped += 1
                continue
            try:
                item = self.set_account_scheduling(token, enabled=enabled, reason=reason, quiet=True)
            except Exception as exc:
                errors.append({"token": anonymize_token(token), "error": str(exc)})
                continue
            if item is None:
                skipped += 1
                continue
            updated += 1
            last_item = item
        return {
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "enabled": bool(enabled),
            "item": last_item,
            "stats": self.get_stats(),
        }

    def update_account_identity(
        self,
        access_token: str,
        updates: dict,
        *,
        reason: str = "",
        quiet: bool = True,
        clear_isolation: bool = False,
    ) -> dict | None:
        """显式身份修复/迁移入口：允许改绑 proxy / fp，并写审计字段。

        clear_isolation=True 时允许把 identity_isolated 升回 verified_ready
        （仅用于确认已换到独立 proxy binding 的运维换绑）。
        """

        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            incoming = dict(updates or {})
            if not clear_isolation:
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
            proxy_keys = ("proxy", "proxy_binding_hash", "proxy_egress_hash")
            if any(str(account.get(k) or "") != str(current.get(k) or "") for k in proxy_keys):
                account = dict(account)
                account["cf_daily"] = []
                account["egress_daily"] = []
                account["proxy_cf_ok"] = False
                account["proxy_cf_ok_at"] = 0
                account["proxy_cf_probe_endpoint"] = ""
                account["proxy_cf_classification"] = ""
                account = self._normalize_account(account)
                if account is None:
                    return None
            self._accounts[access_token] = account
            cf_meta_keys = ("proxy_cf_ok", "proxy_cf_ok_at", "proxy_cf_probe_endpoint", "proxy_cf_classification")
            force_persist = any(key in incoming for key in cf_meta_keys)
            if account != current or force_persist:
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
                self._note_cohort_terminal(account)
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

    @staticmethod
    def _decrement_stored_image_quota(account: dict) -> dict:
        """Decrement cached quota and mirror into limits_progress.image_gen.remaining."""
        item = dict(account)
        if AccountService._is_true_unlimited_image_account(item) or bool(item.get("image_quota_unknown")):
            return item
        next_quota = max(0, int(item.get("quota") or 0) - 1)
        item["quota"] = next_quota
        limits = item.get("limits_progress")
        if not isinstance(limits, list):
            return item
        updated: list[dict] = []
        for entry in limits:
            if not isinstance(entry, dict):
                updated.append(entry)
                continue
            feature = str(entry.get("feature_name") or "").strip().lower().replace("-", "_")
            if feature != "image_gen":
                updated.append(entry)
                continue
            row = dict(entry)
            try:
                remaining = int(row.get("remaining"))
            except (TypeError, ValueError):
                remaining = next_quota + 1
            row["remaining"] = max(0, remaining - 1)
            updated.append(row)
        item["limits_progress"] = updated
        return item

    def mark_image_result(
        self,
        access_token: str,
        success: bool,
        *,
        error: object = None,
        skip_cf_sample: bool = False,
    ) -> dict | None:
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
                next_item["image_fail_streak"] = 0
                if not is_true_unlimited and not image_quota_unknown:
                    next_item = self._decrement_stored_image_quota(next_item)
                if not is_true_unlimited and not image_quota_unknown and next_item["quota"] == 0:
                    # A2-4：硬额度归零与软熔断同规（见 _apply_humanlike_quota_fields 内注释
                    # 「软熔断只用 flag，禁止改 status=限流」）。写 status 会落库，并关死
                    # _quota_window_due_for_lazy_refresh() 的懒刷新逃生口 → 耗尽一次即永久退池。
                    # 这里只打 flag + 保留 restore_at：窗口未到仍不可派发，窗口一过自动复活。
                    next_item["restore_at"] = next_item.get("restore_at") or None
                    if self._has_quota_window_anchor(next_item):
                        # 无时间锚点时不打 flag，否则会换来另一个永久沉底；
                        # quota==0 本身已挡住派发（见 _has_quota_window_anchor 注释）。
                        next_item["image_soft_capped"] = True
                    if config.auto_remove_rate_limited_accounts:
                        # 运维显式要求「自动移除限流账号」：保留旧语义供下方删除分支识别。
                        next_item["status"] = "限流"
                    elif next_item.get("status") == "限流":
                        next_item["status"] = "正常"
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
                next_item = self._stamp_image_next_ok(next_item)
                next_item = self._apply_humanlike_quota_fields(next_item)
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
                streak = int(next_item.get("image_fail_streak") or 0) + 1
                next_item["image_fail_streak"] = streak
                settings = config.get_scheduler_settings()
                if settings.get("enabled") and streak >= int(settings.get("fail_streak_threshold") or 3):
                    from services.humanlike_scheduler import fail_cooldown_seconds

                    cool = fail_cooldown_seconds(
                        min_sec=float(settings.get("fail_cooldown_min_sec") or 1800),
                        max_sec=float(settings.get("fail_cooldown_max_sec") or 5400),
                    )
                    next_item["image_fail_cooldown_until"] = time.time() + cool
                next_item = self._stamp_image_next_ok(next_item)
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
        # 被动 CF 灯：成功记 ok；失败若是 CF 只记 cf，否则记 image_fail（避免双计）
        if not skip_cf_sample:
            try:
                if success:
                    # 成功通常已由 record_account_traffic 记过 ok；此处不再重复
                    pass
                elif self._looks_like_cf_edge_message(error):
                    self.record_cf_sample(access_token, kind="cf")
                    try:
                        from services.proxy_cf_failover import swap_account_proxy_on_cf

                        swap_account_proxy_on_cf(access_token)
                    except Exception:
                        pass
                else:
                    self.record_cf_sample(access_token, kind="image_fail")
            except Exception:
                pass
        return dict(self._accounts.get(access_token) or account or {})

    def fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        defer_invalid_removal: bool = True,
    ) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        _resolved_token, cached = self._get_account_for_token(access_token)
        if cached and self._observe_import_refresh_grace_active(cached):
            return dict(cached)

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
        scheduling_enabled = sum(
            1 for a in items if self.is_manual_scheduling_enabled(a) and a.get("status") == "正常"
        )
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
            max(0, self.available_image_quota_for_account(a))
            for a in items
            if self.available_image_quota_for_account(a) > 0
        )
        available_image_quota = verified_total_quota
        latest_quota_refresh_at: str | None = None
        for a in items:
            if not self._is_image_account_schedulable(a):
                continue
            ts = self._image_quota_refresh_time(a)
            if ts is None:
                continue
            iso = ts.isoformat()
            if latest_quota_refresh_at is None or iso > latest_quota_refresh_at:
                latest_quota_refresh_at = iso
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
            # 可调度 = 人工进调度且状态正常（与号池「进/出调度」开关一致）
            "schedulable": scheduling_enabled,
            "scheduling_enabled": scheduling_enabled,
            # 生图即时候选（额度/失败证据/绑定等额外门槛），仅供运维 breakdown
            "image_schedulable": schedulable,
            "tainted_count": tainted,
            "panda_incoming_count": panda_incoming,
            "panda_verified_count": panda_verified,
            "panda_rejected_count": panda_rejected,
            "verified_quota_count": verified_quota_count,
            "stale_quota_count": stale_quota_count,
            "verified_total_quota": verified_total_quota,
            "available_image_quota": available_image_quota,
            "latest_quota_refresh_at": latest_quota_refresh_at,
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
                "images": 0,
                "images_api": 0,
                "images_chat": 0,
                "dialogues": 0,
                "dialogues_real": 0,
                "dialogues_nurture": 0,
            }
            for offset in range(days)
        }

        def add(day: str | None, key: str, count: int = 1) -> None:
            if day in buckets and count > 0:
                buckets[day][key] += count
                if key in {"images_api", "images_chat"}:
                    buckets[day]["images"] += count
                if key in {"dialogues_real", "dialogues_nurture"}:
                    buckets[day]["dialogues"] += count

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

        # api 生图：call 文生图/图生图；对话生图：call 对话生图
        for item in log_service.list(type=LOG_TYPE_CALL, start_date=start.isoformat(), end_date=today.isoformat(), limit=20000):
            day = str(item.get("time") or "")[:10]
            summary = str(item.get("summary") or "")
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            status = str(detail.get("status") or "").lower()
            ok = status in {"success", "ok", ""} and "失败" not in summary and "超时" not in summary
            if not ok:
                continue
            if summary.startswith("对话生图"):
                add(day, "images_chat")
            elif summary.startswith("文生图") or summary.startswith("图生图"):
                add(day, "images_api")
            elif summary.startswith("文本生成"):
                add(day, "dialogues_real")

        # 拟人对话（llm_ops nurture）；真实对话/对话生图以 CALL 为准，避免双计
        for item in log_service.list(type=LOG_TYPE_LLM_OPS, start_date=start.isoformat(), end_date=today.isoformat(), limit=20000):
            day = str(item.get("time") or "")[:10]
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            kind = str(detail.get("kind") or "").lower()
            outcome = str(detail.get("outcome") or "").lower()
            if outcome not in {"ok", "success", ""}:
                continue
            if kind == "nurture":
                add(day, "dialogues_nurture")
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
