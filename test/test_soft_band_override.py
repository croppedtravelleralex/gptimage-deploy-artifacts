from __future__ import annotations

import unittest

from services.humanlike_scheduler import update_quota_peak_state


class SoftBandOverrideTests(unittest.TestCase):
    def test_override_wins_over_redraw(self) -> None:
        state = update_quota_peak_state(
            remaining=30,
            reset_after="2026-07-21T00:00:00+00:00",
            prev_peak=50,
            prev_reset_at="2026-07-20T00:00:00+00:00",
            prev_soft_band=0.4,
            soft=0.7,
            soft_band_override=0.55,
        )
        self.assertAlmostEqual(state.soft_band, 0.55)
        self.assertFalse(state.soft_capped)

    def test_override_can_trip_cap(self) -> None:
        state = update_quota_peak_state(
            remaining=10,
            reset_after="2026-07-21T00:00:00+00:00",
            prev_peak=100,
            prev_reset_at="2026-07-21T00:00:00+00:00",
            prev_soft_band=0.9,
            soft=0.7,
            soft_band_override=0.5,
        )
        self.assertAlmostEqual(state.soft_band, 0.5)
        self.assertGreaterEqual(state.used_ratio, 0.5)
        self.assertTrue(state.soft_capped)


if __name__ == "__main__":
    unittest.main()
