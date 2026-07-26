"""A3-1 `reconcile_inflight` 语义回归：漂移 key 不碰撞、纠正后唤醒等待者、白名单收窄写。

背景：docs/28-scheduling-queue-slot-audit-20260726.md §A4 —— watchdog 通电后
`reconcile_inflight` 才第一次真正拥有纠正权，于是三个此前无所谓的缺陷开始有后果：

1. drift key 用 `token[:12] + "..."`，生产 access token 是 JWT，全池共享
   `eyJhbGciOiJS...` 前缀 → 所有账号塌成一个 key，drift_count 系统性少报。
2. 纠正分支在持 `_image_slot_condition` 时改了 `_image_inflight` 却不 notify，
   取号线程要等下一个无关事件才醒 → 槽位等于白回收。
3. `force=True` 是全池「一刀切」，调用方无法只纠正已逐个确认过的 stale 子集。
"""

from __future__ import annotations

import os
import threading
from threading import Condition, RLock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")
os.environ.setdefault("STORAGE_BACKEND", "json")

from services.account_service import AccountService, inflight_token_fingerprint

# 真实 access token 是 JWT：header 段 base64 后完全相同，前 12 字符必然撞。
TOKEN_A = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.account-a.sig"
TOKEN_B = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.account-b.sig"


def _svc(inflight: dict[str, int] | None = None, emails: dict[str, str] | None = None):
    """裸 AccountService，只装配锁与在途表。

    关键：`_image_slot_condition` 必须 `Condition(self._lock)`，与 __init__ 的
    接线一致，否则唤醒语义测不准。
    """
    service = AccountService.__new__(AccountService)
    service._lock = RLock()
    service._image_slot_condition = Condition(service._lock)
    service._image_inflight = dict(inflight or {})
    service._accounts = {
        token: {"access_token": token, "email": email}
        for token, email in (emails or {}).items()
    }
    return service


# --------------------------------------------------------------------------- #
# 1. drift key 不再跨 JWT 碰撞
# --------------------------------------------------------------------------- #

def test_two_jwt_tokens_produce_two_distinct_drift_entries():
    service = _svc({TOKEN_A: 2, TOKEN_B: 3})

    result = service.reconcile_inflight(expected_by_token={}, force=False)

    # 前缀照样撞 —— 只是不再被当作身份
    assert TOKEN_A[:12] == TOKEN_B[:12]
    assert result["drift_count"] == 2
    assert len(result["drift"]) == 2
    keys = set(result["drift"])
    assert keys == {
        inflight_token_fingerprint(TOKEN_A),
        inflight_token_fingerprint(TOKEN_B),
    }
    memories = sorted(entry["memory"] for entry in result["drift"].values())
    assert memories == [2, 3]


def test_drift_keys_do_not_leak_raw_tokens():
    """保留原截断想要的匿名性：指纹不可逆，且不含 token 任何原文片段。"""
    service = _svc({TOKEN_A: 1})

    result = service.reconcile_inflight(expected_by_token={}, force=False)

    key = next(iter(result["drift"]))
    assert TOKEN_A not in key
    assert "account-a" not in key
    assert key == inflight_token_fingerprint(TOKEN_A)
    # blake2b digest_size=6 → 12 位十六进制，与 pipeline_watchdog 同口径
    assert len(key) == 12
    assert all(c in "0123456789abcdef" for c in key)


def test_drift_fingerprint_is_stable_and_distinct():
    fp = inflight_token_fingerprint
    assert fp(TOKEN_A) == fp(TOKEN_A)
    assert fp(TOKEN_A) != fp(TOKEN_B)
    assert fp("") == fp(None)


def test_drift_entry_carries_email_for_attribution():
    service = _svc({TOKEN_A: 4}, emails={TOKEN_A: "leak@example.com"})

    result = service.reconcile_inflight(expected_by_token={TOKEN_A: 1}, force=False)

    entry = result["drift"][inflight_token_fingerprint(TOKEN_A)]
    assert entry == {"memory": 4, "expected": 1, "email": "leak@example.com"}


def test_expected_only_token_is_still_reported():
    """expected 有、memory 没有（欠计）也必须上报，方向相反但同样是漂移。"""
    service = _svc({})

    result = service.reconcile_inflight(expected_by_token={TOKEN_A: 2}, force=False)

    entry = result["drift"][inflight_token_fingerprint(TOKEN_A)]
    assert entry["memory"] == 0
    assert entry["expected"] == 2
    assert result["corrected"] == 0


# --------------------------------------------------------------------------- #
# 2. 纠正后必须唤醒等待槽位的线程
# --------------------------------------------------------------------------- #

def _park_waiter(service, *, wait_timeout: float):
    """起一个线程，parked 在 _image_slot_condition.wait() 上。

    `started` 在**持锁**时置位，因此主线程随后调用 reconcile_inflight 时必然要等
    waiter 进入 wait() 释放锁之后才能拿到锁 —— 不存在 lost wakeup 竞态。
    """
    started = threading.Event()
    woke = threading.Event()

    def waiter() -> None:
        with service._image_slot_condition:
            started.set()
            service._image_slot_condition.wait(timeout=wait_timeout)
        woke.set()

    thread = threading.Thread(target=waiter, name="slot-waiter", daemon=True)
    thread.start()
    assert started.wait(timeout=5.0), "waiter thread never started"
    return thread, woke


def test_correction_wakes_thread_waiting_for_an_image_slot():
    service = _svc({TOKEN_A: 2})
    # 长 wait 超时：只有 notify_all() 能让它快速返回，超时兜底不会造成假通过
    thread, woke = _park_waiter(service, wait_timeout=30.0)

    result = service.reconcile_inflight(expected_by_token={}, force=True)

    assert result["corrected"] == 1
    assert TOKEN_A not in service._image_inflight
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "corrected a slot but never notified the waiters"
    assert woke.is_set()


def test_partial_correction_to_nonzero_expected_also_wakes_waiters():
    service = _svc({TOKEN_A: 5})
    thread, woke = _park_waiter(service, wait_timeout=30.0)

    result = service.reconcile_inflight(expected_by_token={TOKEN_A: 2}, force=True)

    assert result["corrected"] == 1
    assert service._image_inflight[TOKEN_A] == 2
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert woke.is_set()


def test_no_correction_does_not_notify():
    """notify 只在真的纠正过时发生，不是无条件唤醒（避免惊群空转）。"""
    service = _svc({TOKEN_A: 2})
    thread, _woke = _park_waiter(service, wait_timeout=1.5)

    # 观测模式：有漂移但没有纠正权
    result = service.reconcile_inflight(expected_by_token={}, force=False)

    assert result["drift_count"] == 1
    assert result["corrected"] == 0
    thread.join(timeout=0.2)
    assert thread.is_alive(), "observation-only tick must not wake slot waiters"
    thread.join(timeout=5.0)


def test_force_without_drift_does_not_notify():
    service = _svc({TOKEN_A: 2})
    thread, _woke = _park_waiter(service, wait_timeout=1.5)

    result = service.reconcile_inflight(expected_by_token={TOKEN_A: 2}, force=True)

    assert result["drift_count"] == 0
    assert result["corrected"] == 0
    thread.join(timeout=0.2)
    assert thread.is_alive()
    thread.join(timeout=5.0)


def test_under_count_is_never_corrected():
    """memory < expected 只上报不改：凭空加在途会造成超卖。"""
    service = _svc({TOKEN_A: 1})

    result = service.reconcile_inflight(expected_by_token={TOKEN_A: 4}, force=True)

    assert result["drift_count"] == 1
    assert result["corrected"] == 0
    assert service._image_inflight[TOKEN_A] == 1


# --------------------------------------------------------------------------- #
# 3. tokens= 白名单：只收窄写，不收窄看
# --------------------------------------------------------------------------- #

def test_tokens_allow_list_corrects_only_the_named_subset():
    service = _svc({TOKEN_A: 3, TOKEN_B: 4})

    result = service.reconcile_inflight(
        expected_by_token={}, force=True, tokens=[TOKEN_A]
    )

    assert result["corrected"] == 1
    assert TOKEN_A not in service._image_inflight
    assert service._image_inflight[TOKEN_B] == 4
    # 观测面不受白名单影响，否则 /health 会跟着瞎掉
    assert result["drift_count"] == 2


def test_tokens_omitted_keeps_all_tokens_behaviour():
    service = _svc({TOKEN_A: 3, TOKEN_B: 4})

    result = service.reconcile_inflight(expected_by_token={}, force=True)

    assert result["corrected"] == 2
    assert service._image_inflight == {}


def test_empty_tokens_allow_list_corrects_nothing_but_still_reports():
    service = _svc({TOKEN_A: 3, TOKEN_B: 4})

    result = service.reconcile_inflight(expected_by_token={}, force=True, tokens=[])

    assert result["corrected"] == 0
    assert service._image_inflight == {TOKEN_A: 3, TOKEN_B: 4}
    assert result["drift_count"] == 2


def test_tokens_allow_list_accepts_set_and_tuple():
    for allow in ({TOKEN_B}, (TOKEN_B,)):
        service = _svc({TOKEN_A: 3, TOKEN_B: 4})
        result = service.reconcile_inflight(
            expected_by_token={}, force=True, tokens=allow
        )
        assert result["corrected"] == 1
        assert service._image_inflight == {TOKEN_A: 3}


def test_tokens_allow_list_ignores_unknown_tokens():
    service = _svc({TOKEN_A: 3})

    result = service.reconcile_inflight(
        expected_by_token={}, force=True, tokens=["not-in-pool"]
    )

    assert result["corrected"] == 0
    assert service._image_inflight == {TOKEN_A: 3}


def test_allow_listed_correction_still_notifies():
    service = _svc({TOKEN_A: 2, TOKEN_B: 2})
    thread, woke = _park_waiter(service, wait_timeout=30.0)

    result = service.reconcile_inflight(
        expected_by_token={}, force=True, tokens=[TOKEN_A]
    )

    assert result["corrected"] == 1
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert woke.is_set()


# --------------------------------------------------------------------------- #
# totals 口径
# --------------------------------------------------------------------------- #

def test_totals_reflect_state_after_correction():
    service = _svc({TOKEN_A: 3, TOKEN_B: 2})

    result = service.reconcile_inflight(
        expected_by_token={TOKEN_A: 1, TOKEN_B: 2}, force=True
    )

    assert result["corrected"] == 1
    # total_memory 在纠正之后采样，反映纠正后的真实水位
    assert result["total_memory"] == 3
    assert result["total_expected"] == 3
