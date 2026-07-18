import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from services.humanlike_scheduler import (
    compute_resume_delay_seconds,
    compute_submit_gap_seconds,
    compute_submit_interval_ms,
    decide_proactive_refresh,
    draw_soft_band,
    effective_daily_soft,
    fail_cooldown_seconds,
    is_new_image_account,
    map_egress_region_to_tz,
    night_or_lunch_soft_weight,
    resolve_account_tz_name,
    update_quota_peak_state,
)
import random


class HumanlikeSchedulerTests(unittest.TestCase):
    def test_gap_within_jitter_bounds(self) -> None:
        rng = random.Random(1)
        gaps = [
            compute_submit_gap_seconds(base_sec=60, jitter_lo=0.65, jitter_hi=1.45, poisson_lambda_sec=0, rng=rng)
            for _ in range(50)
        ]
        self.assertTrue(all(39 <= g <= 87.1 for g in gaps))

    def test_soft_band_triggers(self) -> None:
        state = update_quota_peak_state(
            remaining=7,
            reset_after="2026-07-19T00:00:00+00:00",
            prev_peak=25,
            prev_reset_at="2026-07-19T00:00:00+00:00",
            prev_soft_band=0.70,
            soft=0.70,
            rng=random.Random(0),
        )
        self.assertEqual(state.peak, 25)
        self.assertGreaterEqual(state.used_ratio, 0.70)
        self.assertTrue(state.soft_capped)

    def test_new_window_resets_peak(self) -> None:
        state = update_quota_peak_state(
            remaining=25,
            reset_after="2026-07-20T00:00:00+00:00",
            prev_peak=7,
            prev_reset_at="2026-07-19T00:00:00+00:00",
            prev_soft_band=0.70,
            soft=0.70,
            rng=random.Random(0),
        )
        self.assertEqual(state.peak, 25)
        self.assertFalse(state.soft_capped)

    def test_proactive_weekend_can_skip(self) -> None:
        # 2026-07-18 is Saturday in Singapore
        now = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)  # 14:00 SG
        due_count = 0
        for i in range(100):
            d = decide_proactive_refresh(
                now_utc=now,
                account_key=f"acct-{i}",
                done_date=None,
                p_work=1.0,
                p_rest=0.35,
            )
            if d.due:
                due_count += 1
        # Expect roughly ~35 due; allow wide band
        self.assertGreater(due_count, 15)
        self.assertLess(due_count, 55)

    def test_proactive_already_done(self) -> None:
        tz = ZoneInfo("Asia/Singapore")
        local = datetime(2026, 7, 17, 12, 0, tzinfo=tz)  # Friday
        now = local.astimezone(timezone.utc)
        d = decide_proactive_refresh(
            now_utc=now,
            account_key="acct-1",
            done_date="2026-07-17",
            p_work=1.0,
        )
        self.assertFalse(d.due)
        self.assertEqual(d.reason, "already_done_today")

    def test_draw_soft_band_range(self) -> None:
        rng = random.Random(2)
        bands = [draw_soft_band(0.70, rng=rng) for _ in range(30)]
        self.assertTrue(all(0.65 <= b <= 0.73 for b in bands))

    def test_map_egress_region_to_tz(self) -> None:
        self.assertEqual(map_egress_region_to_tz("sg"), "Asia/Singapore")
        self.assertEqual(map_egress_region_to_tz("HKG"), "Asia/Hong_Kong")
        self.assertEqual(map_egress_region_to_tz("tyo-edge"), "Asia/Tokyo")

    def test_resolve_account_tz_from_egress(self) -> None:
        tz = resolve_account_tz_name(
            {"proxy_region": "sin"},
            timezone_from_egress=True,
            default_tz="Asia/Singapore",
        )
        self.assertEqual(tz, "Asia/Singapore")
        tz2 = resolve_account_tz_name(
            {"proxy_country": "jp"},
            timezone_from_egress=True,
            default_tz="Asia/Singapore",
        )
        self.assertEqual(tz2, "Asia/Tokyo")

    def test_night_soft_weight(self) -> None:
        # 2026-07-18 02:00 SG = 2026-07-17 18:00 UTC
        night = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(night_or_lunch_soft_weight(night, night_weight=0.4), 0.4)
        day = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)  # 10:00 SG
        self.assertEqual(night_or_lunch_soft_weight(day), 1.0)

    def test_new_account_soft_cap(self) -> None:
        account = {"maturity_stage": "observe"}
        self.assertTrue(is_new_image_account(account))
        self.assertAlmostEqual(effective_daily_soft(0.70, account, new_account_cap=0.40), 0.40)

    def test_resume_delay_first_and_backoff(self) -> None:
        rng = random.Random(3)
        first = compute_resume_delay_seconds(1, first_delay_sec=5, rng=rng)
        self.assertGreaterEqual(first, 5 * 0.85)
        later = compute_resume_delay_seconds(4, backoff_base_sec=5, backoff_cap_sec=60, rng=rng)
        self.assertLessEqual(later, 60 * 1.25)

    def test_submit_interval_jitter(self) -> None:
        rng = random.Random(4)
        values = [compute_submit_interval_ms(5000, jitter_lo=0.7, jitter_hi=1.3, rng=rng) for _ in range(40)]
        self.assertTrue(all(3500 <= v <= 6500 for v in values))

    def test_fail_cooldown_range(self) -> None:
        rng = random.Random(5)
        cools = [fail_cooldown_seconds(min_sec=1800, max_sec=5400, rng=rng) for _ in range(20)]
        self.assertTrue(all(1800 <= c <= 5400 for c in cools))


if __name__ == "__main__":
    unittest.main()
