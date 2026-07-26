"""A4-1 / A4-3 回归：冷却戳必须在派发时打；binding 闸门不得锁死单账号并发。

背景：docs/28-scheduling-queue-slot-audit-20260726.md §4 与 §B5。

A4-1  `_stamp_image_next_ok` 原先只在 `mark_image_result`（任务**完成之后**）被调用，
      于是单账号服务周期是 `T_exec + gap` 而不是 `max(T_exec, gap)`。gap 期望 71s，
      7 个可派发账号时冷却受限吞吐（≈8.9 img/min）比并发上限（12 img/min）**更紧**
      —— 冷却才是真天花板。戳移到派发时后冷却与执行重叠。

A4-3  binding 闸门用 `_binding_image_inflight_locked()`，把账号**自己**的在途计进
      自己的 binding 总数；`image_binding_inflight_max = 1` 且生产无空 binding，
      于是 `image_account_concurrency = 2` 永远吃不到，有效单账号并发恒为 1。

所有时间都由注入的假时钟驱动，无 real sleep；可能挂死的路径一律放线程里
`join(timeout=...)` + `assert not thread.is_alive()`。
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time as _REAL_TIME
from pathlib import Path
from threading import Condition, RLock
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")
os.environ.setdefault("STORAGE_BACKEND", "json")

from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend

# 固定 gap。注意**不能**靠 settings 把随机项调零：`_stamp_image_next_ok` 用的是
# `float(settings.get("extra_poisson_lambda_sec") or 8)`，`0 or 8` → 8，指数项照样随机。
GAP = 60.0

_SCHEDULER_SETTINGS = {
    "enabled": True,
    "unrestricted": False,
    "image_min_interval_sec": GAP,
    "jitter_lo": 1.0,
    "jitter_hi": 1.0,
    "extra_poisson_lambda_sec": 0,
    "lazy_refresh_jitter_hours": 0,
    "fail_streak_threshold": 3,
    "fail_cooldown_min_sec": 1800,
    "fail_cooldown_max_sec": 5400,
    "daily_usage_ratio": 0.7,
    "new_account_usage_cap": 0.4,
}


class FakeClock:
    """只替换 `services.account_service` 模块内的 `time`，其余属性委托真 time 模块。

    刻意**不** patch `time.time` 本身：那是进程级副作用，会连 pytest 计时和
    `Condition.wait` 的超时一起改掉。`monotonic` / `sleep` 走 `__getattr__` 落到真模块。
    """

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def __getattr__(self, name):  # pragma: no cover - 纯委托
        return getattr(_REAL_TIME, name)


@contextlib.contextmanager
def scheduling_env(clock: FakeClock, **sched_overrides):
    settings = dict(_SCHEDULER_SETTINGS)
    settings.update(sched_overrides)
    with patch("services.account_service.time", clock), patch(
        "services.account_service.config.get_scheduler_settings", return_value=settings
    ), patch(
        # pipeline 开启会走 sort_tokens_by_aci（读真 time），与假时钟口径不一致
        "services.account_service.config.get_image_pipeline_settings",
        return_value={"enabled": False},
    ), patch(
        "services.humanlike_scheduler.compute_submit_gap_seconds", return_value=GAP
    ):
        yield


@contextlib.contextmanager
def config_data(**overrides):
    """临时改写 config.data 里的顶层键（property 读的就是它），退出时精确还原。"""
    previous = {key: config.data.get(key, _MISSING) for key in overrides}
    config.data.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is _MISSING:
                config.data.pop(key, None)
            else:
                config.data[key] = value


_MISSING = object()


def _account(token: str = "tok-a", **overrides) -> dict:
    base = {
        "access_token": token,
        "email": f"{token}@example.com",
        "status": "正常",
        "type": "free",
        "quota": 50,
        "image_quota_unknown": False,
        "panda_receive_state": "verified_ready",
        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@contextlib.contextmanager
def service_with(*accounts: dict):
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
        service.add_account_items([dict(a) for a in accounts])
        yield service


# =========================================================================== #
# A4-1  冷却戳在派发时打，不在完成时打
# =========================================================================== #

def test_dispatch_stamps_cooldown_at_dispatch_time():
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        token = service._acquire_next_candidate_token()

        account = service.get_account(token)
        assert account["image_next_ok_ts"] == clock.now + GAP
        assert account["image_last_gap_sec"] == GAP
        assert account["image_last_dispatch_ts"] == clock.now


def test_completion_does_not_restamp_cooldown():
    """核心回归：旧代码在完成时打戳 → next_ok 被推到 T_exec + gap。"""
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        token = service._acquire_next_candidate_token()
        next_ok_at_dispatch = service.get_account(token)["image_next_ok_ts"]

        clock.advance(120.0)  # T_exec = 120s
        updated = service.mark_image_result(token, success=True)

        assert updated["image_next_ok_ts"] == next_ok_at_dispatch
        # 旧代码会是 dispatch+120+60 = dispatch+180
        assert updated["image_next_ok_ts"] != clock.now + GAP


def test_long_execution_fully_absorbs_the_cooldown():
    """T_exec(120s) > gap(60s) → 完成瞬间即可再次提交，服务周期 = max 而非 sum。"""
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        token = service._acquire_next_candidate_token()
        clock.advance(120.0)
        service.mark_image_result(token, success=True)

        account = service.get_account(token)
        assert service._is_image_interval_ready(account) is True
        assert service._list_ready_candidate_tokens() == [token]


def test_short_execution_still_pays_the_remaining_cooldown():
    """T_exec(5s) < gap(60s) → 仍须补齐到 dispatch+gap，不许换来上游连击。"""
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        token = service._acquire_next_candidate_token()
        dispatched_at = clock.now
        clock.advance(5.0)
        service.mark_image_result(token, success=True)

        account = service.get_account(token)
        assert service._is_image_interval_ready(account) is False
        assert account["image_next_ok_ts"] == dispatched_at + GAP

        clock.advance(GAP - 5.0)
        assert service._is_image_interval_ready(service.get_account(token)) is True


def test_failure_path_also_keeps_the_dispatch_anchored_stamp():
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        token = service._acquire_next_candidate_token()
        next_ok_at_dispatch = service.get_account(token)["image_next_ok_ts"]

        clock.advance(90.0)
        updated = service.mark_image_result(token, success=False, error="boom")

        assert updated["fail"] == 1
        assert updated["image_next_ok_ts"] == next_ok_at_dispatch


def test_consecutive_submissions_on_same_account_respect_minimum_spacing():
    """gap 的本意是同一账号两次**提交**之间的间隔 —— 该性质必须仍然成立。"""
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        first = service._acquire_next_candidate_token()
        first_submit_at = clock.now
        clock.advance(10.0)
        service.mark_image_result(first, success=True)

        # 距上次提交仅 10s < gap → 取不到号
        clock.advance(GAP - 10.0 - 1.0)
        assert service._list_ready_candidate_tokens() == []

        clock.advance(1.0)
        second = service._acquire_next_candidate_token()
        assert clock.now - first_submit_at >= GAP
        assert service.get_account(second)["image_next_ok_ts"] == clock.now + GAP


def test_second_concurrent_submission_cannot_bypass_the_interval_gate():
    """占位与打戳在同一临界区 → 第二路并发提交必须被间隔闸门挡住（无 TOCTOU）。

    放线程里跑：pre-fix 代码此处会**永久挂死** —— ready 非空（间隔戳还没打）而
    available 为空（旧 binding 闸门拒绝），`_acquire_next_candidate_token` 落进
    `while True: wait(timeout=1.0)`，没有 deadline 检查。
    """
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        first = service._acquire_next_candidate_token()
        assert service._image_inflight[first] == 1

        outcome: dict[str, object] = {}

        def second_attempt() -> None:
            try:
                outcome["token"] = service._acquire_next_candidate_token()
            except RuntimeError as exc:
                outcome["error"] = str(exc)

        thread = threading.Thread(target=second_attempt, name="second-submit", daemon=True)
        thread.start()
        thread.join(timeout=10.0)

        assert not thread.is_alive(), "second acquire hung instead of being refused"
        assert "token" not in outcome, "interval gate let a concurrent submission through"
        assert "no available" in str(outcome.get("error", ""))
        # 第一路仍在途，槽位没有被偷走
        assert service._image_inflight[first] == 1


def test_racing_threads_yield_exactly_one_dispatch():
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        winners: list[str] = []
        errors: list[str] = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait(timeout=5.0)
            try:
                token = service._acquire_next_candidate_token()
            except RuntimeError as exc:
                with lock:
                    errors.append(str(exc))
            else:
                with lock:
                    winners.append(token)

        threads = [threading.Thread(target=attempt, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
            assert not thread.is_alive()

        assert len(winners) == 1, f"expected exactly one dispatch, got {winners}"
        assert len(errors) == 1
        assert service._image_inflight[winners[0]] == 1


def test_ensure_stamp_backfills_only_when_never_dispatched():
    """外部直接调 mark_image_result（没走过取号占位）时保留旧的完成时打戳行为。"""
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock):
        updated = service.mark_image_result("tok-a", success=True)

        assert updated["image_next_ok_ts"] == clock.now + GAP
        assert "image_last_dispatch_ts" not in updated


def test_ensure_stamp_is_a_noop_after_a_dispatch_stamp():
    clock = FakeClock()
    service = AccountService.__new__(AccountService)
    with scheduling_env(clock):
        account = {"image_next_ok_ts": 111.0, "image_last_dispatch_ts": 42.0}
        assert service._ensure_image_next_ok_stamped(dict(account))["image_next_ok_ts"] == 111.0

        never = {"image_next_ok_ts": 111.0}
        assert service._ensure_image_next_ok_stamped(dict(never))["image_next_ok_ts"] == clock.now + GAP


def test_dispatch_stamp_is_inert_when_scheduler_disabled():
    """scheduler 关闭时不打间隔戳（否则等于偷偷开启限速），但派发标记仍要落。"""
    clock = FakeClock()
    with service_with(_account()) as service, scheduling_env(clock, enabled=False):
        token = service._acquire_next_candidate_token()
        account = service.get_account(token)

        assert "image_next_ok_ts" not in account
        assert account["image_last_dispatch_ts"] == clock.now
        assert service._is_image_interval_ready(account) is True


def test_dispatch_stamp_ignores_unknown_token():
    service = AccountService.__new__(AccountService)
    service._accounts = {}
    assert service._stamp_image_dispatch_locked("") is None
    assert service._stamp_image_dispatch_locked("nope") is None


# =========================================================================== #
# A4-3  binding 闸门：按账号席位数计，且排除自己
# =========================================================================== #

BINDING = "binding-shared"


def _slot_service(inflight: dict[str, int], bindings: dict[str, str]):
    service = AccountService.__new__(AccountService)
    service._lock = RLock()
    service._image_slot_condition = Condition(service._lock)
    service._image_inflight = dict(inflight)
    service._accounts = {
        token: {"access_token": token, "email": f"{token}@example.com", "proxy_binding_hash": binding}
        for token, binding in bindings.items()
    }
    return service


def test_per_account_concurrency_of_two_is_reachable():
    """A4-3 主回归：独占 binding 的账号已有 1 路在途时，第 2 路必须放行。"""
    service = _slot_service({"tok-a": 1}, {"tok-a": BINDING})

    with config_data(image_account_concurrency=2, image_binding_inflight_max=1):
        assert service._image_slot_available_locked("tok-a", skip_global_limit=True) is True


def test_account_concurrency_ceiling_is_still_enforced():
    service = _slot_service({"tok-a": 2}, {"tok-a": BINDING})

    with config_data(image_account_concurrency=2, image_binding_inflight_max=1):
        assert service._image_slot_available_locked("tok-a", skip_global_limit=True) is False


def test_binding_limit_still_caps_shared_egress_exposure():
    """同 binding 的第二个账号在第一个还有在途时必须被挡住（CF 共享出口暴露）。"""
    service = _slot_service({"tok-a": 1}, {"tok-a": BINDING, "tok-b": BINDING})

    with config_data(image_account_concurrency=2, image_binding_inflight_max=1):
        assert service._image_slot_available_locked("tok-b", skip_global_limit=True) is False
        # 出口空下来之后立刻放行
        service._image_inflight.pop("tok-a")
        assert service._image_slot_available_locked("tok-b", skip_global_limit=True) is True


def test_binding_limit_two_admits_two_distinct_accounts():
    service = _slot_service({"tok-a": 1}, {"tok-a": BINDING, "tok-b": BINDING})

    with config_data(image_account_concurrency=2, image_binding_inflight_max=2):
        assert service._image_slot_available_locked("tok-b", skip_global_limit=True) is True
        service._image_inflight["tok-b"] = 1
        # 两个席位已满 → 第三个账号被挡
        service._accounts["tok-c"] = {"access_token": "tok-c", "proxy_binding_hash": BINDING}
        assert service._image_slot_available_locked("tok-c", skip_global_limit=True) is False


def test_other_bindings_do_not_interfere():
    service = _slot_service(
        {"tok-a": 1}, {"tok-a": BINDING, "tok-b": "binding-other"}
    )

    with config_data(image_account_concurrency=2, image_binding_inflight_max=1):
        assert service._image_slot_available_locked("tok-b", skip_global_limit=True) is True


def test_account_without_binding_is_unconstrained_by_binding_gate():
    service = _slot_service({"tok-a": 1}, {"tok-a": ""})

    with config_data(image_account_concurrency=2, image_binding_inflight_max=1):
        assert service._image_slot_available_locked("tok-a", skip_global_limit=True) is True


def test_binding_seat_counter_excludes_self_and_counts_accounts_not_requests():
    service = _slot_service(
        {"tok-a": 2, "tok-b": 3}, {"tok-a": BINDING, "tok-b": BINDING}
    )

    # 请求口径：2 + 3 = 5（仅供观测）
    assert service._binding_image_inflight_locked(BINDING) == 5
    # 席位口径：两个账号 = 2；排除自己后 = 1
    assert service._binding_image_account_seats_locked(BINDING) == 2
    assert service._binding_image_account_seats_locked(BINDING, exclude_token="tok-a") == 1
    assert service._binding_image_account_seats_locked("") == 0
    # 零在途的账号不占席位
    service._image_inflight["tok-b"] = 0
    assert service._binding_image_account_seats_locked(BINDING) == 1


def test_list_available_candidate_tokens_uses_the_same_binding_semantics():
    """两处 binding 闸门是复制粘贴关系，口径必须一致，否则取号与准入互相打脸。"""
    clock = FakeClock()
    with service_with(_account("tok-a"), _account("tok-b")) as service, scheduling_env(
        clock
    ), config_data(image_account_concurrency=2, image_binding_inflight_max=1):
        # 直接钉在内存态上：add_account_items() 会按 proxy 重算 proxy_binding_hash
        # （无 proxy → 空），而给它们配真 proxy 又会牵进 CF 资格闸门，与本条无关。
        for token in ("tok-a", "tok-b"):
            service._accounts[token]["proxy_binding_hash"] = BINDING

        assert sorted(service._list_ready_candidate_tokens()) == ["tok-a", "tok-b"]

        with service._image_slot_condition:
            service._image_inflight["tok-a"] = 1

        # tok-a 未打间隔戳（这里直接改的在途表），席位闸门应只放它自己
        available = service._list_available_candidate_tokens()
        assert "tok-b" not in available, "binding seat still occupied by tok-a"
        assert "tok-a" in available, "self must not consume an extra binding seat"
