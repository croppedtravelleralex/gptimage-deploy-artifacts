"""Deadlock guard hysteresis + CPU budget accounting (audit 28 §B8 / fix A4-6).

No real sleeping and no real CPU burning: the monotonic clock and
``time.process_time()`` are both driven by a fake clock, so a "sample" is just an
advance of wall time plus a chosen amount of consumed CPU time.
"""

from __future__ import annotations

from unittest import mock

import pytest

import services.image_deadlock_guard_service as guard_module
from services.image_deadlock_guard_service import (
    ImageDeadlockGuardService,
    read_cgroup_cpu_budget_vcpu,
)

SETTINGS = {
    "enabled": True,
    "cpu_budget_vcpu": 1.5,
    "cpu_budget_source": "config",
    "normal_cpu_p95": 70.0,
    "warning_cpu_p95": 80.0,
    "deadlock_cpu_threshold": 90.0,
    "sustain_seconds": 60.0,
    "recover_cpu_threshold": 65.0,
    "recover_sustain_seconds": 30.0,
    "sample_interval_sec": 2.0,
}


def _settings(**overrides) -> dict[str, object]:
    merged = dict(SETTINGS)
    merged.update(overrides)
    return merged


class FakeClock:
    """Drives both time.monotonic() and time.process_time() for the guard."""

    def __init__(self) -> None:
        self.wall = 1000.0
        self.cpu = 500.0

    def monotonic(self) -> float:
        return self.wall

    def process_time(self) -> float:
        return self.cpu

    def advance(self, *, wall_secs: float, cpu_pct: float, budget_vcpu: float = 1.5) -> None:
        """Advance `wall_secs` while consuming `cpu_pct` of `budget_vcpu`."""
        self.wall += wall_secs
        self.cpu += wall_secs * budget_vcpu * (cpu_pct / 100.0)


class GuardHarness:
    def __init__(self, settings: dict[str, object]) -> None:
        self.clock = FakeClock()
        self.settings = settings
        # Detection is bypassed via cpu_budget_source="config" by default; drop any
        # cached quota from a previous test so nothing leaks across cases.
        guard_module.cpu_budget_resolver.invalidate()
        self.guard = ImageDeadlockGuardService(
            monotonic=self.clock.monotonic,
            process_time=self.clock.process_time,
        )
        self.guard._settings = lambda: self.settings  # type: ignore[method-assign]

    def close(self) -> None:
        guard_module.cpu_budget_resolver.invalidate()

    def sample(self, cpu_pct: float, *, wall_secs: float = 2.0) -> bool:
        """Feed one sample of `cpu_pct` covering `wall_secs`, return is_tripped()."""
        budget = float(self.settings.get("cpu_budget_vcpu") or 1.5)
        self.clock.advance(wall_secs=wall_secs, cpu_pct=cpu_pct, budget_vcpu=budget)
        return self.guard.is_tripped()

    def hold(self, cpu_pct: float, *, secs: float, step: float = 2.0) -> bool:
        tripped = self.guard.is_tripped()
        remaining = secs
        while remaining > 0:
            chunk = min(step, remaining)
            tripped = self.sample(cpu_pct, wall_secs=chunk)
            remaining -= chunk
        return tripped


@pytest.fixture()
def harness():
    created: list[GuardHarness] = []

    def _make(**overrides) -> GuardHarness:
        item = GuardHarness(_settings(**overrides))
        created.append(item)
        return item

    yield _make
    for item in created:
        item.close()


# --- hysteresis -------------------------------------------------------------


def test_two_spikes_across_dead_band_do_not_trip(harness):
    """Regression for the §B8 dead band: 92% -> 10 min at 70~85% -> 92% must NOT trip.

    Against the old code the second spike evaluated `now - _above_since >= sustain`
    against a 10-minute-old timestamp and tripped on two samples.
    """
    h = harness()
    assert h.sample(92.0) is False
    # Ten minutes parked inside the (recover, threshold) dead band.
    assert h.hold(70.0, secs=300.0) is False
    assert h.hold(85.0, secs=300.0) is False
    assert h.sample(92.0) is False
    assert h.guard.status()["above_run_secs"] == pytest.approx(2.0)


def test_sustained_saturation_trips(harness):
    h = harness()
    assert h.hold(95.0, secs=58.0) is False
    assert h.sample(95.0) is True
    status = h.guard.status()
    assert status["tripped"] is True
    assert "for 60.0s" in str(status["reason"])


def test_saturation_interrupted_below_threshold_restarts_the_run(harness):
    h = harness()
    assert h.hold(95.0, secs=50.0) is False
    # One dip below the trip threshold resets the run; 50s + 20s must not trip.
    assert h.sample(80.0) is False
    assert h.hold(95.0, secs=20.0) is False
    assert h.guard.status()["above_run_secs"] == pytest.approx(20.0)


def test_tripped_guard_recovers_inside_dead_band(harness):
    """Once tripped, 66~89% must eventually clear instead of freezing forever."""
    h = harness()
    assert h.hold(95.0, secs=60.0) is True
    # 20s in the dead band is not enough (recover_sustain_seconds = 30).
    assert h.hold(80.0, secs=20.0) is True
    assert h.guard.status()["below_run_secs"] == pytest.approx(20.0)
    assert h.hold(80.0, secs=12.0) is False
    assert h.guard.status()["tripped_at"] is None


def test_tripped_guard_recovers_immediately_below_recover_threshold(harness):
    h = harness()
    assert h.hold(95.0, secs=60.0) is True
    assert h.sample(40.0) is False
    assert h.guard.status()["reason"] == ""


def test_recover_threshold_above_trip_threshold_is_clamped(harness):
    """A misconfigured recover >= threshold must not collapse the hysteresis."""
    h = harness(recover_cpu_threshold=99.0)
    assert h.hold(95.0, secs=60.0) is True
    # 95% is <= the configured recover value but also below the trip threshold;
    # clamping keeps it on the sustained-recovery path, not an instant clear.
    assert h.sample(95.0) is True


# --- staleness --------------------------------------------------------------


def test_is_tripped_not_stale_when_guard_is_disabled(harness):
    h = harness()
    assert h.hold(95.0, secs=60.0) is True
    # Same sampling window (no clock advance at all): disabling must take effect now.
    h.settings["enabled"] = False
    assert h.guard.is_tripped() is False
    assert h.guard.status()["tripped"] is False


def test_is_tripped_not_stale_when_thresholds_are_reconfigured(harness):
    h = harness()
    assert h.hold(95.0, secs=60.0) is True
    # Raise both thresholds above the last measured sample inside the sampling
    # window: the guard must re-evaluate rather than serve the cached True.
    h.settings["deadlock_cpu_threshold"] = 99.0
    h.settings["recover_cpu_threshold"] = 97.0
    assert h.guard.is_tripped() is False


def test_repeated_calls_inside_sampling_window_do_not_grow_the_run(harness):
    h = harness()
    assert h.hold(95.0, secs=58.0) is False
    for _ in range(50):
        assert h.guard.is_tripped() is False
    assert h.guard.status()["above_run_secs"] == pytest.approx(58.0)


# --- CPU budget accounting --------------------------------------------------


def test_cgroup_v2_quota_is_read(tmp_path):
    quota = tmp_path / "cpu.max"
    quota.write_text("150000 100000\n", encoding="utf-8")
    with mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(quota)):
        budget, source = read_cgroup_cpu_budget_vcpu()
    assert budget == pytest.approx(1.5)
    assert source == "cgroup_v2"


def test_cgroup_v2_unlimited_falls_through(tmp_path):
    quota = tmp_path / "cpu.max"
    quota.write_text("max 100000\n", encoding="utf-8")
    with (
        mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(quota)),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_QUOTA", str(tmp_path / "nope")),
    ):
        budget, source = read_cgroup_cpu_budget_vcpu()
    assert budget is None
    assert source == ""


def test_cgroup_v1_quota_is_read(tmp_path):
    (tmp_path / "cpu.cfs_quota_us").write_text("200000\n", encoding="utf-8")
    (tmp_path / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    with (
        mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(tmp_path / "absent")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_QUOTA", str(tmp_path / "cpu.cfs_quota_us")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_PERIOD", str(tmp_path / "cpu.cfs_period_us")),
    ):
        budget, source = read_cgroup_cpu_budget_vcpu()
    assert budget == pytest.approx(2.0)
    assert source == "cgroup_v1"


def test_missing_cgroup_files_do_not_raise(tmp_path):
    """Windows / non-container host: no crash, no misreport."""
    with (
        mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(tmp_path / "cpu.max")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_QUOTA", str(tmp_path / "quota")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_PERIOD", str(tmp_path / "period")),
    ):
        assert read_cgroup_cpu_budget_vcpu() == (None, "")


def test_guard_uses_cgroup_quota_in_auto_mode(harness, tmp_path):
    quota = tmp_path / "cpu.max"
    quota.write_text("150000 100000\n", encoding="utf-8")
    h = harness(cpu_budget_source="auto", cpu_budget_vcpu=8.0)
    guard_module.cpu_budget_resolver.invalidate()
    with mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(quota)):
        # 1.35 vCPU of CPU time over 2s wall == 90% of a 1.5 vCPU quota. The
        # hand-typed 8.0 override would have reported ~17% instead.
        h.clock.advance(wall_secs=2.0, cpu_pct=90.0, budget_vcpu=1.5)
        h.guard.is_tripped()
    status = h.guard.status()
    assert status["cpu_budget_source"] == "cgroup_v2"
    assert status["cpu_budget_vcpu"] == pytest.approx(1.5)
    assert status["current_cpu_pct"] == pytest.approx(90.0, abs=0.5)


def test_explicit_config_source_wins_over_cgroup(harness, tmp_path):
    quota = tmp_path / "cpu.max"
    quota.write_text("150000 100000\n", encoding="utf-8")
    h = harness(cpu_budget_source="config", cpu_budget_vcpu=3.0)
    with mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(quota)):
        h.clock.advance(wall_secs=2.0, cpu_pct=100.0, budget_vcpu=3.0)
        h.guard.is_tripped()
    status = h.guard.status()
    assert status["cpu_budget_source"] == "config"
    assert status["cpu_budget_vcpu"] == pytest.approx(3.0)
    assert status["current_cpu_pct"] == pytest.approx(100.0, abs=0.5)


def test_auto_mode_falls_back_to_cpu_count(harness, tmp_path):
    h = harness(cpu_budget_source="auto", cpu_budget_vcpu=1.5)
    guard_module.cpu_budget_resolver.invalidate()
    with (
        mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(tmp_path / "cpu.max")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_QUOTA", str(tmp_path / "quota")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_PERIOD", str(tmp_path / "period")),
        mock.patch.object(guard_module.os, "cpu_count", return_value=2),
    ):
        h.clock.advance(wall_secs=2.0, cpu_pct=50.0, budget_vcpu=2.0)
        h.guard.is_tripped()
    status = h.guard.status()
    assert status["cpu_budget_source"] == "os_cpu_count"
    assert status["cpu_budget_vcpu"] == pytest.approx(2.0)


def test_auto_mode_falls_back_to_configured_value(harness, tmp_path):
    h = harness(cpu_budget_source="auto", cpu_budget_vcpu=1.5)
    guard_module.cpu_budget_resolver.invalidate()
    with (
        mock.patch.object(guard_module, "_CGROUP_V2_CPU_MAX", str(tmp_path / "cpu.max")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_QUOTA", str(tmp_path / "quota")),
        mock.patch.object(guard_module, "_CGROUP_V1_CPU_PERIOD", str(tmp_path / "period")),
        mock.patch.object(guard_module.os, "cpu_count", return_value=0),
    ):
        h.clock.advance(wall_secs=2.0, cpu_pct=50.0, budget_vcpu=1.5)
        h.guard.is_tripped()
    status = h.guard.status()
    assert status["cpu_budget_source"] == "config_fallback"
    assert status["cpu_budget_vcpu"] == pytest.approx(1.5)


def test_config_normalization_exposes_new_keys():
    from services.config import config

    settings = config.get_image_deadlock_guard_settings()
    assert settings["cpu_budget_source"] in {"auto", "config"}
    assert float(settings["recover_sustain_seconds"]) >= 0.0
