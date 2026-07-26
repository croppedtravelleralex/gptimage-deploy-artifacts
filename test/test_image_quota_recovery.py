"""A2-4 / A2-5 回归：额度归零不得写死 status=限流（单向门），存量沉底账号可捞回。

背景：docs/28-scheduling-queue-slot-audit-20260726.md §A3。
`mark_image_result` 曾在 quota 归零时打 status=限流 并落库，而
`_quota_window_due_for_lazy_refresh()` 第一条要求 status==正常，
逃生口被自己关死 → 账号耗尽一次即永久退池。
本文件同时锁住反向风险：额度窗口未到之前**不得**放回派发面。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")
# 让 account_service 单例落到无副作用的 JSON backend，避免 import 打开生产 accounts.db
os.environ.setdefault("STORAGE_BACKEND", "json")

from scripts import recover_stranded_image_quota as recovery
from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _past(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _future(**kwargs) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat()


def _no_jitter():
    """lazy_refresh_jitter_hours=0：让 restore_at 直接等于唤醒时刻，断言可确定。"""
    return patch(
        "services.account_service.config.get_scheduler_settings",
        return_value={"lazy_refresh_jitter_hours": 0, "unrestricted": False},
    )


def _service(tmp_dir: str) -> AccountService:
    return AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))


def _metered_account(**overrides) -> dict:
    base = {
        "access_token": "tok-metered",
        "email": "metered@example.com",
        "status": "正常",
        "type": "free",
        "quota": 1,
        "image_quota_unknown": False,
        "panda_receive_state": "verified_ready",
        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. A2-4 回归：成功生图把额度打到 0，不得写 status=限流
# --------------------------------------------------------------------------- #

def test_quota_exhausting_success_does_not_set_limited_status():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(tmp_dir)
        service.add_account_items([_metered_account(quota=1, restore_at=_future(hours=5))])

        updated = service.mark_image_result("tok-metered", success=True)

        assert updated is not None
        assert updated["quota"] == 0
        # 这一条针对旧代码必失败：旧实现写 next_item["status"] = "限流"
        assert updated["status"] == "正常", "硬额度归零禁止写 status=限流（单向门）"
        # 与软熔断同规：用 flag 表达"当前不可派发"
        assert bool(updated["image_soft_capped"]) is True


def test_quota_exhausting_success_survives_reload_from_storage():
    """status 会落库，所以必须验证重启后也不是 限流。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = JSONStorageBackend(Path(tmp_dir) / "accounts.json")
        service = AccountService(storage)
        service.add_account_items([_metered_account(quota=1, restore_at=_future(hours=5))])
        service.mark_image_result("tok-metered", success=True)

        reborn = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))

        assert reborn.get_account("tok-metered")["status"] == "正常"


# --------------------------------------------------------------------------- #
# 2. 反向风险：窗口未到之前绝不可派发（不能用饥饿换 429 风暴）
# --------------------------------------------------------------------------- #

def test_zero_quota_before_restore_at_is_not_dispatchable():
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [_metered_account(quota=1, restore_at=_future(hours=6))]
        )
        service.mark_image_result("tok-metered", success=True)
        account = service.get_account("tok-metered")

        assert account["quota"] == 0
        assert AccountService._quota_window_due_for_lazy_refresh(account) is False
        assert AccountService._is_image_account_available(account) is False
        assert service._has_confirmed_image_quota(account) is False
        assert service._is_image_account_schedulable(account) is False
        assert service._list_ready_candidate_tokens() == []


def test_zero_quota_without_restore_at_is_not_dispatchable():
    """没有重置时间 → 无法确证窗口已过 → 必须留在池外（quota==0 自身就够挡）。"""
    with _no_jitter():
        account = _metered_account(quota=0, restore_at=None)
        assert AccountService._quota_window_due_for_lazy_refresh(account) is False
        assert AccountService._is_image_account_available(account) is False


def test_no_window_anchor_must_not_get_soft_cap_flag():
    """反向风险：soft_capped + 无 restore_at 在 _is_image_account_available 里是死路，
    且该 flag 只能由 remaining>0 的 limits_progress 清掉 → 会用饥饿换饥饿。"""
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items([_metered_account(access_token="tok-anchorless", quota=1)])
        assert service.get_account("tok-anchorless").get("restore_at") is None

        updated = service.mark_image_result("tok-anchorless", success=True)

        assert updated["quota"] == 0
        assert updated["status"] == "正常"
        assert bool(updated.get("image_soft_capped")) is False
        assert AccountService._is_image_account_available(updated) is False
        # status 治好了 → 会重新出现在 list_normal_tokens()，被刷新链路重新覆盖
        assert "tok-anchorless" in service.list_normal_tokens()


# --------------------------------------------------------------------------- #
# 3. 逃生口打开：restore_at 过后可懒刷新（旧代码在此必失败）
# --------------------------------------------------------------------------- #

def test_zero_quota_after_restore_at_is_eligible_for_lazy_refresh():
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [_metered_account(quota=1, restore_at=_past(hours=21))]
        )
        service.mark_image_result("tok-metered", success=True)
        account = service.get_account("tok-metered")

        assert account["quota"] == 0
        # 旧代码：status=限流 → :389 的 status 门禁直接 return False
        assert AccountService._quota_window_due_for_lazy_refresh(account) is True
        assert AccountService._is_image_account_available(account) is True
        assert service._has_confirmed_image_quota(account) is True


def test_normalize_heals_persisted_limited_status_on_next_write():
    """活体那一例：status=限流 / quota=0 / restore_at 已过期 21.4h。"""
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [
                _metered_account(
                    access_token="tok-stranded",
                    email="philliphicks336926@outlook.com",
                    status="限流",
                    quota=0,
                    restore_at=_past(hours=21, minutes=24),
                )
            ]
        )
        account = service.get_account("tok-stranded")

        assert account["status"] == "正常"
        assert bool(account["image_soft_capped"]) is True
        assert AccountService._quota_window_due_for_lazy_refresh(account) is True


def test_remote_refresh_reporting_fresh_quota_fully_recovers_account():
    """端到端：懒刷新拉到新额度后，flag 自愈、账号回到可派发。"""
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [
                _metered_account(
                    access_token="tok-stranded",
                    status="限流",
                    quota=0,
                    restore_at=_past(hours=21),
                )
            ]
        )
        reset_after = _future(days=1)

        # openai_backend_api.get_user_info() 的形状：quota==0 时它也会带 status=限流
        service.update_account(
            "tok-stranded",
            {
                "quota": 25,
                "image_quota_unknown": False,
                "restore_at": reset_after,
                "status": "正常",
                "limits_progress": [
                    {"feature_name": "image_gen", "remaining": 25, "reset_after": reset_after}
                ],
            },
            quiet=True,
        )
        account = service.get_account("tok-stranded")

        assert account["status"] == "正常"
        assert account["quota"] == 25
        assert bool(account["image_soft_capped"]) is False
        assert AccountService._is_image_account_available(account) is True


def test_remote_refresh_still_zero_keeps_account_out_of_pool():
    """上游仍报 0 且推后窗口 → 不能派发，但也不能再被写死成单向门。"""
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [_metered_account(access_token="tok-dry", status="限流", quota=0, restore_at=_past(hours=21))]
        )
        reset_after = _future(hours=8)

        service.update_account(
            "tok-dry",
            {
                "quota": 0,
                "image_quota_unknown": False,
                "status": "限流",
                "restore_at": reset_after,
                "limits_progress": [
                    {"feature_name": "image_gen", "remaining": 0, "reset_after": reset_after}
                ],
            },
            quiet=True,
        )
        account = service.get_account("tok-dry")

        assert account["status"] == "正常"
        assert account["quota"] == 0
        assert AccountService._is_image_account_available(account) is False
        # 关键：窗口推到未来了，但 status 不再是终态，窗口一过仍能自愈
        assert AccountService._quota_window_due_for_lazy_refresh(account) is False


# --------------------------------------------------------------------------- #
# 4. image_quota_state 不再对可恢复账号报 blocked
# --------------------------------------------------------------------------- #

def test_image_quota_state_no_longer_blocked_for_recoverable_account():
    """quota>0 却被 限流 卡住 → 旧代码 image_quota_state()=='blocked'。"""
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [
                _metered_account(
                    access_token="tok-blocked",
                    status="限流",
                    quota=25,
                    restore_at=_future(days=1),
                    limits_progress=[
                        {"feature_name": "image_gen", "remaining": 25, "reset_after": _future(days=1)}
                    ],
                )
            ]
        )
        account = service.get_account("tok-blocked")

        assert account["status"] == "正常"
        state = service.image_quota_state(account)
        assert state != "blocked"
        assert state in {"ready", "unverified", "stale"}
        assert service.available_image_quota_for_account(account) == 25


def test_image_quota_state_reports_refresh_pending_after_window():
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [_metered_account(access_token="tok-pending", status="限流", quota=0, restore_at=_past(hours=21))]
        )
        account = service.get_account("tok-pending")

        assert service.image_quota_state(account) == "refresh_pending"
        # 未确认额度前不给上层报可用额度
        assert service.available_image_quota_for_account(account) == 0


# --------------------------------------------------------------------------- #
# 5. 真无限额账号不受影响
# --------------------------------------------------------------------------- #

def test_true_unlimited_account_is_unaffected():
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [
                _metered_account(
                    access_token="tok-pro",
                    email="pro@example.com",
                    type="Pro",
                    quota=0,
                    image_quota_unknown=True,
                )
            ]
        )

        updated = service.mark_image_result("tok-pro", success=True)

        assert updated["status"] == "正常"
        assert updated["quota"] == 0
        assert bool(updated.get("image_soft_capped")) is False
        assert AccountService._is_image_account_available(updated) is True
        assert service.image_quota_state(updated) == "unlimited"


def test_true_unlimited_limited_status_is_left_alone_by_heal():
    """Pro 不参与额度扣减，不属于 A2-4 漂移链路 → 保持原状，改动面不扩大。"""
    account = {"type": "Pro", "status": "限流", "quota": 0}
    healed = AccountService._heal_hard_quota_limited_status(dict(account))
    assert healed["status"] == "限流"
    assert recovery.classify(account) == "true_unlimited"


# --------------------------------------------------------------------------- #
# 6. image_quota_unknown 账号不受影响
# --------------------------------------------------------------------------- #

def test_quota_unknown_account_is_unaffected():
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [
                _metered_account(
                    access_token="tok-unknown",
                    quota=0,
                    image_quota_unknown=True,
                    status="限流",
                )
            ]
        )
        account = service.get_account("tok-unknown")

        # 额度未知 ≠ 已耗尽：不确证可恢复，status 保持原样
        assert account["status"] == "限流"
        assert bool(account["image_quota_unknown"]) is True
        assert AccountService._is_image_account_available(account) is False
        assert AccountService._quota_window_due_for_lazy_refresh(account) is False
        assert recovery.classify(account) == "quota_unknown"


def test_quota_unknown_success_does_not_consume_or_flag():
    with tempfile.TemporaryDirectory() as tmp_dir, _no_jitter():
        service = _service(tmp_dir)
        service.add_account_items(
            [_metered_account(access_token="tok-unknown2", quota=0, image_quota_unknown=True)]
        )

        updated = service.mark_image_result("tok-unknown2", success=True)

        assert updated["status"] == "正常"
        assert updated["quota"] == 0
        assert bool(updated.get("image_soft_capped")) is False


# --------------------------------------------------------------------------- #
# 7. A2-5 恢复脚本：dry-run 零写入 / --apply 精确修复 / 幂等
# --------------------------------------------------------------------------- #

_STRANDED_ROWS: tuple[dict, ...] = (
    # 沉底：restore_at 已过期（活体那一例）
    {
        "access_token": "tok-expired",
        "email": "expired@example.com",
        "status": "限流",
        "type": "free",
        "quota": 0,
        "image_quota_unknown": False,
        "restore_at": _past(hours=21, minutes=24),
    },
    # 沉底：账面有额度却被 限流 卡住（image_quota_state == blocked）
    {
        "access_token": "tok-positive",
        "email": "positive@example.com",
        "status": "限流",
        "type": "free",
        "quota": 25,
        "image_quota_unknown": False,
        "restore_at": _future(days=1),
    },
    # 沉底：连 restore_at 都没有，懒刷新永远不会触发
    {
        "access_token": "tok-norestore",
        "email": "norestore@example.com",
        "status": "限流",
        "type": "free",
        "quota": 0,
        "image_quota_unknown": False,
    },
)

_UNTOUCHED_ROWS: tuple[dict, ...] = (
    # 合法耗尽中：窗口还没到 → 不是沉底，不许动（否则换来 429 风暴）
    {
        "access_token": "tok-pending",
        "email": "pending@example.com",
        "status": "限流",
        "type": "free",
        "quota": 0,
        "image_quota_unknown": False,
        "restore_at": _future(hours=6),
    },
    {
        "access_token": "tok-normal",
        "email": "normal@example.com",
        "status": "正常",
        "type": "free",
        "quota": 12,
        "image_quota_unknown": False,
    },
    {
        "access_token": "tok-pro",
        "email": "pro@example.com",
        "status": "限流",
        "type": "Pro",
        "quota": 0,
        "image_quota_unknown": True,
    },
    {
        "access_token": "tok-unknown",
        "email": "unknown@example.com",
        "status": "限流",
        "type": "free",
        "quota": 0,
        "image_quota_unknown": True,
    },
    {
        "access_token": "tok-disabled",
        "email": "disabled@example.com",
        "status": "禁用",
        "type": "free",
        "quota": 0,
        "image_quota_unknown": False,
    },
)


def _build_fixture_db(path: Path) -> None:
    """一次性抛弃型 SQLite 夹具，schema 与 DatabaseStorageBackend.accounts 对齐。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE accounts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " access_token VARCHAR(2048) NOT NULL UNIQUE,"
            " data TEXT NOT NULL)"
        )
        for row in (*_STRANDED_ROWS, *_UNTOUCHED_ROWS):
            conn.execute(
                "INSERT INTO accounts (access_token, data) VALUES (?, ?)",
                (row["access_token"], json.dumps(row, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def _read_fixture(path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            token: json.loads(raw)
            for token, raw in conn.execute("select access_token, data from accounts")
        }
    finally:
        conn.close()


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    path = tmp_path / "accounts-fixture.db"
    _build_fixture_db(path)
    return path


def test_recovery_classifies_stranded_and_pending_apart(fixture_db: Path):
    with _no_jitter():
        rows = _read_fixture(fixture_db)
        reasons = {token: recovery.classify(row) for token, row in rows.items()}

    assert reasons["tok-expired"] == "window_expired"
    assert reasons["tok-positive"] == "quota_positive"
    assert reasons["tok-norestore"] == "no_restore_at"
    assert reasons["tok-pending"] == "window_pending"
    assert reasons["tok-normal"] == "not_rate_limited"
    assert reasons["tok-pro"] == "true_unlimited"
    assert reasons["tok-unknown"] == "quota_unknown"
    stranded = {t for t, r in reasons.items() if r in recovery.STRANDED_REASONS}
    assert stranded == {"tok-expired", "tok-positive", "tok-norestore"}


def test_recovery_dry_run_writes_nothing(fixture_db: Path, capsys):
    before = _read_fixture(fixture_db)
    digest_before = fixture_db.read_bytes()

    with _no_jitter():
        assert recovery.main(["--sqlite", str(fixture_db)]) == 0

    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["mode"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["would_repair"] == 3
    assert payload["summary"]["stranded"] == 3
    # 零写入：文件字节与逻辑内容都不变
    assert fixture_db.read_bytes() == digest_before
    assert _read_fixture(fixture_db) == before
    # before/after 表可肉眼核对
    assert "<before>" in out and "<after" in out
    assert "expired@example.com" in out
    assert not list(fixture_db.parent.glob("backups/**/*.db"))


def test_recovery_apply_repairs_exactly_the_stranded_rows(fixture_db: Path, capsys):
    with _no_jitter():
        assert recovery.main(["--sqlite", str(fixture_db), "--apply", "--quiet"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["mode"] == "apply"
    assert payload["repaired"] == 3
    rows = _read_fixture(fixture_db)

    # 三行沉底账号：status 回到正常，仍靠 flag/restore_at 控制派发
    assert rows["tok-expired"]["status"] == "正常"
    assert rows["tok-expired"]["image_soft_capped"] is True
    assert rows["tok-norestore"]["status"] == "正常"
    # 无时间锚点 → 不打 soft cap（否则又是一个永久沉底）；quota==0 自身挡住派发
    assert rows["tok-norestore"].get("image_soft_capped") in (None, False)
    assert AccountService._is_image_account_available(rows["tok-norestore"]) is False
    # quota>0 的那行不该被打上 soft cap（它应当立刻可用）
    assert rows["tok-positive"]["status"] == "正常"
    assert rows["tok-positive"].get("image_soft_capped") in (None, False)

    # 其余行逐字节不变
    for row in _UNTOUCHED_ROWS:
        token = row["access_token"]
        assert rows[token] == row, f"{token} must not be touched"

    # 备份 + rollback 落盘
    assert payload["backup"], "apply 必须先备份"
    assert Path(payload["backup"][0]).exists()
    rollback = json.loads(Path(payload["rollback"]).read_text(encoding="utf-8"))
    assert len(rollback["entries"]) == 3
    assert all(e["before"]["status"] == "限流" for e in rollback["entries"])


def test_recovery_apply_is_idempotent(fixture_db: Path, capsys):
    with _no_jitter():
        recovery.main(["--sqlite", str(fixture_db), "--apply", "--quiet"])
        capsys.readouterr()
        after_first = _read_fixture(fixture_db)

        assert recovery.main(["--sqlite", str(fixture_db), "--apply", "--quiet"]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["repaired"] == 0
    assert payload["summary"]["stranded"] == 0
    assert _read_fixture(fixture_db) == after_first


def test_recovery_rollback_restores_original_rows(fixture_db: Path, capsys):
    with _no_jitter():
        recovery.main(["--sqlite", str(fixture_db), "--apply", "--quiet"])
        applied = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        rollback_path = applied["rollback"]

        assert (
            recovery.main(
                ["--sqlite", str(fixture_db), "--rollback", rollback_path, "--apply"]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["restored"] == 3
    rows = _read_fixture(fixture_db)
    for row in _STRANDED_ROWS:
        assert rows[row["access_token"]]["status"] == "限流"


def test_recovery_requires_explicit_source():
    """没有隐式默认库：绝不会因为漏参数就打到生产 accounts.db。"""
    with pytest.raises(SystemExit):
        recovery.main([])


def test_recovery_json_out_snapshot(fixture_db: Path, tmp_path: Path, capsys):
    out_path = tmp_path / "report.json"
    with _no_jitter():
        recovery.main(["--sqlite", str(fixture_db), "--json-out", str(out_path), "--quiet"])
    capsys.readouterr()

    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["summary"]["scanned"] == len(_STRANDED_ROWS) + len(_UNTOUCHED_ROWS)
    assert report["result"]["mode"] == "dry_run"
    # 报表不得夹带明文 token
    assert "tok-expired" not in out_path.read_text(encoding="utf-8")
