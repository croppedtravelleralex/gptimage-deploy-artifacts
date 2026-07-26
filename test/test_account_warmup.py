"""Unit tests for account warmup demote / session accounting."""

from unittest.mock import MagicMock, patch

from services.account_warmup_service import AccountWarmupService, _settings


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


def test_settings_use_config_normalization():
    settings = _settings()
    assert settings["interval_sec"] >= 30.0
    assert settings["max_hot"] >= 1
    assert settings["depth"] in {"bootstrap", "requirements"}


@patch("services.account_warmup_service.config.get_account_warmup_settings")
@patch("services.account_warmup_service.account_service.record_cf_sample", return_value=True)
def test_warmup_requirements_depth_records_ok(mock_cf, mock_settings):
    mock_settings.return_value = {
        "enabled": True,
        "interval_sec": 60.0,
        "max_hot": 3,
        "max_sessions_per_hot": 3,
        "demote_cooldown_sec": 180.0,
        "freq_window_sec": 60.0,
        "freq_max_starts": 6,
        "startup_delay_sec": 0.0,
        "depth": "requirements",
        "rotate_per_tick": 0,
        "hot_refresh_min_interval_sec": 300.0,
        "schedulable_only": True,
        "cf_fail_max_streak": 2,
        "cf_block_sec": 86400.0,
    }
    svc = AccountWarmupService()
    backend = MagicMock()
    with patch("services.account_warmup_service.OpenAIBackendAPI", return_value=backend):
        ok = svc._warmup_one({"access_token": "tok", "email": "warm@example.com"}, mock_settings.return_value)
    assert ok is True
    backend._ensure_bootstrap.assert_called_once_with(soft_fail=True)
    backend._get_chat_requirements_once.assert_called_once()
    mock_cf.assert_called_with("tok", kind="ok")


@patch("services.account_warmup_service.config.get_account_warmup_settings")
@patch("services.account_warmup_service.account_service.record_cf_sample", return_value=True)
def test_warmup_requirements_cf_demotes_hot(mock_cf, mock_settings):
    mock_settings.return_value = {
        "enabled": True,
        "interval_sec": 60.0,
        "max_hot": 3,
        "max_sessions_per_hot": 3,
        "demote_cooldown_sec": 180.0,
        "freq_window_sec": 60.0,
        "freq_max_starts": 6,
        "startup_delay_sec": 0.0,
        "depth": "requirements",
        "rotate_per_tick": 0,
        "hot_refresh_min_interval_sec": 300.0,
        "schedulable_only": True,
        "cf_fail_max_streak": 2,
        "cf_block_sec": 86400.0,
    }
    svc = AccountWarmupService()
    email = "hot@example.com"
    with svc._lock:
        svc._hot[email] = 1.0
    backend = MagicMock()
    backend._get_chat_requirements_once.side_effect = RuntimeError("cf_edge_block")
    with patch("services.account_warmup_service.OpenAIBackendAPI", return_value=backend):
        ok = svc._warmup_one({"access_token": "tok", "email": email}, mock_settings.return_value)
    assert ok is False
    assert email not in svc.status()["hot"]
    mock_cf.assert_called_with("tok", kind="cf")


@patch("services.account_warmup_service.config.get_account_warmup_settings")
@patch("services.account_warmup_service.account_service.record_cf_sample", return_value=True)
def test_warmup_cf_streak_blocks_account(mock_cf, mock_settings):
    mock_settings.return_value = {
        "enabled": True,
        "interval_sec": 60.0,
        "max_hot": 3,
        "max_sessions_per_hot": 3,
        "demote_cooldown_sec": 180.0,
        "freq_window_sec": 60.0,
        "freq_max_starts": 6,
        "startup_delay_sec": 0.0,
        "depth": "requirements",
        "rotate_per_tick": 0,
        "hot_refresh_min_interval_sec": 300.0,
        "schedulable_only": True,
        "cf_fail_max_streak": 2,
        "cf_block_sec": 86400.0,
    }
    svc = AccountWarmupService()
    email = "bad@example.com"
    backend = MagicMock()
    backend._get_chat_requirements_once.side_effect = RuntimeError("cf_edge_block")
    with patch("services.account_warmup_service.OpenAIBackendAPI", return_value=backend):
        assert svc._warmup_one({"access_token": "tok", "email": email}, mock_settings.return_value) is False
        assert svc._warmup_one({"access_token": "tok", "email": email}, mock_settings.return_value) is False
    st = svc.status()
    assert email in st["blocked_until"]
    assert st["cf_fail_streak"].get(email) == 2
    with patch("services.account_warmup_service.OpenAIBackendAPI", return_value=backend):
        assert svc._warmup_one({"access_token": "tok", "email": email}, mock_settings.return_value) is False
    assert backend._get_chat_requirements_once.call_count == 2
