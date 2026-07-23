"""Unit tests for cf_daily passive sampling."""

from __future__ import annotations

import threading

from services.account_service import AccountService


def _svc() -> AccountService:
    svc = AccountService.__new__(AccountService)
    svc._lock = threading.RLock()
    svc._accounts = {}
    svc._persist_upsert_accounts = lambda accounts: None
    svc._resolve_access_token_locked = lambda t: t
    svc._normalize_account = lambda item: dict(item)
    return svc


def test_record_cf_sample_accumulates_kinds():
    svc = _svc()
    token = "tok-cf-1"
    svc._accounts[token] = {"access_token": token, "email": "a@example.com", "cf_daily": []}
    assert svc.record_cf_sample(token, kind="ok") is True
    assert svc.record_cf_sample(token, kind="ok") is True
    assert svc.record_cf_sample(token, kind="cf") is True
    assert svc.record_cf_sample(token, kind="image_fail") is True
    row = svc._accounts[token]["cf_daily"][0]
    assert row["ok"] == 2
    assert row["cf"] == 1
    assert row["image_fail"] == 1
    assert len(str(row["date"])) == 10


def test_record_cf_sample_rejects_bad_kind():
    svc = _svc()
    token = "tok-cf-2"
    svc._accounts[token] = {"access_token": token, "cf_daily": []}
    assert svc.record_cf_sample(token, kind="probe") is False
    assert svc._accounts[token]["cf_daily"] == []


def test_mark_image_result_cf_only_counts_cf_not_image_fail():
    svc = _svc()
    token = "tok-cf-3"
    svc._accounts[token] = {
        "access_token": token,
        "email": "c@example.com",
        "type": "plus",
        "status": "正常",
        "quota": 10,
        "success": 0,
        "fail": 0,
        "cf_daily": [],
    }
    svc.release_image_slot = lambda *_a, **_k: None
    svc._is_true_unlimited_image_account = lambda _a: False
    svc._stamp_image_next_ok = lambda a: a
    svc._apply_humanlike_quota_fields = lambda a: a
    svc._persist_delete_accounts = lambda _tokens: None

    svc.mark_image_result(token, False, error="cloudflare_or_edge_html_block: blocked")
    days = svc._accounts[token]["cf_daily"]
    assert len(days) == 1
    assert days[0]["cf"] == 1
    assert days[0]["image_fail"] == 0
    assert svc._accounts[token]["fail"] == 1


def test_mark_image_result_non_cf_counts_image_fail():
    svc = _svc()
    token = "tok-cf-4"
    svc._accounts[token] = {
        "access_token": token,
        "email": "d@example.com",
        "type": "plus",
        "status": "正常",
        "quota": 10,
        "success": 0,
        "fail": 0,
        "cf_daily": [],
    }
    svc.release_image_slot = lambda *_a, **_k: None
    svc._is_true_unlimited_image_account = lambda _a: False
    svc._stamp_image_next_ok = lambda a: a
    svc._apply_humanlike_quota_fields = lambda a: a
    svc._persist_delete_accounts = lambda _tokens: None

    svc.mark_image_result(token, False, error="upstream timeout")
    days = svc._accounts[token]["cf_daily"]
    assert days[0]["image_fail"] == 1
    assert days[0]["cf"] == 0
