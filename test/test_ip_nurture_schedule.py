from __future__ import annotations

from datetime import datetime, timezone

from services.ip_nurture_schedule import (
    SLOTS_PER_DAY,
    current_slot_weight,
    get_preset,
    list_presets,
    save_binding_schedule,
    slot_allowed,
)


def test_list_presets_has_at_least_25() -> None:
    presets = list_presets()
    assert len(presets) >= 25
    assert all("id" in p and "label" in p for p in presets)


def test_get_preset_returns_7x12_matrix() -> None:
    preset = get_preset("business_hours")
    assert preset is not None
    weights = preset["weights"]
    assert len(weights) == 7
    assert all(len(row) == SLOTS_PER_DAY for row in weights)
    assert all(0.0 <= v <= 1.0 for row in weights for v in row)


def test_current_slot_weight_monday_morning() -> None:
    preset = get_preset("morning_focus")
    assert preset is not None
    # 2026-07-20 is Monday; 09:30 SGT → slot 4
    now = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)  # 09:30 SGT
    weight = current_slot_weight(preset["weights"], tz_name="Asia/Singapore", now_utc=now)
    assert weight > 0.5


def test_slot_allowed_threshold() -> None:
    matrix = [[0.1 for _ in range(SLOTS_PER_DAY)] for _ in range(7)]
    now = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)  # Mon 09:30 SGT → slot 4
    assert not slot_allowed(matrix, now_utc=now)
    matrix[0][4] = 0.2
    assert slot_allowed(matrix, now_utc=now)


def test_save_binding_schedule_roundtrip(monkeypatch) -> None:
    saved_payload: dict = {}

    def _fake_update(data: dict) -> dict:
        saved_payload.update(data)
        return data

    monkeypatch.setattr("services.ip_nurture_schedule.config", type("Cfg", (), {"data": {}, "update": staticmethod(_fake_update)})())
    saved = save_binding_schedule("bind-1", "business_hours")
    assert saved["binding_key"] == "bind-1"
    assert saved["preset_id"] == "business_hours"
    assert len(saved["weights"]) == 7
    assert "bind-1" in saved_payload.get("ip_nurture_bindings", {})
