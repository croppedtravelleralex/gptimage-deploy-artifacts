"""常驻预热：对可调度账号周期性 bootstrap / requirements 探活，并按并发/频率轮换热池。

热池账号加速首轮 TTFT；同号并发会话达阈或短时频率过高时 demote，另选号补位。
`depth=requirements` 时走 prepare+finalize 全链，并写入被动 cf_daily 样本。
连续 CF 探活失败达阈后暂停对该号的 warmup，避免坏号被反复打。

CF 封禁语义（audit A2）：
- "连续"由滑动窗口 `cf_fail_window_sec` 定义，相隔更久的失败不再累积成 streak。
- 封禁时长是分钟级基准 `cf_block_sec` + 阶梯退避，上限 `cf_block_max_sec`；
  退避记忆本身也带衰减窗口，长期清白的号会退回基准时长。
- 被封号会被**主动限速复探**（`cf_reprobe_interval_sec` / `cf_reprobe_max_per_tick`），
  探活成功即刻解封，自愈不再只依赖墙钟到期或进程重启。
- 所有封禁状态纯内存：手动解封与自动解封都不跨重启保留。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from services.account_service import account_service
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from utils.log import logger


def _settings() -> dict[str, Any]:
    return dict(config.get_account_warmup_settings())


class AccountWarmupService:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        # 注入时钟便于测试推进时间，无需 sleep。
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # email -> last successful warmup at
        self._hot: dict[str, float] = {}
        # email -> demoted_until
        self._demoted_until: dict[str, float] = {}
        # email -> blocked_until (CF probe fail streak)
        self._blocked_until: dict[str, float] = {}
        self._cf_fail_streak: dict[str, int] = defaultdict(int)
        # email -> 滑动窗口内的 CF 失败时间戳（streak 的真实来源，_cf_fail_streak 是其派生视图）
        self._cf_fail_events: dict[str, deque[float]] = defaultdict(deque)
        # email -> 衰减窗口内的封禁时间戳（阶梯退避的指数来源）
        self._cf_block_events: dict[str, deque[float]] = defaultdict(deque)
        # email -> 下次允许复探被封号的时刻（A2-2 自愈限速）
        self._reprobe_at: dict[str, float] = {}
        # email -> 仅探活冷却到期时刻；不参与 is_dispatch_blocked，绝不影响派发
        self._probe_paused_until: dict[str, float] = {}
        # email -> inflight session count
        self._inflight: dict[str, int] = defaultdict(int)
        # email -> recent start timestamps
        self._starts: dict[str, deque[float]] = defaultdict(deque)
        self._rotate_index = 0
        self._last_error = ""
        self._last_ok_at = 0.0
        self._totals = {
            "ticks": 0,
            "warmed": 0,
            "demoted": 0,
            "errors": 0,
            "rotated": 0,
            "blocked": 0,
            "skipped_blocked": 0,
            "reprobed": 0,
            "recovered": 0,
            "unblocked": 0,
            "expired": 0,
        }

    def _now(self) -> float:
        return float(self._clock())

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-warmup", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def demote(self, email: str, *, reason: str = "manual") -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        settings = _settings()
        with self._lock:
            self._demote_locked(key, settings, reason=reason)

    def hot_emails(self) -> set[str]:
        with self._lock:
            return set(self._hot.keys())

    def status(self) -> dict[str, Any]:
        settings = _settings()
        now = self._now()
        with self._lock:
            return {
                "enabled": settings["enabled"],
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "hot": sorted(self._hot.keys()),
                "hot_count": len(self._hot),
                "inflight": dict(self._inflight),
                "demoted_until": {k: v for k, v in self._demoted_until.items() if v > now},
                "blocked_until": {k: v for k, v in self._blocked_until.items() if v > now},
                "cf_fail_streak": dict(self._cf_fail_streak),
                "cf_block_repeats": {k: len(v) for k, v in self._cf_block_events.items() if v},
                "reprobe_at": {k: v for k, v in self._reprobe_at.items() if k in self._blocked_until},
                "probe_paused_until": {k: v for k, v in self._probe_paused_until.items() if v > now},
                "rotate_index": self._rotate_index,
                "last_error": self._last_error,
                "last_ok_at": self._last_ok_at or None,
                "totals": dict(self._totals),
                "settings": settings,
                # 封禁/解封全部是进程内内存态，不落库、不跨重启。
                "block_state_persistent": False,
            }

    def begin_chat_session(self, email: str) -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        settings = _settings()
        now = self._now()
        with self._lock:
            self._inflight[key] += 1
            q = self._starts[key]
            q.append(now)
            window = float(settings["freq_window_sec"])
            while q and now - q[0] > window:
                q.popleft()
            inflight = self._inflight[key]
            freq = len(q)
            should_demote = key in self._hot and (
                inflight >= int(settings["max_sessions_per_hot"])
                or freq >= int(settings["freq_max_starts"])
            )
            if should_demote:
                self._demote_locked(key, settings, reason="inflight_or_freq")

    def end_chat_session(self, email: str) -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        with self._lock:
            cur = self._inflight.get(key, 0)
            if cur <= 1:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = cur - 1

    def is_hot(self, email: str) -> bool:
        key = str(email or "").strip().lower()
        with self._lock:
            return key in self._hot

    def is_dispatch_blocked(self, email: str) -> bool:
        """CF 探活连续失败或 demote 冷却期内，调度应跳过该号。"""
        key = str(email or "").strip().lower()
        if not key:
            return False
        now = self._now()
        with self._lock:
            return self._is_skipped_locked(key, now)

    def clear_block(self, email: str, *, reason: str = "manual") -> dict[str, Any]:
        """运维手动解封单个账号（A2-3）。

        清除 CF 封禁 + demote 冷却 + streak/退避记忆 + 探活冷却，账号立即恢复可派发。
        语义是「完全清白」：阶梯退避记忆一并清零，下次封禁回到基准时长而非继续翻倍
        （运维手动介入通常意味着已修好上游/代理，不该继续惩罚该号）。
        注意：封禁状态纯内存，本操作**不跨进程重启保留**（重启同样会抹掉封禁本身）。
        """
        key = str(email or "").strip().lower()
        if not key:
            raise ValueError("email is required")
        now = self._now()
        with self._lock:
            blocked_until = float(self._blocked_until.get(key) or 0.0)
            demoted_until = float(self._demoted_until.get(key) or 0.0)
            was_blocked = blocked_until > now
            was_demoted = demoted_until > now
            self._forget_block_state_locked(key)
            self._demoted_until.pop(key, None)
            if was_blocked:
                self._totals["unblocked"] += 1
        logger.info(
            {
                "event": "account_warmup_unblock",
                "email": key,
                "reason": reason,
                "was_blocked": was_blocked,
                "was_demoted": was_demoted,
            }
        )
        return {
            "ok": True,
            "email": key,
            "cleared": bool(was_blocked or was_demoted),
            "was_blocked": was_blocked,
            "was_demoted": was_demoted,
            "blocked_until": blocked_until or None,
            "demoted_until": demoted_until or None,
            "persistent": False,
            "note": "in-memory only; cleared state does not survive a process restart",
        }

    def clear_all_blocks(self, *, reason: str = "manual") -> dict[str, Any]:
        """运维批量解封所有 CF 封禁账号（A2-3）。全池清白，同样不跨重启保留。"""
        now = self._now()
        with self._lock:
            blocked = sorted(e for e, until in self._blocked_until.items() if until > now)
            demoted = sorted(e for e, until in self._demoted_until.items() if until > now)
            # 连未成封的 streak / 探活冷却一并清零，让"全部解封"是可预测的全池清白。
            self._blocked_until.clear()
            self._cf_fail_streak.clear()
            self._cf_fail_events.clear()
            self._cf_block_events.clear()
            self._reprobe_at.clear()
            self._probe_paused_until.clear()
            self._demoted_until.clear()
            self._totals["unblocked"] += len(blocked)
        logger.info(
            {
                "event": "account_warmup_unblock_all",
                "reason": reason,
                "unblocked_count": len(blocked),
                "undemoted_count": len(demoted),
            }
        )
        return {
            "ok": True,
            "cleared": bool(blocked or demoted),
            "unblocked": blocked,
            "unblocked_count": len(blocked),
            "undemoted": demoted,
            "undemoted_count": len(demoted),
            "persistent": False,
            "note": "in-memory only; cleared state does not survive a process restart",
        }

    def _forget_block_state_locked(self, email: str) -> None:
        """丢弃一个账号的全部 CF 封禁派生状态（封禁窗口、streak、退避记忆、探活冷却）。"""
        self._blocked_until.pop(email, None)
        self._cf_fail_streak.pop(email, None)
        self._cf_fail_events.pop(email, None)
        self._cf_block_events.pop(email, None)
        self._reprobe_at.pop(email, None)
        self._probe_paused_until.pop(email, None)

    @staticmethod
    def _fail_window(settings: dict[str, Any]) -> float:
        return max(60.0, float(settings.get("cf_fail_window_sec") or 600.0))

    @staticmethod
    def _reprobe_interval(settings: dict[str, Any]) -> float:
        return max(60.0, float(settings.get("cf_reprobe_interval_sec") or 300.0))

    def _prune_events_locked(self, store: dict[str, deque[float]], email: str, now: float, window: float) -> int:
        """按滑动窗口裁掉过期事件，返回窗口内剩余条数。"""
        q = store.get(email)
        if not q:
            store.pop(email, None)
            return 0
        while q and now - q[0] > window:
            q.popleft()
        if not q:
            store.pop(email, None)
            return 0
        return len(q)

    def _record_cf_fail_locked(self, email: str, settings: dict[str, Any], now: float) -> int:
        """记一次 CF 探活失败，返回滑动窗口内的连败数（A2-1：窗口外的旧失败不再累积）。"""
        window = self._fail_window(settings)
        q = self._cf_fail_events[email]
        q.append(now)
        streak = self._prune_events_locked(self._cf_fail_events, email, now, window)
        if streak:
            self._cf_fail_streak[email] = streak
        else:  # pragma: no cover - 刚 append 过，窗口内至少 1 条
            self._cf_fail_streak.pop(email, None)
        return streak

    def _demote_locked(self, email: str, settings: dict[str, Any], *, reason: str) -> None:
        if email in self._hot:
            self._hot.pop(email, None)
            self._totals["demoted"] += 1
            self._demoted_until[email] = self._now() + float(settings["demote_cooldown_sec"])
            logger.info(
                {
                    "event": "account_warmup_demote",
                    "email": email,
                    "reason": reason,
                    "inflight": self._inflight.get(email, 0),
                    "cooldown_sec": settings["demote_cooldown_sec"],
                }
            )

    def _block_locked(self, email: str, settings: dict[str, Any], *, reason: str, streak: int) -> None:
        """阶梯退避封禁：base × factor^(窗口内历史封禁次数)，夹在 [base, cf_block_max_sec]。

        退避记忆本身带衰减窗口（= cf_block_max_sec），清白足够久的号退回 base，
        避免偶发失败在账号一生中单调累积成长封禁。
        """
        base = max(1.0, float(settings.get("cf_block_sec") or 600.0))
        ceiling = max(base, float(settings.get("cf_block_max_sec") or 3600.0))
        factor = max(1.0, float(settings.get("cf_block_backoff_factor") or 2.0))
        now = self._now()
        repeats = self._prune_events_locked(self._cf_block_events, email, now, ceiling)
        block_sec = min(ceiling, base * (factor**repeats))
        self._cf_block_events[email].append(now)
        self._blocked_until[email] = now + block_sec
        # 立刻排一次复探（A2-2），使自愈不再只依赖墙钟到期。
        self._reprobe_at[email] = now + self._reprobe_interval(settings)
        self._totals["blocked"] += 1
        logger.info(
            {
                "event": "account_warmup_block",
                "email": email,
                "reason": reason,
                "streak": streak,
                "block_sec": block_sec,
                "block_base_sec": base,
                "block_max_sec": ceiling,
                "repeat": repeats + 1,
                "reprobe_at": self._reprobe_at[email],
            }
        )

    def _is_skipped_locked(self, email: str, now: float) -> bool:
        if self._demoted_until.get(email, 0) > now:
            return True
        if self._blocked_until.get(email, 0) > now:
            return True
        return False

    def _is_probe_paused_locked(self, email: str, now: float) -> bool:
        """单次探活失败后的探活冷却。仅用于 tick 选号，不进入 is_dispatch_blocked。"""
        return float(self._probe_paused_until.get(email) or 0.0) > now

    def _skip_for_probe_locked(self, email: str, now: float) -> bool:
        if self._is_skipped_locked(email, now):
            self._totals["skipped_blocked"] += 1
            return True
        return self._is_probe_paused_locked(email, now)

    def _due_reprobe_locked(
        self,
        now: float,
        settings: dict[str, Any],
        emails_alive: set[str] | None = None,
    ) -> list[str]:
        """挑出到期该复探的被封号，并当场预占下一次复探时刻（限速在探活抛异常时也成立）。

        只挑仍在候选池里的号：否则已下线/限流的封禁号会白白吃掉每 tick 的复探配额。
        """
        limit = max(0, int(settings.get("cf_reprobe_max_per_tick") or 0))
        if limit <= 0:
            return []
        due = sorted(
            (float(self._reprobe_at.get(email) or 0.0), email)
            for email, until in self._blocked_until.items()
            if until > now
            and float(self._reprobe_at.get(email) or 0.0) <= now
            and (emails_alive is None or email in emails_alive)
        )
        interval = self._reprobe_interval(settings)
        picked = [email for _, email in due[:limit]]
        for email in picked:
            self._reprobe_at[email] = now + interval
        return picked

    def _loop(self) -> None:
        settings = _settings()
        delay = float(settings["startup_delay_sec"])
        if delay > 0:
            self._stop.wait(delay)
        while not self._stop.is_set():
            settings = _settings()
            if settings["enabled"]:
                try:
                    self._tick(settings)
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"{type(exc).__name__}: {exc}"[:240]
                        self._totals["errors"] += 1
                    logger.warning({"event": "account_warmup_tick_error", "error": str(exc)[:240]})
            self._stop.wait(float(settings["interval_sec"]))

    def _candidate_accounts(self, *, schedulable_only: bool) -> list[dict[str, Any]]:
        """Prefer image-schedulable (verified*) accounts for warm pool."""
        items = account_service.list_accounts()
        out: list[dict[str, Any]] = []
        for account in items:
            if not isinstance(account, dict):
                continue
            if account.get("status") in {"禁用", "异常", "限流"}:
                continue
            receive = str(account.get("panda_receive_state") or "").strip().lower()
            if receive and receive not in {"verified_ready", "verified", "local_verified"}:
                continue
            if schedulable_only and not account_service._is_image_account_schedulable(account):
                continue
            token = str(account.get("access_token") or "").strip()
            email = str(account.get("email") or "").strip().lower()
            if not token or not email:
                continue
            out.append(account)
        return out

    def _tick(self, settings: dict[str, Any]) -> None:
        schedulable_only = bool(settings.get("schedulable_only", True))
        candidates = self._candidate_accounts(schedulable_only=schedulable_only)
        emails_alive = {str(a.get("email") or "").strip().lower() for a in candidates}
        now = self._now()
        fail_window = self._fail_window(settings)
        hot_refresh_min = float(settings.get("hot_refresh_min_interval_sec") or 300.0)
        with self._lock:
            self._totals["ticks"] += 1
            for e, until in list(self._demoted_until.items()):
                if until <= now:
                    self._demoted_until.pop(e, None)
            for e, until in list(self._probe_paused_until.items()):
                if until <= now:
                    self._probe_paused_until.pop(e, None)
            for e, until in list(self._blocked_until.items()):
                if until <= now:
                    self._blocked_until.pop(e, None)
                    self._cf_fail_streak.pop(e, None)
                    self._cf_fail_events.pop(e, None)
                    self._reprobe_at.pop(e, None)
                    self._totals["expired"] += 1
            # A2-1：streak 按滑动窗口衰减，让 status 与判定用同一份真相。
            for e in list(self._cf_fail_events.keys()):
                remaining = self._prune_events_locked(self._cf_fail_events, e, now, fail_window)
                if remaining:
                    self._cf_fail_streak[e] = remaining
                else:
                    self._cf_fail_streak.pop(e, None)
            for e in list(self._cf_block_events.keys()):
                self._prune_events_locked(self._cf_block_events, e, now, float(settings.get("cf_block_max_sec") or 3600.0))
            for email in list(self._hot.keys()):
                if email not in emails_alive:
                    self._hot.pop(email, None)
            need = max(0, int(settings["max_hot"]) - len(self._hot))
            hot_now = set(self._hot.keys())
            hot_last = dict(self._hot)
            reprobe_emails = self._due_reprobe_locked(now, settings, emails_alive)

        max_hot = int(settings["max_hot"])
        hot_to_refresh: list[str] = []
        for email in list(hot_now)[:max_hot]:
            with self._lock:
                if self._skip_for_probe_locked(email, now):
                    continue
            last_at = float(hot_last.get(email) or 0.0)
            if now - last_at >= hot_refresh_min:
                hot_to_refresh.append(email)

        picked: list[dict[str, Any]] = []
        if need > 0:
            for account in candidates:
                email = str(account.get("email") or "").strip().lower()
                if email in hot_now:
                    continue
                with self._lock:
                    if self._skip_for_probe_locked(email, now):
                        continue
                picked.append(account)
                if len(picked) >= need:
                    break

        rotate_n = int(settings.get("rotate_per_tick") or 0)
        rotated: list[dict[str, Any]] = []
        if rotate_n > 0 and candidates:
            with self._lock:
                start = self._rotate_index % max(1, len(candidates))
                self._rotate_index = (start + rotate_n) % max(1, len(candidates))
            pool = candidates[start:] + candidates[:start]
            seen: set[str] = set()
            for account in pool:
                email = str(account.get("email") or "").strip().lower()
                if email in hot_now or email in seen:
                    continue
                with self._lock:
                    if self._skip_for_probe_locked(email, now):
                        continue
                rotated.append(account)
                seen.add(email)
                if len(rotated) >= rotate_n:
                    break

        # A2-2：主动限速复探被封号；探活成功即在 _warmup_one 里解封。
        for email in reprobe_emails:
            account = next(
                (a for a in candidates if str(a.get("email") or "").strip().lower() == email),
                None,
            )
            if account is None:
                continue
            with self._lock:
                self._totals["reprobed"] += 1
            logger.info({"event": "account_warmup_reprobe", "email": email})
            self._warmup_one(account, settings, force=True)

        for email in hot_to_refresh:
            account = next(
                (a for a in candidates if str(a.get("email") or "").strip().lower() == email),
                None,
            )
            if account:
                self._warmup_one(account, settings)

        for account in picked:
            ok = self._warmup_one(account, settings)
            if not ok:
                continue
            email = str(account.get("email") or "").strip().lower()
            with self._lock:
                if len(self._hot) < max_hot:
                    self._hot[email] = self._now()
                    self._totals["warmed"] += 1
                    logger.info({"event": "account_warmup_promote", "email": email})

        for account in rotated:
            if self._warmup_one(account, settings):
                with self._lock:
                    self._totals["rotated"] += 1

    def _warmup_one(
        self,
        account: dict[str, Any],
        settings: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """探活一个账号。

        `force=True` 用于 A2-2 的复探：绕过封禁早退，让被封号有机会走到成功分支解封。
        调用方（`_due_reprobe_locked`）负责限速，此处不再自行限速。
        """
        settings = settings or _settings()
        token = str(account.get("access_token") or "").strip()
        email = str(account.get("email") or "").strip().lower()
        if not token or not email:
            return False
        now = self._now()
        if not force:
            with self._lock:
                if self._is_skipped_locked(email, now):
                    self._totals["skipped_blocked"] += 1
                    return False
        depth = str(settings.get("depth") or "bootstrap").strip().lower()
        backend: OpenAIBackendAPI | None = None
        try:
            backend = OpenAIBackendAPI(access_token=token)
            if depth == "requirements":
                backend._ensure_bootstrap(soft_fail=True)
                backend._get_chat_requirements()
            else:
                backend._ensure_bootstrap()
            account_service.record_cf_sample(token, kind="ok")
            with self._lock:
                ok_at = self._now()
                self._last_ok_at = ok_at
                self._last_error = ""
                recovered = float(self._blocked_until.get(email) or 0.0) > ok_at
                # 成功即清空全部封禁派生状态：streak、退避记忆、复探排期、探活冷却。
                self._forget_block_state_locked(email)
                if recovered:
                    self._totals["recovered"] += 1
                if email in self._hot:
                    self._hot[email] = ok_at
            if recovered:
                logger.info({"event": "account_warmup_recovered", "email": email})
            return True
        except Exception as exc:
            account_service.record_cf_sample(token, kind="cf")
            max_streak = max(1, int(settings.get("cf_fail_max_streak") or 2))
            probe_cooldown = max(0.0, float(settings.get("cf_fail_probe_cooldown_sec") or 0.0))
            with self._lock:
                fail_at = self._now()
                self._last_error = f"{email}: {type(exc).__name__}: {exc}"[:240]
                self._totals["errors"] += 1
                streak = self._record_cf_fail_locked(email, settings, fail_at)
                if probe_cooldown > 0:
                    self._probe_paused_until[email] = fail_at + probe_cooldown
                if email in self._hot:
                    self._demote_locked(email, settings, reason="cf_probe_fail")
                if streak >= max_streak:
                    self._block_locked(email, settings, reason="cf_probe_streak", streak=streak)
            logger.warning(
                {
                    "event": "account_warmup_fail",
                    "email": email,
                    "depth": depth,
                    "streak": streak,
                    "blocked": streak >= max_streak,
                    "reprobe": bool(force),
                    "error": str(exc)[:240],
                }
            )
            return False
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass


account_warmup_service = AccountWarmupService()
