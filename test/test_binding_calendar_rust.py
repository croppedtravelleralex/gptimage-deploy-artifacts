"""Rust binding_calendar 与 Python fallback 对齐测试。"""
from __future__ import annotations

import unittest
from datetime import date

from services.humanlike_scheduler import _stable_u
from services.image_pipeline.binding_calendar import (
    REFRESH_SALT,
    _py_compute_account_phase_slot,
    engine_info,
    evaluate_schedule_pick,
)
from services.image_pipeline import binding_calendar as bc_mod


class BindingCalendarRustTests(unittest.TestCase):
    def test_stable_u_golden(self) -> None:
        u = _stable_u(["bind-1", "2026-07-28", "0", REFRESH_SALT, "binding"])
        self.assertGreaterEqual(u, 0.0)
        self.assertLess(u, 1.0)

    def test_rust_python_slot_parity_when_rust_available(self) -> None:
        info = engine_info()
        if info.get("engine") != "rust":
            self.skipTest("rust lib not built")
        day = date(2026, 7, 28)
        py = _py_compute_account_phase_slot(
            account_key="acct@test.com",
            binding_key="bind-1",
            local_day=day,
            phase_index=0,
            tz_name="Asia/Singapore",
            jitter_min_minutes=30,
            jitter_max_minutes=60,
            salt=REFRESH_SALT,
        )
        rust = bc_mod._RUST.account_phase_slot_unix(
            account_key="acct@test.com",
            binding_key="bind-1",
            local_day=day,
            phase_index=0,
            tz_name="Asia/Singapore",
            jitter_min_minutes=30,
            jitter_max_minutes=60,
            salt=REFRESH_SALT,
        )
        self.assertIsNotNone(rust)
        binding_unix, account_unix = rust or (0, 0)
        self.assertEqual(binding_unix, int(py["binding_slot_utc"].timestamp()))
        self.assertEqual(account_unix, int(py["account_slot_utc"].timestamp()))

    def test_evaluate_schedule_pick(self) -> None:
        day = "2026-07-28"
        slot = _py_compute_account_phase_slot(
            account_key="a1",
            binding_key="b1",
            local_day=date.fromisoformat(day),
            phase_index=0,
            tz_name="Asia/Singapore",
            jitter_min_minutes=30,
            jitter_max_minutes=60,
            salt=REFRESH_SALT,
        )
        now_unix = int(slot["account_slot_utc"].timestamp()) + 1
        out = evaluate_schedule_pick(
            {
                "now_unix": now_unix,
                "binding_gap_sec": 0,
                "binding_last_refresh_unix": {},
                "jitter_min_minutes": 30,
                "jitter_max_minutes": 60,
                "accounts": [
                    {
                        "index": 7,
                        "account_key": "a1",
                        "binding_key": "b1",
                        "tz_name": "Asia/Singapore",
                        "local_date": day,
                        "phases_done": [],
                        "schedulable": True,
                    }
                ],
            }
        )
        picked = out.get("picked")
        self.assertIsInstance(picked, dict)
        self.assertEqual(picked.get("index"), 7)


if __name__ == "__main__":
    unittest.main()
