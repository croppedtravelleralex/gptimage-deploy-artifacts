"""Single-task image poll budget (conversation GET + tasks GET + wall time)."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# GET-budget derivation (audit 28 §B6 / fix A4-4).
#
# The poll loop performs exactly one conversation GET per iteration and then sleeps
# `image_poll_interval_secs`. The legacy hidden default of 24 GETs therefore capped
# every poll at ~24 × (interval + RTT) ≈ 82~120s of wall clock, so the configured
# 300s (edit) and 360s (multi-reference) wall budgets were dead — they died at
# roughly 1/3 and 1/4 of their budget while the error blamed the wall timeout.
#
# The wall clock is the budget operators actually configure and reason about, so it
# must be the binding constraint. The GET cap is demoted to an anti-hammering
# backstop: enough headroom for settle iterations (which sleep `image_settle_secs`,
# typically 2s, instead of the poll interval), the extra check-before-hit GET, short
# CF/429 backoff retries and the near-deadline `min(interval, remaining)` clamp,
# while still bounding the request rate at ~2 GETs per poll interval.
_GET_BUDGET_OVERSHOOT_FACTOR = 2
_GET_BUDGET_SLACK_ATTEMPTS = 8
# Sleeps in the loop never go below this in practice; also protects the derivation
# from a zero/absent interval (common in tests) blowing up the cap.
_GET_BUDGET_INTERVAL_FLOOR_SECS = 0.5
_GET_BUDGET_FALLBACK_INTERVAL_SECS = 3.0

_EXHAUST_REASON_LABELS = {
    "wall_time": "墙钟预算耗尽",
    "conversation_get_budget": "轮询 GET 预算耗尽",
    "tasks_get_budget": "tasks GET 预算耗尽",
    # The poll loop can also `break` on repeated upstream 429/5xx/network errors
    # without any budget being spent; that is neither a wall nor a GET exhaustion.
    "retry_exhausted": "上游错误重试后提前退出",
}


def derive_max_conversation_gets(*, timeout_secs: float, poll_interval_secs: float | None) -> int:
    """Derive the conversation GET cap from the wall budget and the poll interval."""
    try:
        interval = float(poll_interval_secs or 0.0)
    except (TypeError, ValueError):
        interval = 0.0
    if interval <= 0:
        interval = _GET_BUDGET_FALLBACK_INTERVAL_SECS
    interval = max(_GET_BUDGET_INTERVAL_FLOOR_SECS, interval)
    try:
        wall = max(0.1, float(timeout_secs))
    except (TypeError, ValueError):
        wall = 0.1
    nominal_attempts = math.ceil(wall / interval)
    return max(1, nominal_attempts * _GET_BUDGET_OVERSHOOT_FACTOR + _GET_BUDGET_SLACK_ATTEMPTS)


_EXHAUST_LOCK = threading.Lock()
_EXHAUST_COUNTS: dict[str, int] = {
    "wall_time": 0,
    "conversation_get_budget": 0,
    "tasks_get_budget": 0,
}


def record_poll_exhausted(reason: str) -> None:
    key = str(reason or "").strip() or "unknown"
    if key not in _EXHAUST_COUNTS:
        # normalize known aliases
        if "conversation" in key:
            key = "conversation_get_budget"
        elif "tasks" in key:
            key = "tasks_get_budget"
        elif "wall" in key:
            key = "wall_time"
        else:
            _EXHAUST_COUNTS.setdefault(key, 0)
    with _EXHAUST_LOCK:
        _EXHAUST_COUNTS[key] = int(_EXHAUST_COUNTS.get(key) or 0) + 1


def poll_exhaust_snapshot() -> dict[str, Any]:
    with _EXHAUST_LOCK:
        return {
            "wall": int(_EXHAUST_COUNTS.get("wall_time") or 0),
            "conversation_get": int(_EXHAUST_COUNTS.get("conversation_get_budget") or 0),
            "tasks": int(_EXHAUST_COUNTS.get("tasks_get_budget") or 0),
            "by_reason": dict(_EXHAUST_COUNTS),
        }


@dataclass
class ImagePollBudget:
    """Per logical image task poll budget. One coordinator instance per poll loop."""

    max_conversation_gets: int
    max_tasks_gets: int
    tasks_every_n_attempts: int
    wall_deadline: float
    conversation_gets: int = 0
    tasks_gets: int = 0
    attempt: int = 0
    exhausted_reason: str = ""
    # Diagnostics so an exhausted poll can report what actually happened
    # (audit 28 §B6 / fix A4-4).
    timeout_secs: float = 0.0
    started_at: float = field(default_factory=time.time)
    get_budget_source: str = "explicit"
    mode: str = ""
    timeout_config_key: str = ""

    @classmethod
    def create(
        cls,
        *,
        timeout_secs: float,
        max_conversation_gets: int | None,
        max_tasks_gets: int,
        tasks_every_n_attempts: int,
        poll_interval_secs: float | None = None,
        mode: str = "",
        timeout_config_key: str = "",
    ) -> "ImagePollBudget":
        """Build a budget for one logical poll loop.

        ``max_conversation_gets=None`` means the operator did not configure
        ``image_poll_max_upstream_gets``; the cap is then derived from the wall
        budget so the configured wall timeout is what actually binds. An explicit
        value is honoured verbatim.
        """
        if max_conversation_gets is None:
            resolved_gets = derive_max_conversation_gets(
                timeout_secs=timeout_secs,
                poll_interval_secs=poll_interval_secs,
            )
            get_budget_source = "derived_from_wall"
        else:
            resolved_gets = max(1, int(max_conversation_gets))
            get_budget_source = "explicit"
        now = time.time()
        return cls(
            max_conversation_gets=resolved_gets,
            max_tasks_gets=max(0, int(max_tasks_gets)),
            tasks_every_n_attempts=max(1, int(tasks_every_n_attempts)),
            wall_deadline=now + max(0.1, float(timeout_secs)),
            timeout_secs=max(0.1, float(timeout_secs)),
            started_at=now,
            get_budget_source=get_budget_source,
            mode=str(mode or ""),
            timeout_config_key=str(timeout_config_key or ""),
        )

    def remaining_wall(self) -> float:
        return self.wall_deadline - time.time()

    def elapsed_wall(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def begin_attempt(self) -> bool:
        if self.remaining_wall() <= 0:
            self.exhausted_reason = "wall_time"
            record_poll_exhausted(self.exhausted_reason)
            return False
        if self.conversation_gets >= self.max_conversation_gets:
            self.exhausted_reason = "conversation_get_budget"
            record_poll_exhausted(self.exhausted_reason)
            return False
        self.attempt += 1
        return True

    def record_conversation_get(self) -> None:
        self.conversation_gets += 1

    def should_query_tasks(self) -> bool:
        if self.tasks_gets >= self.max_tasks_gets:
            return False
        if self.remaining_wall() <= 0:
            return False
        # First attempt + every N thereafter (tasks are low-frequency diagnostics).
        return self.attempt == 1 or (self.attempt % self.tasks_every_n_attempts == 0)

    def record_tasks_get(self) -> None:
        self.tasks_gets += 1

    def effective_exhausted_reason(self) -> str:
        """The reason to report, including loops that exited without spending a budget."""
        if self.exhausted_reason:
            return self.exhausted_reason
        if self.remaining_wall() <= 0:
            return "wall_time"
        if self.conversation_gets >= self.max_conversation_gets:
            return "conversation_get_budget"
        return "retry_exhausted"

    def exhaustion_message(self) -> str:
        """Operator-facing exhaustion text.

        Reports the *real* elapsed wall clock, the *real* exhaustion reason and the
        config key that genuinely governs the mode in play. The previous text always
        blamed the wall timeout and named ``image_poll_timeout_secs``, which is not
        the key that wins for queue-driven modes (audit 28 §5 / §B6).

        Must keep the ``超时`` marker: ``image_task_service._looks_like_timeout`` and
        ``resume_poll`` classify resumable tasks by substring.
        """
        reason = self.effective_exhausted_reason()
        label = _EXHAUST_REASON_LABELS.get(reason, reason)
        head = (
            f"ChatGPT 生图超时：实际已等待 {self.elapsed_wall():.1f} 秒"
            f"（本次墙钟预算 {self.timeout_secs:.0f} 秒），退出原因 {label}（{reason}）。"
        )
        if reason == "conversation_get_budget":
            detail = (
                f"conversation GET 预算 {self.conversation_gets}/{self.max_conversation_gets}"
                f" 已用尽（来源 {self.get_budget_source}），墙钟尚余 "
                f"{max(0.0, self.remaining_wall()):.1f} 秒 —— "
                "请调大 config.json 的 image_poll_max_upstream_gets（或删除该键改用按墙钟推导）。"
            )
        elif reason == "retry_exhausted":
            detail = (
                f"轮询在上游错误重试后提前退出，墙钟尚余 {max(0.0, self.remaining_wall()):.1f} 秒 —— "
                "请查看同 conversation 的 image_poll_retry / image_poll_rate_limited / "
                "image_poll_cf_edge 日志。"
            )
        else:
            key = self.timeout_config_key or "image_poll_timeout_secs"
            mode_txt = f"当前模式 {self.mode}，" if self.mode else ""
            detail = f"{mode_txt}墙钟预算已耗尽 —— 请调大 config.json 的 {key}。"
        return head + detail + "也可能是账号被限流或生图队列拥堵导致。"

    def snapshot(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "conversation_gets": self.conversation_gets,
            "tasks_gets": self.tasks_gets,
            "max_conversation_gets": self.max_conversation_gets,
            "max_tasks_gets": self.max_tasks_gets,
            "remaining_wall_secs": round(max(0.0, self.remaining_wall()), 2),
            "exhausted_reason": self.exhausted_reason,
            "effective_exhausted_reason": self.effective_exhausted_reason(),
            "elapsed_wall_secs": round(self.elapsed_wall(), 2),
            "timeout_secs": round(self.timeout_secs, 2),
            "get_budget_source": self.get_budget_source,
            "mode": self.mode,
            "timeout_config_key": self.timeout_config_key,
        }
