"""Regression tests for audit A2 — CF probe blocking starved the dispatchable pool.

A2-1  封禁时长从 86400s 降到分钟级 + 阶梯退避；streak 加滑动窗口，不再无限累积。
A2-2  被封号被主动限速复探，探活成功即解封，自愈不再只靠墙钟到期 / 进程重启。
A2-3  运维手动解封（单个 + 批量），且明确不跨重启保留。

时间全部由注入时钟驱动，测试内不使用真实 sleep。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from services.account_warmup_service import AccountWarmupService
from services.config import _normalize_account_warmup_settings

# audit A2 实测：fail 20/6h ÷ cf_fail_max_streak 2 ≈ 1.67 个封禁/小时，全池 19 个账号。
MEASURED_BLOCKS_PER_HOUR = 1.67
POOL_SIZE = 19
LEGACY_BLOCK_SEC = 86400.0


class _FakeClock:
    """可推进的假时钟，替代 time.time()。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _make_settings(**overrides: Any) -> dict[str, Any]:
    settings = dict(_normalize_account_warmup_settings({}))
    settings["startup_delay_sec"] = 0.0
    settings["max_hot"] = 3
    settings.update(overrides)
    return settings


def _service(clock: _FakeClock) -> AccountWarmupService:
    return AccountWarmupService(clock=clock)


def _account(email: str, token: str = "tok") -> dict[str, Any]:
    return {"email": email, "access_token": token, "status": "正常"}


def _run_guarded(fn: Callable[[], Any], *, timeout: float = 10.0) -> Any:
    """在子线程里跑可能阻塞的调用；超时即视为挂死。"""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样回抛给主线程断言
            box["error"] = exc

    thread = threading.Thread(target=target, name="warmup-under-test", daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "warmup call did not finish within timeout"
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _probe(
    svc: AccountWarmupService,
    settings: dict[str, Any],
    email: str,
    *,
    fail: bool = True,
    force: bool = False,
) -> bool:
    backend = MagicMock()
    if fail:
        backend._get_chat_requirements_once.side_effect = RuntimeError("cf_edge_block")
    with patch("services.account_warmup_service.OpenAIBackendAPI", return_value=backend), patch(
        "services.account_warmup_service.account_service.record_cf_sample", return_value=True
    ):
        return bool(
            _run_guarded(lambda: svc._warmup_one(_account(email), settings, force=force))
        )


def _tick(
    svc: AccountWarmupService,
    settings: dict[str, Any],
    accounts: list[dict[str, Any]],
    *,
    fail: bool = True,
) -> None:
    backend = MagicMock()
    if fail:
        backend._get_chat_requirements_once.side_effect = RuntimeError("cf_edge_block")
    with patch("services.account_warmup_service.OpenAIBackendAPI", return_value=backend), patch(
        "services.account_warmup_service.account_service"
    ) as acc:
        acc.list_accounts.return_value = list(accounts)
        acc._is_image_account_schedulable.return_value = True
        acc.record_cf_sample.return_value = True
        _run_guarded(lambda: svc._tick(settings))


def _block_via_two_fails(
    svc: AccountWarmupService,
    settings: dict[str, Any],
    clock: _FakeClock,
    email: str,
    *,
    gap: float = 30.0,
) -> None:
    assert _probe(svc, settings, email) is False
    clock.advance(gap)
    assert _probe(svc, settings, email) is False
    assert email in svc.status()["blocked_until"]


# ---------------------------------------------------------------- A2-1 时长


def test_default_block_duration_is_minutes_scale_not_86400():
    out = _normalize_account_warmup_settings({})
    assert float(out["cf_block_sec"]) == 600.0
    assert float(out["cf_block_max_sec"]) == 3600.0
    # 上限也必须远低于旧的 24h
    assert float(out["cf_block_max_sec"]) < LEGACY_BLOCK_SEC / 10.0


def test_applied_block_window_is_minutes_scale():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    _block_via_two_fails(svc, settings, clock, "bad@example.com")
    blocked_until = svc.status()["blocked_until"]["bad@example.com"]
    assert blocked_until - clock.now == pytest.approx(600.0)
    assert blocked_until - clock.now < LEGACY_BLOCK_SEC


def test_legacy_persisted_86400_is_clamped_down():
    """向后兼容：老配置文件里持久化的 24h 会被自动压到新上限，无需人工改配置。"""
    out = _normalize_account_warmup_settings({"cf_block_sec": LEGACY_BLOCK_SEC})
    assert float(out["cf_block_sec"]) == 3600.0
    assert float(out["cf_block_sec"]) < LEGACY_BLOCK_SEC


def test_block_ceiling_keeps_inflight_blocked_far_below_pool():
    """Little's Law：在途封禁 = 到达率 × 封禁时长。旧 24h 的不动点是可调度归零。"""
    out = _normalize_account_warmup_settings({})
    legacy_inflight = MEASURED_BLOCKS_PER_HOUR * (LEGACY_BLOCK_SEC / 3600.0)
    assert legacy_inflight > POOL_SIZE  # 40.1 > 19 → 池子被封空

    base_inflight = MEASURED_BLOCKS_PER_HOUR * (float(out["cf_block_sec"]) / 3600.0)
    worst_inflight = MEASURED_BLOCKS_PER_HOUR * (float(out["cf_block_max_sec"]) / 3600.0)
    assert base_inflight < POOL_SIZE * 0.02  # 0.28 个 ≈ 1.5% 池子
    assert worst_inflight < POOL_SIZE * 0.10  # 1.67 个 ≈ 8.8% 池子


def _expire_block(
    svc: AccountWarmupService,
    settings: dict[str, Any],
    clock: _FakeClock,
    email: str,
) -> None:
    """推进到墙钟到期并跑一次 tick 走过期清扫（保留阶梯退避记忆）。"""
    blocked_until = svc.status()["blocked_until"][email]
    clock.now = blocked_until + 1.0
    _tick(svc, settings, [], fail=True)
    assert email not in svc.status()["blocked_until"]


def test_repeat_offender_escalates_but_stays_under_ceiling():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "repeat@example.com"

    _block_via_two_fails(svc, settings, clock, email)
    first = svc.status()["blocked_until"][email] - clock.now
    assert first == pytest.approx(600.0)

    # 墙钟到期后再犯 → 退避记忆仍在衰减窗口内 → 时长翻倍
    _expire_block(svc, settings, clock, email)
    _block_via_two_fails(svc, settings, clock, email)
    second = svc.status()["blocked_until"][email] - clock.now
    assert second == pytest.approx(1200.0)
    assert second > first

    durations = [first, second]
    for _ in range(8):
        _expire_block(svc, settings, clock, email)
        _block_via_two_fails(svc, settings, clock, email)
        durations.append(svc.status()["blocked_until"][email] - clock.now)

    # 退避自限：封禁越长，两次封禁的间隔也越长，旧封禁事件会自己滑出衰减窗口，
    # 于是时长在 base..ceiling 之间收敛到平衡点，不会单调爬到上限并钉死在那里。
    assert all(float(settings["cf_block_sec"]) <= d <= float(settings["cf_block_max_sec"]) for d in durations)
    assert max(durations) < LEGACY_BLOCK_SEC / 10.0
    # 即便按最坏时长算，在途封禁也远小于池子
    assert MEASURED_BLOCKS_PER_HOUR * (max(durations) / 3600.0) < POOL_SIZE * 0.10


def test_escalation_memory_decays_after_clean_period():
    """清白超过衰减窗口（= cf_block_max_sec）后退回基准时长，避免一生单调累积。"""
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "aged@example.com"

    _block_via_two_fails(svc, settings, clock, email)
    _expire_block(svc, settings, clock, email)
    _block_via_two_fails(svc, settings, clock, email)
    assert svc.status()["blocked_until"][email] - clock.now == pytest.approx(1200.0)

    _expire_block(svc, settings, clock, email)
    clock.advance(float(settings["cf_block_max_sec"]) + 60.0)
    _tick(svc, settings, [], fail=True)
    _block_via_two_fails(svc, settings, clock, email)
    assert svc.status()["blocked_until"][email] - clock.now == pytest.approx(600.0)


# ---------------------------------------------------------- A2-1 streak 窗口


def test_failures_outside_window_do_not_compound_into_block():
    """A2-1 回归：相隔超过 cf_fail_window_sec 的两次失败不得累积成封禁。

    旧实现 `streak = self._cf_fail_streak[email] + 1` 无窗口无衰减，此断言必然失败。
    """
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_fail_probe_cooldown_sec=0.0)
    email = "sparse@example.com"

    assert _probe(svc, settings, email) is False
    assert svc.status()["cf_fail_streak"].get(email) == 1
    assert email not in svc.status()["blocked_until"]

    clock.advance(float(settings["cf_fail_window_sec"]) + 1.0)
    assert _probe(svc, settings, email) is False

    status = svc.status()
    assert status["cf_fail_streak"].get(email) == 1, "窗口外的旧失败不应再计入 streak"
    assert email not in status["blocked_until"]
    assert svc.is_dispatch_blocked(email) is False


def test_failures_inside_window_still_block():
    """保留原意：真正连续的失败照旧封禁。"""
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_fail_probe_cooldown_sec=0.0)
    email = "burst@example.com"

    assert _probe(svc, settings, email) is False
    clock.advance(float(settings["cf_fail_window_sec"]) / 2.0)
    assert _probe(svc, settings, email) is False

    status = svc.status()
    assert status["cf_fail_streak"].get(email) == 2
    assert email in status["blocked_until"]
    assert svc.is_dispatch_blocked(email) is True


def test_tick_decays_stale_streak_without_any_probe():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "decay@example.com"
    accounts = [_account(email)]

    assert _probe(svc, settings, email) is False
    assert svc.status()["cf_fail_streak"].get(email) == 1

    clock.advance(float(settings["cf_fail_window_sec"]) + 1.0)
    _tick(svc, settings, accounts, fail=False)
    assert email not in svc.status()["cf_fail_streak"]


def test_success_resets_streak():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_fail_probe_cooldown_sec=0.0)
    email = "flaky@example.com"

    assert _probe(svc, settings, email) is False
    assert svc.status()["cf_fail_streak"].get(email) == 1

    clock.advance(10.0)
    assert _probe(svc, settings, email, fail=False) is True
    assert email not in svc.status()["cf_fail_streak"]

    # 归零之后单次失败不得直接触发封禁
    clock.advance(10.0)
    assert _probe(svc, settings, email) is False
    status = svc.status()
    assert status["cf_fail_streak"].get(email) == 1
    assert email not in status["blocked_until"]


# --------------------------------------------------------------- A2-2 自愈


def test_blocked_account_is_reprobed_and_recovers_without_restart():
    """A2-2 回归：被封号必须被主动复探，成功即解封。

    旧实现 `_warmup_one` 对已封号早退，永远走不到解封分支；且 `_tick` 无复探路径，
    只能等墙钟到期或进程重启，此断言必然失败。
    """
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "recover@example.com"
    accounts = [_account(email)]

    _block_via_two_fails(svc, settings, clock, email)
    blocked_until = svc.status()["blocked_until"][email]
    assert svc.is_dispatch_blocked(email) is True

    clock.advance(float(settings["cf_reprobe_interval_sec"]))
    assert clock.now < blocked_until, "复探必须发生在墙钟到期之前，否则不算主动自愈"

    _tick(svc, settings, accounts, fail=False)

    status = svc.status()
    assert email not in status["blocked_until"]
    assert email not in status["cf_fail_streak"]
    assert svc.is_dispatch_blocked(email) is False
    assert status["totals"]["reprobed"] >= 1
    assert status["totals"]["recovered"] == 1


def test_reprobe_is_rate_limited_across_ticks():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "paced@example.com"
    accounts = [_account(email)]

    _block_via_two_fails(svc, settings, clock, email)

    # interval_sec=60 的 4 个 tick 都早于 cf_reprobe_interval_sec=300 → 一次都不复探
    for _ in range(4):
        clock.advance(60.0)
        _tick(svc, settings, accounts, fail=True)
    assert svc.status()["totals"]["reprobed"] == 0
    assert svc.status()["totals"]["ticks"] == 4

    clock.advance(60.0)
    _tick(svc, settings, accounts, fail=True)
    assert svc.status()["totals"]["reprobed"] == 1


def test_reprobe_respects_max_per_tick():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_reprobe_max_per_tick=2)
    emails = ["a@example.com", "b@example.com", "c@example.com"]
    accounts = [_account(e, token=f"tok-{e}") for e in emails]

    for email in emails:
        _block_via_two_fails(svc, settings, clock, email, gap=1.0)

    clock.advance(float(settings["cf_reprobe_interval_sec"]))
    _tick(svc, settings, accounts, fail=True)
    assert svc.status()["totals"]["reprobed"] == 2, "每 tick 复探数必须受 cf_reprobe_max_per_tick 限制"


def test_dead_account_reprobe_does_not_hot_loop():
    """永久坏号：复探被限速 + 阶梯退避，一小时内复探次数有界。"""
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "dead@example.com"
    accounts = [_account(email)]

    _block_via_two_fails(svc, settings, clock, email)
    for _ in range(60):  # 60 × 60s = 1h
        clock.advance(60.0)
        _tick(svc, settings, accounts, fail=True)

    totals = svc.status()["totals"]
    assert totals["ticks"] == 60
    ceiling = 3600.0 / float(settings["cf_reprobe_interval_sec"])
    assert 1 <= totals["reprobed"] <= ceiling
    assert email in svc.status()["blocked_until"], "坏号复探失败后应继续保持封禁"


def test_reprobe_disabled_when_max_per_tick_is_zero():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_reprobe_max_per_tick=0)
    email = "noreprobe@example.com"
    accounts = [_account(email)]

    _block_via_two_fails(svc, settings, clock, email)
    blocked_until = svc.status()["blocked_until"][email]
    clock.advance(float(settings["cf_reprobe_interval_sec"]))
    assert clock.now < blocked_until
    _tick(svc, settings, accounts, fail=False)
    assert svc.status()["totals"]["reprobed"] == 0
    assert email in svc.status()["blocked_until"], "关掉复探后应退化为纯墙钟到期"


def test_probe_cooldown_never_blocks_dispatch():
    """探活冷却只压探活频率（缓解 max_hot 常年 > 存活数的压力），不得影响派发。"""
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_fail_max_streak=5)
    email = "cooldown@example.com"
    accounts = [_account(email)]

    assert _probe(svc, settings, email) is False
    status = svc.status()
    assert email in status["probe_paused_until"]
    assert email not in status["blocked_until"]
    assert svc.is_dispatch_blocked(email) is False

    # 冷却期内 tick 不再探活该号
    clock.advance(float(settings["cf_fail_probe_cooldown_sec"]) / 2.0)
    _tick(svc, settings, accounts, fail=True)
    assert svc.status()["cf_fail_streak"].get(email) == 1

    # 冷却到期后恢复探活
    clock.advance(float(settings["cf_fail_probe_cooldown_sec"]))
    _tick(svc, settings, accounts, fail=True)
    assert svc.status()["cf_fail_streak"].get(email) == 2


# ------------------------------------------------------------ A2-3 运维解封


def test_clear_block_makes_account_dispatchable_again():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "unblock@example.com"

    _block_via_two_fails(svc, settings, clock, email)
    assert svc.is_dispatch_blocked(email) is True

    result = svc.clear_block(email, reason="test")
    assert result["ok"] is True
    assert result["cleared"] is True
    assert result["was_blocked"] is True
    assert result["persistent"] is False
    assert "restart" in result["note"]

    status = svc.status()
    assert email not in status["blocked_until"]
    assert email not in status["cf_fail_streak"]
    assert email not in status["probe_paused_until"]
    assert svc.is_dispatch_blocked(email) is False
    assert status["totals"]["unblocked"] == 1
    assert status["block_state_persistent"] is False


def test_clear_block_also_clears_demote_cooldown():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "hot@example.com"
    with svc._lock:
        svc._hot[email] = clock.now
    svc.demote(email, reason="test")
    assert svc.is_dispatch_blocked(email) is True

    result = svc.clear_block(email)
    assert result["was_demoted"] is True
    assert svc.is_dispatch_blocked(email) is False


def test_clear_block_resets_escalation_memory_to_clean_slate():
    """运维手动解封语义 = 完全清白：下次封禁回到基准时长，而非继续翻倍。"""
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    email = "slate@example.com"

    _block_via_two_fails(svc, settings, clock, email)
    svc.clear_block(email, reason="test")
    assert email not in svc.status()["cf_block_repeats"]

    clock.advance(60.0)
    _block_via_two_fails(svc, settings, clock, email)
    assert svc.status()["blocked_until"][email] - clock.now == pytest.approx(600.0)


def test_clear_block_is_idempotent_and_validates_email():
    clock = _FakeClock()
    svc = _service(clock)
    result = svc.clear_block("nobody@example.com")
    assert result["ok"] is True
    assert result["cleared"] is False
    with pytest.raises(ValueError):
        svc.clear_block("   ")


def test_clear_all_blocks_bulk_variant():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    emails = ["x@example.com", "y@example.com"]
    for email in emails:
        _block_via_two_fails(svc, settings, clock, email, gap=1.0)
    assert all(svc.is_dispatch_blocked(e) for e in emails)

    result = svc.clear_all_blocks(reason="test")
    assert result["ok"] is True
    assert result["unblocked_count"] == 2
    assert sorted(result["unblocked"]) == sorted(emails)
    assert result["persistent"] is False
    assert not any(svc.is_dispatch_blocked(e) for e in emails)
    assert svc.status()["totals"]["unblocked"] == 2


def test_clear_all_blocks_also_resets_pending_streaks():
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings()
    pending = "pending@example.com"

    assert _probe(svc, settings, pending) is False  # streak 1，尚未成封
    assert svc.status()["cf_fail_streak"].get(pending) == 1

    svc.clear_all_blocks(reason="test")
    status = svc.status()
    assert pending not in status["cf_fail_streak"]
    assert pending not in status["probe_paused_until"]


def test_reprobe_budget_not_wasted_on_offline_accounts():
    """已掉出候选池的封禁号不得吃掉每 tick 的复探配额。"""
    clock = _FakeClock()
    svc = _service(clock)
    settings = _make_settings(cf_reprobe_max_per_tick=1)
    offline = "offline@example.com"
    alive = "alive@example.com"

    # offline 先被封（reprobe_at 更早，排序上优先），但它已不在候选池里
    _block_via_two_fails(svc, settings, clock, offline, gap=1.0)
    clock.advance(5.0)
    _block_via_two_fails(svc, settings, clock, alive, gap=1.0)

    clock.advance(float(settings["cf_reprobe_interval_sec"]))
    _tick(svc, settings, [_account(alive)], fail=False)

    status = svc.status()
    assert status["totals"]["reprobed"] == 1
    assert alive not in status["blocked_until"], "在池的封禁号应拿到复探配额并解封"
    assert offline in status["blocked_until"]


def test_ops_router_exposes_warmup_unblock_routes():
    from api.ops import create_router

    paths = {route.path: sorted(route.methods) for route in create_router().routes}
    assert paths.get("/api/ops/warmup/unblock") == ["POST"]
    assert paths.get("/api/ops/warmup/unblock-all") == ["POST"]
    # 只读 status 端点必须保留
    assert "GET" in paths.get("/api/ops/warmup/status", [])
