from services.risk_audit_service import _deterministic_risk_level, _extract_json_object


def test_deterministic_ok_when_schedulable_healthy():
    level = _deterministic_risk_level(
        {
            "breakdown": {"buckets": {"schedulable": 6}},
            "derived": {"soft_capped_count": 0, "cooldown_account_count": 0, "fail_streak_ge3": 0, "dup_binding_groups": 0},
            "cohort": {"paused_cohort_count": 0},
            "llm_ops": {"error_pool": 2, "ok": 10},
            "gaps": ["workload_shadow", "maturity_stage_mostly_empty"],
        }
    )
    assert level == "ok"


def test_deterministic_medium_on_dup_binding():
    level = _deterministic_risk_level(
        {
            "breakdown": {"buckets": {"schedulable": 4}},
            "derived": {"dup_binding_groups": 2, "soft_capped_count": 0},
            "cohort": {"paused_cohort_count": 0},
            "llm_ops": {"error_pool": 0, "ok": 1},
        }
    )
    assert level == "medium"


def test_extract_json_from_markdown_fence():
    obj = _extract_json_object('```json\n{"risk_level_hint":"ok","findings":[]}\n```')
    assert obj["risk_level_hint"] == "ok"


def test_extract_json_extra_data_trailing_object():
    """DeepSeek sometimes emits two JSON objects → json.loads Extra data."""
    raw = '{"risk_level_hint":"ok","findings":[]}\n{"notes":"trash"}'
    obj = _extract_json_object(raw)
    assert obj["risk_level_hint"] == "ok"
    assert obj.get("findings") == []


def test_extract_json_extra_data_line3_style():
    raw = 'Here is the result:\n{"risk_level_hint":"low","findings":[{"item":"a","detail":"b"}]}\nExtra: {"x":1}'
    obj = _extract_json_object(raw)
    assert obj["risk_level_hint"] == "low"
    assert len(obj["findings"]) == 1
