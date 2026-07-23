"""Unit tests for account warmup demote / session accounting."""

from services.account_warmup_service import AccountWarmupService


def test_begin_chat_demotes_when_sessions_reach_threshold():
    svc = AccountWarmupService()
    email = "hot@example.com"
    with svc._lock:
        svc._hot[email] = 1.0
    # max_sessions_per_hot default 3 → demote on 3rd begin
    svc.begin_chat_session(email)
    svc.begin_chat_session(email)
    assert email in svc.status()["hot"]
    svc.begin_chat_session(email)
    assert email not in svc.status()["hot"]
    assert email in svc.status()["demoted_until"]
    svc.end_chat_session(email)
    svc.end_chat_session(email)
    svc.end_chat_session(email)
    assert svc.status()["inflight"].get(email) in (None, 0)


def test_end_chat_session_clears_inflight():
    svc = AccountWarmupService()
    email = "a@b.c"
    svc.begin_chat_session(email)
    assert svc.status()["inflight"].get(email) == 1
    svc.end_chat_session(email)
    assert not svc.status()["inflight"].get(email)
