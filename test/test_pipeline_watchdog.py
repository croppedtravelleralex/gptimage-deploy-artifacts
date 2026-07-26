"""Regression tests for the pipeline watchdog (audit 28 §A4/§A5, fixes A3-1..A3-4).

A3-3  ``ss_active`` / ``ss_queued`` were pinned to 0 forever because ``tick()``
      read ``snapshot()["pools"]`` -- a layer that has never existed. The real
      shape flattens ``ps``/``ss``/``upload``/``download``/``in_flight`` at the
      top level.
A3-4  the expected-inflight baseline counted ``RUNNING`` *or* ``TIMEOUT_PENDING``
      tasks, but a resume poll re-enters as ``RUNNING`` without ever acquiring an
      account slot and ``TIMEOUT_PENDING`` releases its slot before the status is
      written. ``expected`` was therefore permanently inflated and
      ``pipeline_inflight_drift`` was structurally guaranteed.
A3-1  both force switches were hardcoded ``False``, making
      ``reconcile_inflight``'s correction branch dead code. They are now
      config-driven, with the ``_image_inflight``-mutating one additionally gated
      on a confirmation streak.
A3-2  ``tick()``'s only caller was the ``/health`` handler with no background
      timer, so no monitoring traffic meant no watchdog at all.

Tests instantiate ``PipelineWatchdogService`` directly and bind the *real*
``AccountService.reconcile_inflight`` onto a light stub, so the correction branch
under test is production code rather than a mock of it.
"""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

from services.account_service import AccountService
from services.image_pipeline.pipeline_watchdog import (
    PipelineWatchdogService,
    _token_fingerprint,
)
from services.image_pipeline.pools import PipelinePools
from services.image_pipeline.slot_ledger import _PySlotLedger, _RustSlotLedger

LOGGER_NAME = "services.image_pipeline.pipeline_watchdog"
JOIN_TIMEOUT = 5.0

TOKEN_A = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.account-a.sig"
TOKEN_B = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.account-b.sig"


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class _FakeAccountService:
    """Minimal stand-in that reuses the real ``reconcile_inflight`` body."""

    reconcile_inflight = AccountService.reconcile_inflight
    _total_image_inflight_locked = AccountService._total_image_inflight_locked

    def __init__(self, inflight: dict[str, int], emails: dict[str, str] | None = None) -> None:
        self._image_inflight = dict(inflight)
        self._image_slot_condition = threading.Condition()
        self._lock = threading.Lock()
        self._accounts = {
            token: {"access_token": token, "email": email}
            for token, email in (emails or {}).items()
        }


class _FakeTaskService:
    """``_tasks`` / ``_active_pipeline_runs`` shaped like ImageTaskService's."""

    def __init__(self, tasks: dict[str, dict], runs: dict[str, object] | None = None) -> None:
        self._lock = threading.Lock()
        self._tasks = dict(tasks)
        self._active_pipeline_runs = dict(runs or {})

    @staticmethod
    def _is_resume_polling_task(task: dict) -> bool:
        return (
            task.get("status") == "running"
            and str(task.get("progress") or "").strip() == "resume_polling"
            and bool(str(task.get("conversation_id") or "").strip())
        )


class _BoundRun:
    def __init__(self, token: str) -> None:
        self._account_access_token = token


def _settings(**overrides) -> dict:
    base = {
        "enabled": True,
        "interval_secs": 30.0,
        "startup_delay_secs": 0.0,
        "min_tick_interval_secs": 0.0,
        "force_release_expired": True,
        "reconcile_force": True,
        "reconcile_confirm_ticks": 2,
    }
    base.update(overrides)
    return base


def _scheduler_with_pools(pools: PipelinePools) -> MagicMock:
    """Scheduler double returning the real flattened ``PipelinePools.snapshot()``."""
    scheduler = MagicMock()

    def _snapshot() -> dict:
        base = pools.snapshot()
        base["ready_buffer"] = {}
        base["segments"] = []
        return base

    scheduler.snapshot.side_effect = _snapshot
    return scheduler


def _python_ledger_facade():
    """Slot ledger facade pinned to the Python mirror, ignoring local .so builds."""
    from services.image_pipeline.slot_ledger import SlotLedgerFacade

    with patch.object(_RustSlotLedger, "_lib_path", return_value=None):
        return SlotLedgerFacade()


def _tick(
    svc: PipelineWatchdogService,
    *,
    account_service,
    task_service=None,
    scheduler=None,
    ledger=None,
    settings=None,
    **tick_kwargs,
) -> dict:
    """Drive one tick with every collaborator replaced."""
    task_service = task_service if task_service is not None else _FakeTaskService({})
    scheduler = scheduler if scheduler is not None else _scheduler_with_pools(_pools())
    ledger = ledger if ledger is not None else _python_ledger_facade()
    with patch.object(svc, "_settings", return_value=settings or _settings()):
        with patch("services.account_service.account_service", account_service):
            with patch("services.image_pipeline.image_pipeline_scheduler", scheduler):
                with patch("services.image_pipeline.pipeline_watchdog.slot_ledger", ledger):
                    with patch("services.image_task_service.image_task_service", task_service):
                        return svc.tick(**tick_kwargs)


def _pools(*, sse_slots: int = 4) -> PipelinePools:
    return PipelinePools(
        prompt_slots=4,
        sse_slots=sse_slots,
        download_concurrency=2,
        upload_concurrency=2,
    )


def _events(caplog, event: str) -> list[dict]:
    return [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == event
    ]


# ---------------------------------------------------------------------------
# 1. A3-3 -- sS pool occupancy must be real, not a hardcoded 0
# ---------------------------------------------------------------------------


def test_ss_active_and_queued_reflect_real_pool_occupancy():
    """Fails against the old ``snapshot()["pools"]`` lookup, which yielded 0/0."""
    pools = _pools(sse_slots=2)
    pools.ss.acquire("holder-1")
    pools.ss.acquire("holder-2")
    waiter = threading.Thread(target=pools.ss.acquire, args=("holder-3",), daemon=True)
    waiter.start()
    # let the third holder land in the wait queue
    deadline = time.monotonic() + JOIN_TIMEOUT
    while pools.ss.snapshot().queued < 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        scheduler=_scheduler_with_pools(pools),
    )

    assert report["ss_active"] == 2
    assert report["ss_queued"] == 1
    assert report["ss_limit"] == 2
    # the raw layer must no longer be null either (production showed pools: null)
    assert isinstance(report["pipeline_pools"], dict)
    assert report["pipeline_pools"]["ss"]["active"] == 2

    pools.ss.release(0, "holder-1")
    waiter.join(timeout=JOIN_TIMEOUT)


def test_pool_snapshots_read_flat_top_level_keys():
    svc = PipelineWatchdogService()
    flat = {
        "in_flight": 3,
        "ps": {"name": "ps", "limit": 4, "active": 1, "queued": 0},
        "ss": {"name": "ss", "limit": 10, "active": 5, "queued": 2},
        "upload": {"name": "upload", "limit": 8, "active": 0, "queued": 0},
        "download": {"name": "download", "limit": 8, "active": 0, "queued": 0},
        "ready_buffer": {},
        "segments": [],
    }
    pools = svc._pool_snapshots(flat)
    assert pools["ss"]["active"] == 5
    assert pools["in_flight"] == 3
    assert "segments" not in pools


def test_pool_snapshots_still_accept_a_nested_pools_layer():
    """Forward-compat fallback if ``snapshot()`` is ever re-nested."""
    svc = PipelineWatchdogService()
    nested = {"pools": {"ss": {"limit": 10, "active": 7, "queued": 1}, "in_flight": 2}}
    pools = svc._pool_snapshots(nested)
    assert pools["ss"]["active"] == 7
    assert pools["in_flight"] == 2


def test_ps_and_in_flight_are_reported_too():
    pools = _pools()
    pools.ps.acquire("ps-holder")
    pools.admit(100)
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        scheduler=_scheduler_with_pools(pools),
    )
    assert report["ps_active"] == 1
    assert report["pipeline_in_flight"] == 1


# ---------------------------------------------------------------------------
# 2. A3-4 -- resume-polling / timeout_pending tasks hold no account slot
# ---------------------------------------------------------------------------


def test_resume_polling_task_is_excluded_from_expected_baseline():
    """Fails against the old baseline, which counted it and forged a drift."""
    tasks = {
        "t1": {
            "status": "running",
            "progress": "resume_polling",
            "conversation_id": "conv-1",
            "resume_access_token": TOKEN_A,
        }
    }
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        task_service=_FakeTaskService(tasks),
    )

    assert report["expected_holders"] == 0
    assert report["inflight_drift"]["drift_count"] == 0
    assert report["inflight_drift"]["total_expected"] == 0
    assert report["inflight_over_count"] == {}


def test_timeout_pending_task_is_excluded_from_expected_baseline():
    """``TIMEOUT_PENDING`` gives its slot back *before* the status is written."""
    tasks = {
        "t1": {
            "status": "timeout_pending",
            "progress": "timeout_pending",
            "conversation_id": "conv-1",
            "resume_access_token": TOKEN_A,
        }
    }
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        task_service=_FakeTaskService(tasks),
    )
    assert report["expected_holders"] == 0
    assert report["inflight_drift"]["drift_count"] == 0


def test_no_phantom_drift_while_a_resume_poll_runs_alongside_a_live_task():
    """The structural-drift case: one real holder, one resumer, zero drift."""
    tasks = {
        "live": {
            "status": "running",
            "progress": "image_stream_resolve_start",
            "resume_access_token": TOKEN_A,
        },
        "resuming": {
            "status": "running",
            "progress": "resume_polling",
            "conversation_id": "conv-9",
            "resume_access_token": TOKEN_B,
        },
    }
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({TOKEN_A: 1}),
        task_service=_FakeTaskService(tasks),
    )

    assert report["expected_holders"] == 1
    assert report["inflight_drift"]["drift_count"] == 0
    assert report["inflight_over_count"] == {}
    assert report["reconcile"]["forced"] is False
    assert report["reconcile"]["reason"] == "no_over_count"


def test_running_task_is_counted_and_resolved_from_the_bound_pipeline_run():
    """Pre-conversation tasks carry no token field; the run object holds it."""
    tasks = {"live": {"status": "running", "progress": "submitting"}}
    runs = {"live": _BoundRun(TOKEN_A)}
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({TOKEN_A: 1}),
        task_service=_FakeTaskService(tasks, runs),
    )
    assert report["expected_holders"] == 1
    assert report["unidentified_holders"] == 0
    assert report["inflight_drift"]["drift_count"] == 0


def test_legacy_running_token_counts_delegates_to_the_corrected_baseline():
    tasks = {
        "live": {"status": "running", "progress": "submitting", "resume_access_token": TOKEN_A},
        "resuming": {
            "status": "running",
            "progress": "resume_polling",
            "conversation_id": "c",
            "resume_access_token": TOKEN_B,
        },
        "pending": {"status": "timeout_pending", "resume_access_token": TOKEN_B},
    }
    svc = PipelineWatchdogService()
    with patch("services.image_task_service.image_task_service", _FakeTaskService(tasks)):
        assert svc._running_token_counts() == {TOKEN_A: 1}


# ---------------------------------------------------------------------------
# 3./4. A3-1 -- a genuine leak is reported, and corrected only with the teeth on
# ---------------------------------------------------------------------------


def test_genuine_leak_is_reported_and_corrected_when_teeth_enabled(caplog):
    account_service = _FakeAccountService({TOKEN_A: 3}, emails={TOKEN_A: "leak@example.com"})
    tasks = {"live": {"status": "running", "progress": "submitting", "resume_access_token": TOKEN_A}}
    svc = PipelineWatchdogService()
    settings = _settings(reconcile_force=True, reconcile_confirm_ticks=2)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        first = _tick(
            svc,
            account_service=account_service,
            task_service=_FakeTaskService(tasks),
            settings=settings,
        )
        # tick 1 observes only: one sample cannot distinguish a leak from a race
        assert first["reconcile"]["forced"] is False
        assert first["reconcile"]["reason"] == "awaiting_confirmation"
        assert account_service._image_inflight[TOKEN_A] == 3

        second = _tick(
            svc,
            account_service=account_service,
            task_service=_FakeTaskService(tasks),
            settings=settings,
        )

    fingerprint = _token_fingerprint(TOKEN_A)
    assert second["reconcile"]["forced"] is True
    assert second["reconcile"]["reason"] == "confirmed"
    assert second["reconcile"]["corrected"] == 1
    # memory was actually rewritten down to the real holder count
    assert account_service._image_inflight[TOKEN_A] == 1

    corrections = _events(caplog, "pipeline_inflight_forced_correction")
    assert len(corrections) == 1
    assert corrections[0]["token_fp"] == fingerprint
    assert corrections[0]["email"] == "leak@example.com"
    assert corrections[0]["memory"] == 3
    assert corrections[0]["expected"] == 1
    assert corrections[0]["delta"] == 2
    assert corrections[0]["confirm_streak"] == 2
    assert _events(caplog, "pipeline_inflight_drift")


def test_leak_is_reported_but_not_corrected_when_teeth_disabled(caplog):
    account_service = _FakeAccountService({TOKEN_A: 3})
    tasks = {"live": {"status": "running", "progress": "submitting", "resume_access_token": TOKEN_A}}
    svc = PipelineWatchdogService()
    settings = _settings(reconcile_force=False, reconcile_confirm_ticks=2)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(4):
            report = _tick(
                svc,
                account_service=account_service,
                task_service=_FakeTaskService(tasks),
                settings=settings,
            )

    assert report["reconcile"]["force_enabled"] is False
    assert report["reconcile"]["forced"] is False
    assert report["reconcile"]["reason"] == "disabled_by_config"
    assert report["reconcile"]["corrected"] == 0
    # still visible ...
    assert report["inflight_drift"]["drift_count"] == 1
    assert report["inflight_over_count"][_token_fingerprint(TOKEN_A)]["delta"] == 2
    # ... but never applied
    assert account_service._image_inflight[TOKEN_A] == 3
    assert _events(caplog, "pipeline_inflight_forced_correction") == []
    assert _events(caplog, "pipeline_inflight_drift")


def test_correction_leaves_undercount_alone():
    """``memory < expected`` is reported, never written -- shrink-only by design."""
    account_service = _FakeAccountService({TOKEN_A: 1})
    tasks = {
        "a": {"status": "running", "progress": "submitting", "resume_access_token": TOKEN_A},
        "b": {"status": "running", "progress": "resolve", "resume_access_token": TOKEN_A},
    }
    svc = PipelineWatchdogService()
    for _ in range(3):
        report = _tick(
            svc,
            account_service=account_service,
            task_service=_FakeTaskService(tasks),
        )
    assert report["inflight_drift"]["drift_count"] == 1
    assert report["inflight_over_count"] == {}
    assert report["reconcile"]["forced"] is False
    assert account_service._image_inflight[TOKEN_A] == 1


def test_flapping_drift_never_accumulates_enough_confirmations():
    account_service = _FakeAccountService({TOKEN_A: 2})
    leaking = _FakeTaskService({"a": {"status": "running", "resume_access_token": TOKEN_A}})
    healthy = _FakeTaskService(
        {
            "a": {"status": "running", "resume_access_token": TOKEN_A},
            "b": {"status": "running", "resume_access_token": TOKEN_A},
        }
    )
    svc = PipelineWatchdogService()
    settings = _settings(reconcile_confirm_ticks=3)

    for task_service in (leaking, healthy, leaking, healthy, leaking):
        report = _tick(
            svc,
            account_service=account_service,
            task_service=task_service,
            settings=settings,
        )
        assert report["reconcile"]["forced"] is False

    assert account_service._image_inflight[TOKEN_A] == 2


def test_unidentifiable_holder_blocks_forced_correction():
    """A holder we cannot attribute could own the over-count -- report only."""
    account_service = _FakeAccountService({TOKEN_A: 4})
    tasks = {
        "known": {"status": "running", "resume_access_token": TOKEN_A},
        "mystery": {"status": "running", "progress": "submitting"},  # no token, no run
    }
    svc = PipelineWatchdogService()
    for _ in range(4):
        report = _tick(
            svc,
            account_service=account_service,
            task_service=_FakeTaskService(tasks),
        )

    assert report["unidentified_holders"] == 1
    assert report["reconcile"]["forced"] is False
    assert report["reconcile"]["reason"] == "unidentified_holders"
    assert account_service._image_inflight[TOKEN_A] == 4


def test_reconcile_inflight_drift_keys_no_longer_collide_across_jwt_tokens():
    """A3-1: drift keys are full-token fingerprints, so JWT accounts stay distinct.

    The old body labelled drift by ``token[:12] + "..."``. Real access tokens are
    JWTs sharing a base64 header prefix, so two distinct accounts collapsed into
    one entry and ``drift_count`` under-reported the exact leak the map exists to
    surface. ``reconcile_inflight`` now fingerprints the full token with the same
    blake2b scheme this module uses, so its keys and ``_over_counted``'s keys are
    character-for-character equal and can be cross-referenced.
    """
    account_service = _FakeAccountService({TOKEN_A: 2, TOKEN_B: 2})
    result = account_service.reconcile_inflight(expected_by_token={}, force=False)
    # The colliding prefix still exists in the raw tokens -- it just no longer
    # decides identity.
    assert TOKEN_A[:12] == TOKEN_B[:12]
    assert len(result["drift"]) == 2
    assert result["drift_count"] == 2
    assert set(result["drift"]) == {_token_fingerprint(TOKEN_A), _token_fingerprint(TOKEN_B)}
    # Anonymisation preserved: no raw token leaks into the reported map.
    assert not any(TOKEN_A in key or TOKEN_B in key for key in result["drift"])

    svc = PipelineWatchdogService()
    over = svc._over_counted(account_service, {TOKEN_A: 2, TOKEN_B: 2}, {})
    assert len(over) == 2
    assert set(over) == {_token_fingerprint(TOKEN_A), _token_fingerprint(TOKEN_B)}
    # The two maps agree on identity -- that is the point of sharing the scheme.
    assert set(over) == set(result["drift"])


def test_reconcile_inflight_drift_carries_email_for_attribution():
    account_service = _FakeAccountService({TOKEN_A: 3}, emails={TOKEN_A: "leak@example.com"})
    result = account_service.reconcile_inflight(expected_by_token={}, force=False)
    entry = result["drift"][_token_fingerprint(TOKEN_A)]
    assert entry == {"memory": 3, "expected": 0, "email": "leak@example.com"}


def test_reconcile_inflight_drift_email_is_none_when_account_unknown():
    account_service = _FakeAccountService({TOKEN_A: 1})
    result = account_service.reconcile_inflight(expected_by_token={}, force=False)
    assert result["drift"][_token_fingerprint(TOKEN_A)]["email"] is None


# ---------------------------------------------------------------------------
# 5. forced ledger release -- expired lease goes, live lease stays
# ---------------------------------------------------------------------------


def test_forced_release_drops_expired_lease_and_keeps_live_one():
    ledger = _python_ledger_facade()
    assert ledger.try_acquire_account("expired-holder", TOKEN_A, deadline_secs=0.0) is True
    assert ledger.try_acquire_account("live-holder", TOKEN_B, deadline_secs=300.0) is True
    assert ledger.try_acquire_ss("expired-ss", deadline_secs=0.0) is True
    assert ledger.try_acquire_ss("live-ss", deadline_secs=300.0) is True

    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        ledger=ledger,
        settings=_settings(force_release_expired=True),
    )

    assert report["force_release_expired"] is True
    assert report["ledger_watchdog"]["account_expired_forced"] == 1
    assert report["ledger_watchdog"]["ss_expired_forced"] == 1
    assert report["ledger_watchdog"]["account_held"] == 1
    assert report["ledger_watchdog"]["ss_held"] == 1
    # the live leases are the survivors
    assert ledger.release_account("live-holder") is True
    assert ledger.release_ss("live-ss") is True
    assert ledger.release_account("expired-holder") is False
    assert ledger.release_ss("expired-ss") is False


def test_force_release_disabled_keeps_expired_leases():
    ledger = _python_ledger_facade()
    assert ledger.try_acquire_account("expired-holder", TOKEN_A, deadline_secs=0.0) is True
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        ledger=ledger,
        settings=_settings(force_release_expired=False),
    )
    assert report["force_release_expired"] is False
    assert report["ledger_watchdog"]["account_expired_forced"] == 0
    assert report["ledger_watchdog"]["account_held"] == 1


def test_force_release_defaults_to_config_and_honours_explicit_override():
    ledger = _python_ledger_facade()
    svc = PipelineWatchdogService()
    report = _tick(
        svc,
        account_service=_FakeAccountService({}),
        ledger=ledger,
        settings=_settings(force_release_expired=True),
        force_release_expired=False,
    )
    assert report["force_release_expired"] is False


def test_forced_release_completes_without_deadlocking_the_python_ledger():
    """A0-1 guard: the ledger must not wedge when force-release is on."""
    ledger = _python_ledger_facade()
    assert ledger.try_acquire_account("expired-holder", TOKEN_A, deadline_secs=0.0) is True
    svc = PipelineWatchdogService()
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["report"] = _tick(
                svc,
                account_service=_FakeAccountService({}),
                ledger=ledger,
                settings=_settings(force_release_expired=True),
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=JOIN_TIMEOUT)
    assert not thread.is_alive(), "watchdog tick wedged on the slot ledger lock"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    assert box["report"]["ledger_watchdog"]["account_expired_forced"] == 1  # type: ignore[index]


# ---------------------------------------------------------------------------
# 6. A3-2 -- background loop ticks on its interval and stops cleanly
# ---------------------------------------------------------------------------


def test_background_loop_ticks_on_interval_and_stops_cleanly():
    svc = PipelineWatchdogService()
    ticked = threading.Event()
    calls: list[float] = []

    def _fake_tick(**_kwargs) -> dict:
        calls.append(time.monotonic())
        if len(calls) >= 3:
            ticked.set()
        return {}

    settings = _settings(interval_secs=0.02, startup_delay_secs=0.0)
    with patch.object(svc, "_settings", return_value=settings):
        with patch.object(svc, "tick", side_effect=_fake_tick):
            assert svc.loop_status()["running"] is False
            svc.start_background()
            try:
                assert ticked.wait(timeout=JOIN_TIMEOUT), f"loop ticked only {len(calls)}x"
                assert svc.loop_status()["running"] is True
            finally:
                thread = svc._thread
                svc.stop_background(timeout=JOIN_TIMEOUT)

    assert thread is not None
    assert not thread.is_alive()
    assert svc.loop_status()["running"] is False
    assert len(calls) >= 3
    # spacing is driven by the interval, not a busy spin
    assert calls[-1] - calls[0] >= 0.02


def test_start_background_is_idempotent():
    svc = PipelineWatchdogService()
    settings = _settings(interval_secs=1.0, startup_delay_secs=5.0)
    with patch.object(svc, "_settings", return_value=settings):
        svc.start_background()
        first = svc._thread
        svc.start_background()
        try:
            assert svc._thread is first
        finally:
            svc.stop_background(timeout=JOIN_TIMEOUT)
    assert first is not None and not first.is_alive()


def test_background_loop_respects_startup_delay_then_stops():
    svc = PipelineWatchdogService()
    settings = _settings(interval_secs=0.02, startup_delay_secs=30.0)
    with patch.object(svc, "_settings", return_value=settings):
        with patch.object(svc, "tick", return_value={}) as mock_tick:
            svc.start_background()
            thread = svc._thread
            svc.stop_background(timeout=JOIN_TIMEOUT)
    assert thread is not None and not thread.is_alive()
    mock_tick.assert_not_called()


def test_disabled_loop_does_not_tick_but_still_exits():
    svc = PipelineWatchdogService()
    settings = _settings(enabled=False, interval_secs=0.02, startup_delay_secs=0.0)
    with patch.object(svc, "_settings", return_value=settings):
        with patch.object(svc, "tick", return_value={}) as mock_tick:
            svc.start_background()
            time.sleep(0.1)
            thread = svc._thread
            svc.stop_background(timeout=JOIN_TIMEOUT)
    assert thread is not None and not thread.is_alive()
    mock_tick.assert_not_called()


def test_loop_survives_a_failing_tick(caplog):
    svc = PipelineWatchdogService()
    settings = _settings(interval_secs=0.02, startup_delay_secs=0.0)
    seen = threading.Event()
    calls: list[int] = []

    def _boom(**_kwargs):
        calls.append(1)
        if len(calls) >= 2:
            seen.set()
        raise RuntimeError("tick exploded")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with patch.object(svc, "_settings", return_value=settings):
            with patch.object(svc, "tick", side_effect=_boom):
                svc.start_background()
                try:
                    assert seen.wait(timeout=JOIN_TIMEOUT)
                finally:
                    thread = svc._thread
                    svc.stop_background(timeout=JOIN_TIMEOUT)

    assert thread is not None and not thread.is_alive()
    assert _events(caplog, "pipeline_watchdog_tick_error")


# ---------------------------------------------------------------------------
# 7. /health tick + background tick must not double-correct
# ---------------------------------------------------------------------------


def test_ticks_inside_the_coalescing_window_reuse_the_last_report():
    account_service = _FakeAccountService({TOKEN_A: 3})
    tasks = {"live": {"status": "running", "resume_access_token": TOKEN_A}}
    svc = PipelineWatchdogService()
    settings = _settings(interval_secs=30.0, min_tick_interval_secs=5.0)

    first = _tick(svc, account_service=account_service, task_service=_FakeTaskService(tasks), settings=settings)
    second = _tick(svc, account_service=account_service, task_service=_FakeTaskService(tasks), settings=settings)

    assert first["coalesced"] is False
    assert second["coalesced"] is True
    assert second["tick_count"] == first["tick_count"]
    # the coalesced call must not have advanced the confirmation streak
    assert svc.status()["coalesced_count"] == 1
    assert list(svc._over_count_streaks.values()) == [1]


def test_coalescing_window_never_throttles_the_loop_below_its_interval():
    svc = PipelineWatchdogService()
    settings = _settings(interval_secs=0.02, min_tick_interval_secs=5.0)
    assert svc._coalesce_window_secs(settings) == 0.01


def test_concurrent_health_and_loop_ticks_correct_at_most_once():
    """Two threads racing a confirmed leak must apply a single correction."""
    account_service = _FakeAccountService({TOKEN_A: 5})
    tasks = {"live": {"status": "running", "resume_access_token": TOKEN_A}}
    svc = PipelineWatchdogService()
    settings = _settings(reconcile_confirm_ticks=1, min_tick_interval_secs=5.0, interval_secs=30.0)

    reports: list[dict] = []
    reports_lock = threading.Lock()
    start = threading.Barrier(2)

    def _target() -> None:
        start.wait(timeout=JOIN_TIMEOUT)
        report = _tick(
            svc,
            account_service=account_service,
            task_service=_FakeTaskService(tasks),
            settings=settings,
        )
        with reports_lock:
            reports.append(report)

    threads = [threading.Thread(target=_target, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive(), "concurrent ticks deadlocked"

    assert len(reports) == 2
    assert account_service._image_inflight[TOKEN_A] == 1
    assert svc.status()["corrections_applied"] == 1
    assert sum(1 for r in reports if r.get("coalesced")) == 1


def test_tick_serialisation_holds_across_a_slow_tick():
    """The second caller must never enter the body while the first is inside."""
    svc = PipelineWatchdogService()
    inside = threading.Event()
    overlap = {"seen": False}
    depth = {"n": 0}
    depth_lock = threading.Lock()
    original = svc._tick_locked

    def _slow(*args, **kwargs):
        with depth_lock:
            depth["n"] += 1
            if depth["n"] > 1:
                overlap["seen"] = True
        inside.set()
        time.sleep(0.05)
        try:
            return original(*args, **kwargs)
        finally:
            with depth_lock:
                depth["n"] -= 1

    account_service = _FakeAccountService({})
    settings = _settings(min_tick_interval_secs=0.0, interval_secs=30.0)

    def _target() -> None:
        _tick(svc, account_service=account_service, settings=settings)

    with patch.object(svc, "_tick_locked", side_effect=_slow):
        threads = [threading.Thread(target=_target, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        assert inside.wait(timeout=JOIN_TIMEOUT)
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
            assert not thread.is_alive()

    assert overlap["seen"] is False


# ---------------------------------------------------------------------------
# status / config plumbing
# ---------------------------------------------------------------------------


def test_status_keeps_legacy_keys_and_adds_loop_state():
    svc = PipelineWatchdogService()
    _tick(svc, account_service=_FakeAccountService({}))
    status = svc.status()
    assert status["last_tick_mono"] > 0
    assert status["last_report"]["ss_active"] == 0
    assert status["tick_count"] == 1
    assert status["loop"]["running"] is False


def test_settings_come_from_config_with_expected_defaults():
    from services.config import config

    settings = config.get_image_pipeline_watchdog_settings()
    assert settings["enabled"] is True
    assert settings["force_release_expired"] is True
    assert settings["reconcile_force"] is True
    assert settings["interval_secs"] >= 1.0
    # a single confirmation sample is never enough
    assert settings["reconcile_confirm_ticks"] >= 2

    svc = PipelineWatchdogService()
    assert svc._settings()["reconcile_force"] is True


def test_confirm_ticks_is_clamped_to_at_least_two():
    from services.config import _normalize_image_pipeline_watchdog_settings

    assert _normalize_image_pipeline_watchdog_settings({"reconcile_confirm_ticks": 1})[
        "reconcile_confirm_ticks"
    ] == 2
    assert _normalize_image_pipeline_watchdog_settings({"reconcile_confirm_ticks": "junk"})[
        "reconcile_confirm_ticks"
    ] == 3


def test_settings_fall_back_when_config_raises():
    svc = PipelineWatchdogService()
    with patch("services.config.config.get_image_pipeline_watchdog_settings", side_effect=RuntimeError("boom")):
        settings = svc._settings()
    assert settings["reconcile_force"] is True
    assert settings["interval_secs"] == 30.0


def test_expected_baseline_is_empty_when_the_task_service_blows_up():
    svc = PipelineWatchdogService()
    broken = MagicMock()
    type(broken)._lock = property(lambda _self: (_ for _ in ()).throw(RuntimeError("boom")))
    with patch("services.image_task_service.image_task_service", broken):
        counts, unknown = svc._account_slot_holders()
    assert counts == {}
    assert unknown == 0


def test_token_fingerprint_is_stable_and_distinguishes_jwt_prefixes():
    assert _token_fingerprint(TOKEN_A) == _token_fingerprint(TOKEN_A)
    assert _token_fingerprint(TOKEN_A) != _token_fingerprint(TOKEN_B)
    assert len(_token_fingerprint(TOKEN_A)) == 12
    assert TOKEN_A not in _token_fingerprint(TOKEN_A)


def test_python_ledger_mirror_is_used_by_the_tick_report():
    ledger = _python_ledger_facade()
    svc = PipelineWatchdogService()
    report = _tick(svc, account_service=_FakeAccountService({}), ledger=ledger)
    assert report["slot_ledger"]["backend"] == "python"
    assert isinstance(_PySlotLedger().stats(), dict)
