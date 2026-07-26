from services.config import _normalize_account_warmup_settings


def test_normalize_account_warmup_defaults():
    out = _normalize_account_warmup_settings({})
    assert out["enabled"] is True
    assert out["interval_sec"] == 60.0
    assert out["max_hot"] == 10
    assert out["depth"] == "requirements"
    assert out["rotate_per_tick"] == 0


def test_normalize_account_warmup_clamps_interval():
    out = _normalize_account_warmup_settings({"interval_sec": 5, "depth": "bootstrap"})
    assert out["interval_sec"] == 30.0
    assert out["depth"] == "bootstrap"
    assert out["rotate_per_tick"] == 0
    assert out["cf_fail_max_streak"] == 2
