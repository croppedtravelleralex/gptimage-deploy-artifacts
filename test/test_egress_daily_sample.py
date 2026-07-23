"""Unit tests for egress_daily sampling and usage email fallback."""

from __future__ import annotations

from services.account_service import AccountService


def test_record_egress_sample_warn_on_registration_drift(tmp_path, monkeypatch):
    svc = AccountService.__new__(AccountService)
    svc._lock = __import__("threading").RLock()
    svc._accounts = {}
    svc._persist_upsert_accounts = lambda accounts: None
    svc._resolve_access_token_locked = lambda t: t
    svc._normalize_account = lambda item: dict(item)
    svc._now = lambda: "2026-07-20T00:00:00"

    token = "tok-egress-1"
    svc._accounts[token] = {
        "access_token": token,
        "email": "a@example.com",
        "proxy_egress_ip": "1.2.3.4",
        "proxy_egress_hash": "aaaa1111bbbb",
        "registration_egress_hash": "cccc2222dddd",
        "egress_daily": [],
    }
    assert svc.record_egress_sample(token, status="ok") is True
    days = svc._accounts[token]["egress_daily"]
    assert len(days) == 1
    assert days[0]["status"] == "warn"
    assert days[0]["hash"] == "aaaa1111bbbb"


def test_record_egress_sample_ok_when_matches_registration(tmp_path):
    svc = AccountService.__new__(AccountService)
    svc._lock = __import__("threading").RLock()
    svc._accounts = {}
    svc._persist_upsert_accounts = lambda accounts: None
    svc._resolve_access_token_locked = lambda t: t
    svc._normalize_account = lambda item: dict(item)

    token = "tok-egress-2"
    h = "samehash1234"
    svc._accounts[token] = {
        "access_token": token,
        "email": "b@example.com",
        "proxy_egress_ip": "5.6.7.8",
        "proxy_egress_hash": h,
        "registration_egress_hash": h,
        "egress_daily": [],
    }
    assert svc.record_egress_sample(token, status="ok") is True
    assert svc._accounts[token]["egress_daily"][0]["status"] == "ok"
