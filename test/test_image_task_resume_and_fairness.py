"""Resume-ladder budget and per-owner fairness regressions (audit 28 §B7 / §A4-7).

Covered:
  A4-5  the sync resume ladder must converge strictly inside the client wait budget
          - summed worst case (first attempt + every resume attempt + backoffs) < budget
          - the wall is enforced *during* an attempt, not only at dispatch
          - a detached/async task keeps the original, unshortened ladder
          - the startup validation surfaces an inverted timeout configuration
          - every resume path still ends terminal
  A4-7  per-user knobs must actually bind, and one owner must not own the pool
          - per_user_running_max below sse_slots really lowers the ceiling
          - one owner cannot occupy every submit worker while another owner queues
          - list_task_statuses shows the limit dispatch enforces

All timing is driven by an injected clock or by the stored deadline; nothing sleeps for
longer than a slice, and anything that could hang runs in a joined thread.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import services.image_task_service as its
from services.config import config
from services.image_task_service import (
    ImageTaskService,
    TASK_STATUS_ERROR,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_TIMEOUT_PENDING,
    TERMINAL_STATUSES,
)

OWNER_A: dict[str, object] = {"id": "owner-ladder-a", "name": "A", "role": "admin"}
OWNER_B: dict[str, object] = {"id": "owner-ladder-b", "name": "B", "role": "user"}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def temp_dir():
    """Temp dir that tolerates the known Windows WinError 32 unlink flake.

    The service is always stopped inside the test body; removal stays best-effort so a
    still-mapped SQLite WAL sibling can never fail an otherwise green test.
    """
    path = Path(tempfile.mkdtemp(prefix="image-task-ladder-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def pipeline_off(monkeypatch):
    monkeypatch.setitem(config.data, "image_generation_paused", False)
    monkeypatch.setitem(config.data, "image_pipeline", {"enabled": False})
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        {
            "enabled": True,
            # Pacing would make dispatch depend on wall-clock ordering.
            "submit_start_min_interval_ms": 0,
            "submit_interval_adaptive": False,
        },
    )


def queue_settings(**overrides: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "submit_start_min_interval_ms": 0,
        "submit_interval_adaptive": False,
        **overrides,
    }


def make_service(temp_dir: Path, **kwargs: Any) -> ImageTaskService:
    """No workers are started; every test drives the dispatch functions directly."""
    return ImageTaskService(
        temp_dir / "image_tasks.json",
        generation_handler=lambda _payload: {"data": [{"url": "http://example.test/i.png"}]},
        edit_handler=lambda _payload: {"data": [{"url": "http://example.test/e.png"}]},
        retention_days_getter=lambda: 30,
        **kwargs,
    )


def seed_task(
    service: ImageTaskService,
    task_id: str,
    *,
    owner: dict[str, object] = OWNER_A,
    status: str = TASK_STATUS_QUEUED,
    payload: dict[str, Any] | None = None,
    age_secs: float = 0.0,
    **extra: Any,
) -> str:
    """Insert straight into ``_tasks``, bypassing admission guards and dedup windows."""
    key = f"{owner['id']}:{task_id}"
    ts = time.time() - float(age_secs)
    task: dict[str, Any] = {
        "id": task_id,
        "owner_id": owner["id"],
        "status": status,
        "mode": "generate",
        "model": "gpt-image-2",
        "size": None,
        "quality": "auto",
        "created_at": "2026-07-26 00:00:00",
        "updated_at": "2026-07-26 00:00:00",
        "created_ts": ts,
        "updated_ts": ts,
        "payload": {"prompt": "a cat", "n": 1, "poll_timeout_secs": 120.0, **(payload or {})},
        "identity": dict(owner),
        "resume_attempts": 0,
        "progress": "queued",
    }
    task.update(extra)
    with service._condition:
        service._tasks[key] = task
        service._save_task_locked(key)
    return key


def read_task(service: ImageTaskService, key: str) -> dict[str, Any]:
    with service._lock:
        task = service._tasks.get(key)
        if not isinstance(task, dict):
            task = service._load_task_from_db_locked(key)
        return dict(task) if isinstance(task, dict) else {}


class FakeClock:
    """Injected monotonic-ish clock. Patched onto the module's ``time.time`` only in
    tests that start no worker threads, so nothing else observes it."""

    def __init__(self, start: float | None = None):
        self.value = float(start if start is not None else time.time())

    def __call__(self) -> float:
        return self.value

    def advance(self, secs: float) -> float:
        self.value += float(secs)
        return self.value


def attach_sync_waiter(service: ImageTaskService, key: str, *, budget_secs: float, now: float) -> float:
    """Register a sync client bound exactly as ``wait_for_result`` does."""
    with service._condition:
        service._attach_sync_waiter_locked(
            key,
            wait_timeout_secs=budget_secs,
            client_deadline_ts=now + budget_secs,
        )
    return service._sync_ladder_deadline_ts(key)


# ------------------------------------------------------------------ A4-5 (§B7) ladder


def test_summed_worst_case_ladder_fits_inside_the_sync_client_budget(temp_dir, pipeline_off, monkeypatch):
    """The regression: 225/435/495s first attempt + 4 resume attempts + backoffs summed to
    ~1395s against a 540s client budget, because the wall was only checked at dispatch.

    Asserted on the *budgets the service hands out*, walked on a fake clock — never on
    wall-clock duration.
    """
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(timeout_pending_poll_secs=300, timeout_pending_max_attempts=4),
    )
    service = make_service(temp_dir)
    try:
        client_budget = service._sync_client_budget_secs()
        assert client_budget == 540.0

        clock = FakeClock()
        monkeypatch.setattr(its.time, "time", clock)

        # Multi-reference edit: the widest first attempt in the audit (495s nominal).
        payload = {"prompt": "a cat", "poll_timeout_secs": 360.0}
        key = seed_task(service, "ladder-1", payload=payload, conversation_id="conv-ladder-1")
        attach_sync_waiter(service, key, budget_secs=client_budget, now=clock.value)

        consumed = 0.0
        first_attempt = service._effective_task_hard_timeout_secs(key, payload)
        assert first_attempt > 0
        consumed += first_attempt
        clock.advance(first_attempt)

        # First attempt hard-timed out with a conversation_id captured.
        with service._condition:
            task = service._tasks[key]
            task.update(
                status=TASK_STATUS_TIMEOUT_PENDING,
                progress="timeout_pending",
                resume_timeout_secs=300.0,
                next_resume_ts=clock.value,
            )

        attempts_dispatched = 0
        for _ in range(20):
            with service._condition:
                run_args = service._next_poll_task_locked()
            if run_args is None:
                break
            attempts_dispatched += 1
            attempt_hard = service._effective_resume_poll_hard_timeout_secs(key, run_args[2])
            consumed += attempt_hard
            clock.advance(attempt_hard)
            # This attempt timed out; account the backoff before the next dispatch.
            with service._condition:
                task = service._tasks.get(key) or {}
                attempts = int(task.get("resume_attempts") or 0)
                backoff = service._resume_delay_secs(attempts)
                consumed += backoff
                clock.advance(backoff)
                if _clean_status(task) not in TERMINAL_STATUSES:
                    task.update(
                        status=TASK_STATUS_TIMEOUT_PENDING,
                        progress="timeout_pending",
                        next_resume_ts=clock.value,
                    )

        assert consumed < client_budget, (
            f"ladder consumed {consumed:.1f}s against a {client_budget:.1f}s client budget "
            f"({attempts_dispatched} resume attempts)"
        )
        # Sanity: the ladder is not degenerate — it still used most of its own budget.
        assert consumed >= service._sync_ladder_budget_secs(client_budget) * 0.5
        assert _clean_status(read_task(service, key)) in TERMINAL_STATUSES
    finally:
        service.stop()


def _clean_status(task: dict[str, Any]) -> str:
    return str(task.get("status") or "")


def test_ladder_budget_is_derived_from_the_client_budget(temp_dir, pipeline_off, monkeypatch):
    """Retuning newapi_image_sync_wait_timeout_secs must move the whole ladder with it."""
    service = make_service(temp_dir)
    try:
        seen: list[tuple[float, float]] = []
        for budget in (120.0, 300.0, 540.0, 900.0):
            monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", budget)
            ladder = service._sync_ladder_budget_secs()
            assert ladder < budget, "the ladder must never be allowed the whole client budget"
            seen.append((budget, ladder))
        # Monotone in the client budget: no hardcoded plateau.
        assert [item[1] for item in seen] == sorted(item[1] for item in seen)
        assert len({item[1] for item in seen}) == len(seen)
    finally:
        service.stop()


def test_resume_wall_is_enforced_mid_attempt_not_only_at_dispatch(temp_dir, pipeline_off, monkeypatch):
    """An attempt dispatched just inside the deadline used to run its full hard timeout.

    Pre-fix this waited 360s (300s poll + 60s margin) and re-armed another attempt; the
    joined thread below is what keeps that from hanging the suite.
    """
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(timeout_pending_poll_secs=300, timeout_pending_max_attempts=4),
    )
    service = make_service(temp_dir)
    blocked = threading.Event()
    try:
        key = seed_task(
            service,
            "midflight-1",
            status=TASK_STATUS_RUNNING,
            progress="resume_polling",
            conversation_id="conv-midflight",
            resume_timeout_secs=300.0,
            resume_attempts=1,
        )
        # Client budget has 0.4s left: the attempt itself must be cut, not the next one.
        with service._condition:
            service._tasks[key]["sync_ladder_deadline_ts"] = time.time() + 0.4

        def never_returns(*_args: Any, **_kwargs: Any) -> None:
            blocked.wait(timeout=30.0)

        service._run_resume_poll = never_returns  # type: ignore[method-assign]

        natural = service._resume_poll_hard_timeout_secs(300.0)
        assert natural >= 300.0, "the natural bound must be far beyond the deadline"

        worker = threading.Thread(
            target=service._run_resume_poll_with_hard_timeout,
            args=(key, "conv-midflight", 300.0, dict(OWNER_A), "generate", "gpt-image-2"),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=20.0)
        assert not worker.is_alive(), "the attempt ran past the ladder deadline"

        task = read_task(service, key)
        assert task["status"] == TASK_STATUS_ERROR
        assert task["status"] in TERMINAL_STATUSES
        assert "deadline" in str(task.get("error"))
    finally:
        blocked.set()
        service.stop()


def test_first_attempt_is_bounded_by_a_late_attaching_sync_waiter(temp_dir, pipeline_off, monkeypatch):
    """`_wait_for_runner` re-reads the deadline in slices, so a waiter that attaches after
    dispatch still bounds the attempt already in flight."""
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    service = make_service(temp_dir)
    try:
        key = seed_task(service, "late-attach", status=TASK_STATUS_RUNNING)
        finished = threading.Event()
        granted: list[float] = []

        def run() -> None:
            granted.extend(service._wait_for_runner(key, finished, 600.0))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        # Attach mid-flight with almost no budget left.
        with service._condition:
            service._tasks[key]["sync_ladder_deadline_ts"] = time.time() + 0.2
        worker.join(timeout=20.0)
        assert not worker.is_alive()
        assert granted[0] is False, "the runner never finished, so it must report a timeout"
        assert granted[1] < 600.0, "the granted budget must have been cut to the deadline"
    finally:
        finished.set()
        service.stop()


def test_detached_async_task_keeps_the_full_ladder(temp_dir, pipeline_off, monkeypatch):
    """An async submitter never calls wait_for_result, so it has no client bound.

    The shortened ladder must apply only to sync-attached tasks; the async sibling in the
    same service keeps its full per-mode timeout and full resume budget.
    """
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(timeout_pending_poll_secs=300, timeout_pending_max_attempts=4),
    )
    service = make_service(temp_dir)
    try:
        payload = {"prompt": "a cat", "poll_timeout_secs": 360.0}
        async_key = seed_task(
            service,
            "async-1",
            status=TASK_STATUS_TIMEOUT_PENDING,
            progress="timeout_pending",
            payload=payload,
            conversation_id="conv-async",
            resume_timeout_secs=300.0,
            next_resume_ts=time.time() - 1.0,
        )
        sync_key = seed_task(
            service,
            "sync-1",
            owner=OWNER_B,
            status=TASK_STATUS_TIMEOUT_PENDING,
            progress="timeout_pending",
            payload=payload,
            conversation_id="conv-sync",
            resume_timeout_secs=300.0,
            next_resume_ts=time.time() - 1.0,
        )
        # Only the sync one has a client bound, and only 90s of it left.
        with service._condition:
            service._tasks[sync_key]["sync_ladder_deadline_ts"] = time.time() + 90.0

        assert service._sync_ladder_remaining_secs(async_key) is None
        assert service._effective_task_hard_timeout_secs(async_key, payload) == pytest.approx(
            service._task_hard_timeout_secs(payload)
        )
        assert service._effective_resume_poll_hard_timeout_secs(async_key, 300.0) == pytest.approx(
            service._resume_poll_hard_timeout_secs(300.0)
        )

        dispatched: dict[str, float] = {}
        for _ in range(4):
            with service._condition:
                run_args = service._next_poll_task_locked()
            if run_args is None:
                break
            dispatched[run_args[0]] = run_args[2]
            with service._condition:
                service._tasks[run_args[0]].update(
                    status=TASK_STATUS_TIMEOUT_PENDING,
                    progress="timeout_pending",
                    next_resume_ts=time.time() + 3600.0,
                )

        assert dispatched[async_key] == pytest.approx(300.0), "async poll budget must not be clamped"
        assert dispatched[sync_key] < 300.0, "sync poll budget must be clamped to the client bound"

        # And the wall itself: the async task keeps the plain resume wall.
        async_deadline = float(read_task(service, async_key)["resume_deadline_ts"])
        assert async_deadline > time.time() + (service._resume_wall_secs() - 30.0)
        sync_deadline = float(read_task(service, sync_key)["resume_deadline_ts"])
        assert sync_deadline <= service._sync_ladder_deadline_ts(sync_key) + 0.01
    finally:
        service.stop()


def test_short_polling_wait_does_not_bind_the_ladder(temp_dir, pipeline_off, monkeypatch):
    """``wait_for_result`` doubles as a polling read whose contract is "the task keeps
    running in the background", so a 5s poll must not become a 5s server deadline.

    Only a budget that can hold the reserve plus one useful attempt binds — which the real
    sync path always can, because ``newapi_image_sync_wait_timeout_secs`` clamps to >=60s.
    """
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    service = make_service(temp_dir)
    try:
        key = seed_task(service, "poll-read", status=TASK_STATUS_RUNNING)
        with service._condition:
            service._attach_sync_waiter_locked(
                key, wait_timeout_secs=5.0, client_deadline_ts=time.time() + 5.0
            )
        assert service._sync_ladder_remaining_secs(key) is None

        with service._condition:
            service._attach_sync_waiter_locked(
                key, wait_timeout_secs=540.0, client_deadline_ts=time.time() + 540.0
            )
        remaining = service._sync_ladder_remaining_secs(key)
        assert remaining is not None and remaining > 0
        assert remaining < 540.0
    finally:
        service.stop()


def test_earliest_sync_waiter_wins(temp_dir, pipeline_off, monkeypatch):
    """Two callers can await the same task_id; the ladder must fit the tightest bound."""
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    service = make_service(temp_dir)
    try:
        key = seed_task(service, "two-waiters", status=TASK_STATUS_RUNNING)
        now = time.time()
        with service._condition:
            service._attach_sync_waiter_locked(key, wait_timeout_secs=540.0, client_deadline_ts=now + 540.0)
            wide = service._sync_ladder_deadline_ts(key)
            service._attach_sync_waiter_locked(key, wait_timeout_secs=120.0, client_deadline_ts=now + 120.0)
            tight = service._sync_ladder_deadline_ts(key)
            # A later, wider waiter must not push the deadline back out.
            service._attach_sync_waiter_locked(key, wait_timeout_secs=900.0, client_deadline_ts=now + 900.0)
            after = service._sync_ladder_deadline_ts(key)
        assert tight < wide
        assert after == tight
    finally:
        service.stop()


def test_config_validation_surfaces_an_inverted_ladder(temp_dir, pipeline_off, monkeypatch):
    """A silently-violated invariant is how the 720/1395s ladder arose in the first place."""
    monkeypatch.setitem(config.data, "image_poll_timeout_secs", 30)
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(
            timeout_pending_poll_secs=5,
            timeout_pending_max_attempts=1,
            generation_poll_timeout_secs=30,
            edit_poll_timeout_secs=30,
            multi_reference_poll_timeout_secs=30,
            pre_conversation_timeout_secs=30,
        ),
    )
    service = make_service(temp_dir)
    try:
        monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 900.0)
        healthy = service.validate_sync_ladder_budget()
        assert healthy["inverted"] is False, healthy["detail"]
        assert healthy["inverted_modes"] == []
        assert healthy["ladder_budget_secs"] < healthy["client_budget_secs"]

        # Same server-side ladder, client budget cut below it.
        monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 60.0)
        inverted = service.validate_sync_ladder_budget()
        assert inverted["inverted"] is True
        assert set(inverted["inverted_modes"]) == {"generate", "edit", "multi_reference"}
        for mode in inverted["modes"].values():
            assert mode["overflow_secs"] > 0
            # Whatever the nominal config says, the enforced ladder still fits.
            assert mode["enforced_total_secs"] <= inverted["ladder_budget_secs"]
        assert "exceeds the sync client budget" in inverted["detail"]

        # Re-widening the client budget clears it again: the check tracks config.
        monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 900.0)
        assert service.validate_sync_ladder_budget()["inverted"] is False
    finally:
        service.stop()


def test_startup_validation_logs_the_inversion_once(temp_dir, pipeline_off, monkeypatch):
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 60.0)
    service = make_service(temp_dir)
    try:
        with mock.patch.object(its.log_service, "add") as add:
            with service._condition:
                service._log_sync_ladder_validation_locked()
                service._log_sync_ladder_validation_locked()
        assert add.call_count == 1
        detail = add.call_args.args[2]
        assert detail["inverted"] is True
        assert detail["client_budget_secs"] == 60.0
    finally:
        service.stop()


def test_live_default_config_ladder_is_reported_as_inverted(temp_dir, pipeline_off):
    """Documents the audited state: today's shipped timeouts *are* inverted, and the
    clamp — not the config — is what keeps the ladder inside the client budget."""
    service = make_service(temp_dir)
    try:
        report = service.validate_sync_ladder_budget()
        assert report["client_budget_secs"] == 540.0
        assert report["inverted"] is True
        for mode in report["modes"].values():
            assert mode["nominal_total_secs"] > report["client_budget_secs"]
            assert mode["enforced_total_secs"] < report["client_budget_secs"]
    finally:
        service.stop()


@pytest.mark.parametrize(
    "name,overrides",
    [
        ("no_conversation_id", {"conversation_id": ""}),
        ("wall_already_passed", {"resume_deadline_ts": 1.0}),
        ("attempts_exhausted", {"resume_attempts": 99}),
        ("backoff_past_the_wall", {"next_resume_ts": time.time() + 10_000.0}),
        ("budget_below_min_attempt", {}),
    ],
)
def test_every_resume_path_reaches_a_terminal_status(temp_dir, pipeline_off, monkeypatch, name, overrides):
    """No shape may be left non-terminal: a stuck timeout_pending row keeps consuming
    per-owner and global capacity forever (only a removed poll worker used to clear it)."""
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(timeout_pending_poll_secs=300, timeout_pending_max_attempts=2),
    )
    service = make_service(temp_dir)
    try:
        fields: dict[str, Any] = {
            "conversation_id": "conv-terminal",
            "resume_timeout_secs": 300.0,
            "next_resume_ts": time.time() - 1.0,
            **overrides,
        }
        key = seed_task(
            service,
            f"terminal-{name}",
            status=TASK_STATUS_TIMEOUT_PENDING,
            progress="timeout_pending",
            **fields,
        )
        with service._condition:
            # Every shape is sync-attached; "budget_below_min_attempt" has 1s left.
            remaining = 1.0 if name == "budget_below_min_attempt" else 400.0
            service._tasks[key]["sync_ladder_deadline_ts"] = time.time() + remaining

        for _ in range(8):
            with service._condition:
                run_args = service._next_poll_task_locked()
            if run_args is None:
                break
            # Simulate the attempt exhausting its budget without a result.
            with service._condition:
                service._tasks[run_args[0]].update(
                    status=TASK_STATUS_TIMEOUT_PENDING,
                    progress="timeout_pending",
                    next_resume_ts=time.time() - 1.0,
                )

        task = read_task(service, key)
        assert task["status"] in TERMINAL_STATUSES, f"{name} left the task in {task.get('status')!r}"
        assert task.get("error")
    finally:
        service.stop()


def test_budget_starved_sync_task_dispatches_no_further_upstream_attempt(temp_dir, pipeline_off, monkeypatch):
    """The point of A4-5 is the *quota*, not just the eventual terminal status.

    Pre-fix a task whose client had ~1s of budget left still had its remaining resume
    attempts dispatched — each one a conversation GET on the same ``resume_access_token``
    for a response nobody could receive. The parametrised test above only proves the row
    ends terminal; this one proves it ends terminal *without* spending another attempt.
    """
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(timeout_pending_poll_secs=300, timeout_pending_max_attempts=4),
    )
    service = make_service(temp_dir)
    try:
        key = seed_task(
            service,
            "starved",
            status=TASK_STATUS_TIMEOUT_PENDING,
            progress="timeout_pending",
            conversation_id="conv-starved",
            resume_timeout_secs=300.0,
            next_resume_ts=time.time() - 1.0,
        )
        with service._condition:
            # Inside the resume wall, but under one useful attempt of client budget.
            service._tasks[key]["sync_ladder_deadline_ts"] = time.time() + 1.0

        dispatched = 0
        for _ in range(8):
            with service._condition:
                run_args = service._next_poll_task_locked()
            if run_args is None:
                break
            dispatched += 1
            with service._condition:
                service._tasks[run_args[0]].update(
                    status=TASK_STATUS_TIMEOUT_PENDING,
                    progress="timeout_pending",
                    next_resume_ts=time.time() - 1.0,
                )

        assert dispatched == 0, f"{dispatched} upstream resume attempt(s) dispatched after the client gave up"
        task = read_task(service, key)
        assert task["status"] == TASK_STATUS_ERROR
        assert "预算不足" in str(task.get("error"))
    finally:
        service.stop()


def test_resume_poll_success_still_terminalises(temp_dir, pipeline_off, monkeypatch):
    """The happy resume path must not be collateral damage of the budget clamp."""
    monkeypatch.setitem(config.data, "newapi_image_sync_wait_timeout_secs", 540.0)
    service = make_service(temp_dir)
    try:
        key = seed_task(
            service,
            "resume-success",
            status=TASK_STATUS_RUNNING,
            progress="resume_polling",
            conversation_id="conv-ok",
            resume_timeout_secs=30.0,
            resume_attempts=1,
        )
        with service._condition:
            service._tasks[key]["sync_ladder_deadline_ts"] = time.time() + 300.0

        backend = mock.MagicMock()
        backend._poll_image_results.return_value = (["file-1"], [])
        backend.resolve_conversation_image_urls.return_value = ["http://example.test/x.png"]
        backend.download_image_bytes.return_value = [b"png-bytes"]

        with mock.patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend):
            worker = threading.Thread(
                target=service._run_resume_poll_with_hard_timeout,
                args=(key, "conv-ok", 30.0, dict(OWNER_A), "generate", "gpt-image-2"),
                daemon=True,
            )
            worker.start()
            worker.join(timeout=20.0)
        assert not worker.is_alive()

        task = read_task(service, key)
        assert task["status"] == TASK_STATUS_SUCCESS
        assert task["data"]
    finally:
        service.stop()


def test_queued_task_whose_sync_client_gave_up_is_not_started(temp_dir, pipeline_off):
    """Dispatching it would spend an account slot and quota on an undeliverable result."""
    service = make_service(temp_dir)
    try:
        # The stale one is the oldest, so plain FIFO reaches it first.
        stale = seed_task(service, "stale-queued", age_secs=10.0)
        fresh = seed_task(service, "fresh-queued", age_secs=1.0)
        with service._condition:
            service._tasks[stale]["sync_ladder_deadline_ts"] = time.time() - 5.0
            service._tasks[fresh]["sync_ladder_deadline_ts"] = time.time() + 300.0
            run_args = service._next_submit_task_locked()

        assert run_args is not None
        assert run_args[0] == fresh
        assert read_task(service, stale)["status"] == TASK_STATUS_ERROR
    finally:
        service.stop()


# --------------------------------------------------------- A4-7 per-owner fairness


def test_per_user_running_max_below_sse_slots_lowers_the_ceiling(temp_dir, monkeypatch):
    """`relaxed_per_user_running` returned before base/burst were consulted, so it always
    won with `sse_slots` (10) and dead-configured every per-user knob."""
    monkeypatch.setitem(config.data, "image_generation_paused", False)
    monkeypatch.setitem(
        config.data,
        "image_pipeline",
        {"enabled": True, "sse_slots": 10, "prompt_slots": 10, "relaxed_per_user_running": True},
    )
    monkeypatch.setitem(
        config.data,
        "image_task_queue",
        queue_settings(
            submit_workers=10,
            per_user_running_max=3,
            per_user_running_base=3,
            burst_enabled=False,
        ),
    )
    service = make_service(temp_dir)
    try:
        assert config.get_image_pipeline_settings()["sse_slots"] == 10
        assert service._effective_per_user_running_max_locked() == 3
        assert service._owner_running_cap_locked(str(OWNER_A["id"])) == 3

        with mock.patch.object(type(service), "_warm_account_lease_pool_locked", return_value=None):
            for index in range(6):
                seed_task(service, f"cap-{index}", age_secs=6 - index)
            started = 0
            for _ in range(10):
                with service._condition:
                    if service._next_submit_task_locked() is None:
                        break
                started += 1
        assert started == 3, "dispatch ignored per_user_running_max and used sse_slots"
    finally:
        service.stop()


def test_relaxed_per_user_running_still_applies_when_nothing_is_configured(temp_dir, monkeypatch):
    """Backward compatibility: an operator who never set the knobs keeps the old ceiling."""
    monkeypatch.setitem(config.data, "image_generation_paused", False)
    monkeypatch.setitem(
        config.data,
        "image_pipeline",
        {"enabled": True, "sse_slots": 9, "prompt_slots": 9, "relaxed_per_user_running": True},
    )
    monkeypatch.setitem(config.data, "image_task_queue", queue_settings())
    service = make_service(temp_dir)
    try:
        assert service._explicit_per_user_running_configured() is False
        assert service._effective_per_user_running_max_locked() == 9

        monkeypatch.setitem(
            config.data,
            "image_pipeline",
            {"enabled": True, "sse_slots": 9, "prompt_slots": 9, "relaxed_per_user_running": False},
        )
        # Valve off → falls back to the normalised base, not to sse_slots.
        assert service._effective_per_user_running_max_locked() == int(
            config.get_image_task_queue_settings()["per_user_running_base"]
        )
    finally:
        service.stop()


def test_one_owner_cannot_occupy_every_submit_worker(temp_dir, pipeline_off):
    """`_run_task` holds its submit worker for the whole task, so a per-user ceiling equal
    to `submit_workers` let one owner starve everyone else completely.

    Pre-fix the per-user ceiling was `sse_slots`/`per_user_running_max` = the whole pool,
    and dispatch was global FIFO by created_ts, so A's older backlog took all 4 workers
    and B waited up to ~495s per task for one to free up.
    """
    workers = 4
    service = make_service(
        temp_dir,
        submit_workers_getter=lambda: workers,
        poll_workers_getter=lambda: 0,
        per_user_running_max_getter=lambda: 10,
    )
    try:
        for index in range(6):
            seed_task(service, f"greedy-{index}", owner=OWNER_A, age_secs=100 - index)
        for index in range(2):
            seed_task(service, f"polite-{index}", owner=OWNER_B, age_secs=2 - index)

        with service._condition:
            cap = service._owner_running_cap_locked(str(OWNER_A["id"]))
        assert cap < workers, "the per-owner ceiling is still coupled to the pool size"

        # Only `workers` dispatches can be in flight at once — each one costs the worker
        # that made the call, so the loop is bounded by the pool, not by the queue.
        dispatched: list[str] = []
        for _ in range(workers):
            with service._condition:
                run_args = service._next_submit_task_locked()
            if run_args is None:
                break
            dispatched.append(run_args[0])

        a_running = sum(1 for key in dispatched if key.startswith(str(OWNER_A["id"])))
        b_running = sum(1 for key in dispatched if key.startswith(str(OWNER_B["id"])))
        assert a_running < workers, f"owner A took {a_running}/{workers} submit workers"
        assert b_running >= 1, "owner B never got a worker while A had a long queue"
        assert a_running <= cap
    finally:
        service.stop()


def test_single_owner_keeps_the_full_configured_ceiling(temp_dir, pipeline_off):
    """No contention → no fair-share cap, so single-tenant throughput is untouched."""
    service = make_service(
        temp_dir,
        submit_workers_getter=lambda: 4,
        per_user_running_max_getter=lambda: 4,
    )
    try:
        for index in range(6):
            seed_task(service, f"solo-{index}", age_secs=6 - index)
        with service._condition:
            assert service._owner_running_cap_locked(str(OWNER_A["id"])) == 4
        started = 0
        for _ in range(8):
            with service._condition:
                if service._next_submit_task_locked() is None:
                    break
            started += 1
        assert started == 4
    finally:
        service.stop()


def test_dispatch_prefers_the_least_served_owner(temp_dir, pipeline_off):
    """Global FIFO by created_ts let one owner's backlog hold the head of the line."""
    service = make_service(
        temp_dir,
        submit_workers_getter=lambda: 4,
        per_user_running_max_getter=lambda: 4,
    )
    try:
        # Owner A is already running one task and owns the two oldest queued rows.
        seed_task(service, "a-running", owner=OWNER_A, status=TASK_STATUS_RUNNING, progress="submitting")
        seed_task(service, "a-old-1", owner=OWNER_A, age_secs=500.0)
        seed_task(service, "a-old-2", owner=OWNER_A, age_secs=400.0)
        seed_task(service, "b-new", owner=OWNER_B, age_secs=1.0)

        with service._condition:
            run_args = service._next_submit_task_locked()
        assert run_args is not None
        assert run_args[0].startswith(str(OWNER_B["id"])), "the newcomer waited behind A's backlog"
    finally:
        service.stop()


def test_status_running_limit_matches_what_dispatch_enforces(temp_dir, pipeline_off):
    """The UI read per_user_running_max_getter() while dispatch used sse_slots."""
    workers = 4
    service = make_service(
        temp_dir,
        submit_workers_getter=lambda: workers,
        per_user_running_max_getter=lambda: 10,
    )
    try:
        for index in range(5):
            seed_task(service, f"ui-{index}", owner=OWNER_A, age_secs=10 - index)
        for index in range(3):
            seed_task(service, f"ui-other-{index}", owner=OWNER_B, age_secs=3 - index)

        items = service.list_task_statuses(OWNER_A, ["ui-0"])["items"]
        with service._condition:
            enforced = service._owner_running_cap_locked(str(OWNER_A["id"]))
        assert items[0]["running_limit"] == enforced

        started = 0
        for _ in range(workers):
            with service._condition:
                run_args = service._next_submit_task_locked()
            if run_args is None:
                break
            if run_args[0].startswith(str(OWNER_A["id"])):
                started += 1
        assert started <= items[0]["running_limit"], "dispatch ran more than the UI promised"
    finally:
        service.stop()


def test_status_running_limit_tracks_the_configured_ceiling(temp_dir, pipeline_off):
    """With no contention the displayed limit is the configured one, not the pool size."""
    service = make_service(temp_dir, submit_workers_getter=lambda: 8, per_user_running_max_getter=lambda: 2)
    try:
        seed_task(service, "single-0", owner=OWNER_A)
        items = service.list_task_statuses(OWNER_A, ["single-0"])["items"]
        assert items[0]["running_limit"] == 2
    finally:
        service.stop()
