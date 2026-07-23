from services.risk_dashboard_service import AUTOMATION_WEIGHTS, automation_factors_from_point


def test_automation_weights_sum_to_one():
    assert abs(sum(AUTOMATION_WEIGHTS.values()) - 1.0) < 1e-9


def test_automation_risk_low_when_pool_healthy():
    factors = automation_factors_from_point(
        {
            "schedulable": 6,
            "soft_capped_count": 0,
            "identity_isolated": 0,
            "incoming": 0,
            "fail_streak_ge3": 0,
            "cooldown_account_count": 0,
            "cohort_paused": 0,
            "cohort_terminal_hits_sum": 0,
            "busy_429_count": 0,
            "llm_ops_ok": 20,
            "llm_ops_error": 0,
        }
    )
    assert factors["detection"] == 0.0
    assert factors["soft_risk"] == 0.0
    assert factors["composite"] < 15.0


def test_automation_risk_high_when_isolated_and_soft():
    factors = automation_factors_from_point(
        {
            "schedulable": 1,
            "soft_capped_count": 3,
            "identity_isolated": 4,
            "incoming": 0,
            "fail_streak_ge3": 2,
            "cooldown_account_count": 1,
            "cohort_paused": 1,
            "cohort_terminal_hits_sum": 5,
            "busy_429_count": 4,
            "llm_ops_ok": 1,
            "llm_ops_error": 5,
        }
    )
    assert factors["detection"] > 40
    assert factors["soft_risk"] > 30
    assert factors["cohort_risk"] >= 80
    assert factors["composite"] > 40
