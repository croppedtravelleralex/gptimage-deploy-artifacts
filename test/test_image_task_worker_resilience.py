"""Worker/pool resilience regressions from audit 28 §B9 / §B10 (backlog A3-5, A3-6).

Covered:
  A3-5  PipelinePools._in_flight must be given back on every path
          - finish() raising before _pools.finish() still decrements
          - begin_run() raising after admit() rolls the permit back
          - finish() is idempotent (no double-decrement)
  A3-6  a worker-loop escape must terminalise the *task*, not the worker thread
          - non-RuntimeError from begin_run / _run_task
          - the explicit re-raise for a non-"queue is full" RuntimeError
          - the same for _poll_worker_loop
          - RUNNING tasks past their hard-timeout bound are reaped, and the reap
            releases the account slot + pipeline accounting, not just the status
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

from services.config import config
from services.image_pipeline.orchestrator import ImagePipelineScheduler
from services.image_task_service import (
    ImageTaskService,
    TASK_STATUS_ERROR,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_TIMEOUT_PENDING,
    TERMINAL_STATUSES,
)

OWNER: dict[str, object] = {"id": "owner-resilience", "name": "Owner", "role": "admin"}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def temp_dir():
    """Temp dir that tolerates the known Windows WinError 32 unlink flake.

    SQLite WAL/SHM siblings can still be mapped when the service's daemon threads are
    winding down, so removal is best-effort — a leaked temp dir must never fail a test.
    """
    path = Path(tempfile.mkdtemp(prefix="image-task-resilience-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def pipeline_off(monkeypatch):
    monkeypatch.setitem(config.data, "image_generation_paused", False)
    monkeypatch.setitem(config.data, "image_task_queue", {"enabled": True})
    monkeypatch.setitem(config.data, "image_pipeline", {"enabled": False})


@pytest.fixture()
def pipeline_on(monkeypatch):
    monkeypatch.setitem(config.data, "image_generation_paused", False)
    monkeypatch.setitem(config.data, "image_task_queue", {"enabled": True})
    monkeypatch.setitem(
        config.data,
        "image_pipeline",
        {
            "enabled": True,
            "sse_slots": 2,
            "prompt_slots": 2,
            # Keeps the lease-pool warm-up from touching the real account pool.
            "account_lease_prewarm_enabled": False,
        },
    )


def make_service(temp_dir: Path, **kwargs: Any) -> ImageTaskService:
    return ImageTaskService(
        temp_dir / "image_tasks.json",
        generation_handler=lambda _payload: {"data": [{"url": "http://example.test/i.png"}]},
        edit_handler=lambda _payload: {"data": [{"url": "http://example.test/e.png"}]},
        retention_days_getter=lambda: 30,
        submit_workers_getter=kwargs.pop("submit_workers_getter", lambda: 1),
        poll_workers_getter=kwargs.pop("poll_workers_getter", lambda: 1),
        **kwargs,
    )


def seed_task(
    service: ImageTaskService,
    task_id: str,
    *,
    status: str = TASK_STATUS_QUEUED,
    payload: dict[str, Any] | None = None,
    age_secs: float = 0.0,
    **extra: Any,
) -> str:
    """Insert a task straight into the service, bypassing admission checks.

    ``_submit`` pulls in dispatchable-pool guards and dedup windows that are irrelevant
    here; the worker loops only ever read ``self._tasks``.
    """
    key = f"{OWNER['id']}:{task_id}"
    ts = time.time() - float(age_secs)
    task: dict[str, Any] = {
        "id": task_id,
        "owner_id": OWNER["id"],
        "status": status,
        "mode": "generate",
        "model": "gpt-image-2",
        "size": None,
        "quality": "auto",
        "created_at": "2026-07-26 00:00:00",
        "updated_at": "2026-07-26 00:00:00",
        "created_ts": ts,
        "updated_ts": ts,
        "payload": {
            "prompt": "a cat",
            "n": 1,
            "poll_timeout_secs": 120.0,
            # Keeps a real run short if one ever starts; the reaper margin is derived
            # from this value, so it stays well above any test wall clock.
            "task_hard_timeout_secs": 0.5,
            **(payload or {}),
        },
        "identity": dict(OWNER),
        "resume_attempts": 0,
        "progress": "queued",
    }
    task.update(extra)
    with service._condition:
        service._tasks[key] = task
        service._save_task_locked(key)
        service._condition.notify_all()
    return key


def read_task(service: ImageTaskService, key: str) -> dict[str, Any]:
    """Read a task from memory, falling back to the DB.

    ``_log_call`` evicts terminal tasks from ``self._tasks`` once it has logged them, so
    an in-memory-only read races the success path.
    """
    with service._lock:
        task = service._tasks.get(key)
        if not isinstance(task, dict):
            task = service._load_task_from_db_locked(key)
        return dict(task) if isinstance(task, dict) else {}


def wait_for_status(service: ImageTaskService, key: str, status: str, timeout: float = 8.0) -> dict[str, Any]:
    """Poll until ``status`` — deadline-bounded, so it cannot hang."""
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = read_task(service, key)
        if str(last.get("status") or "") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {key} never reached {status}; last={last.get('status')!r} error={last.get('error')!r}")


def new_scheduler_with_pools() -> tuple[ImagePipelineScheduler, Any]:
    scheduler = ImagePipelineScheduler()
    return scheduler, scheduler.pools()


# ------------------------------------------------------------------- A3-5 (§B9)


def test_finish_decrements_in_flight_even_when_it_raises_early(pipeline_on):
    """An exception before ``_pools.finish()`` used to leak an admission permit forever.

    ``_finalize_pipeline_run`` swallows the exception, so the leak was silent; after
    ``global_queue_max`` leaks every task fails with "image pipeline global queue is
    full" and nothing reconciles the counter.
    """
    from services.image_pipeline import orchestrator as orch

    scheduler, pools = new_scheduler_with_pools()
    run = scheduler.begin_run(task_key="leak-1", mode="generate", payload={"prompt": "a cat"})
    assert pools._in_flight == 1

    with mock.patch.object(orch.schedule_trace, "emit", side_effect=RuntimeError("trace blew up")):
        with pytest.raises(RuntimeError, match="trace blew up"):
            run.finish()

    assert pools._in_flight == 0, "finish() must give the admission permit back on the exception path"


def test_finish_decrements_in_flight_when_sweep_raises(pipeline_on):
    scheduler, pools = new_scheduler_with_pools()
    run = scheduler.begin_run(task_key="leak-2", mode="generate", payload={"prompt": "a cat"})
    assert pools._in_flight == 1

    with mock.patch.object(type(run), "sweep_leaked_slots", side_effect=RuntimeError("sweep blew up")):
        with pytest.raises(RuntimeError, match="sweep blew up"):
            run.finish()

    assert pools._in_flight == 0


def test_begin_run_rolls_back_admit_on_bad_n(pipeline_on):
    """``int(payload.get("n") or 1)`` raises ValueError *after* admit() incremented."""
    scheduler, pools = new_scheduler_with_pools()

    with pytest.raises(ValueError):
        scheduler.begin_run(task_key="leak-3", mode="generate", payload={"prompt": "a cat", "n": "four"})

    assert pools._in_flight == 0, "a failed begin_run must not consume a global queue slot"


def test_begin_run_rolls_back_admit_on_trace_failure(pipeline_on):
    from services.image_pipeline import orchestrator as orch

    scheduler, pools = new_scheduler_with_pools()

    with mock.patch.object(orch.schedule_trace, "enabled", return_value=True), \
            mock.patch.object(orch.schedule_trace, "emit", side_effect=RuntimeError("trace blew up")):
        with pytest.raises(RuntimeError, match="trace blew up"):
            scheduler.begin_run(task_key="leak-4", mode="generate", payload={"prompt": "a cat"})

    assert pools._in_flight == 0


def test_begin_run_leak_exhausts_global_queue_without_rollback(pipeline_on, monkeypatch):
    """The end state the audit predicted: leaks accumulate into a permanent hard fail."""
    monkeypatch.setitem(
        config.data,
        "image_pipeline",
        {**config.get_image_pipeline_settings(), "enabled": True, "global_queue_max": 2},
    )
    scheduler, pools = new_scheduler_with_pools()

    for index in range(4):
        with pytest.raises(ValueError):
            scheduler.begin_run(task_key=f"leak-5-{index}", mode="generate", payload={"n": "four"})

    assert pools._in_flight == 0
    # Still admits real work: without the rollback this raised "global queue is full".
    run = scheduler.begin_run(task_key="leak-5-ok", mode="generate", payload={"prompt": "a cat"})
    assert pools._in_flight == 1
    run.finish()
    assert pools._in_flight == 0


def test_finish_twice_does_not_double_decrement(pipeline_on):
    """A second finish() must not steal another live run's permit."""
    scheduler, pools = new_scheduler_with_pools()
    first = scheduler.begin_run(task_key="idem-1", mode="generate", payload={"prompt": "a cat"})
    assert pools._in_flight == 1
    first.finish()
    assert pools._in_flight == 0

    second = scheduler.begin_run(task_key="idem-2", mode="generate", payload={"prompt": "a cat"})
    assert pools._in_flight == 1

    first.finish()  # stale duplicate

    assert pools._in_flight == 1, "the duplicate finish() stole the live run's admission permit"
    second.finish()
    assert pools._in_flight == 0


def test_finish_twice_returns_timings_both_times(pipeline_on):
    scheduler, _pools = new_scheduler_with_pools()
    run = scheduler.begin_run(task_key="idem-3", mode="generate", payload={"prompt": "a cat"})
    first = run.finish()
    second = run.finish()
    assert second is first


# ------------------------------------------------------------------ A3-6 (§B10)


def test_submit_worker_survives_non_runtime_error_from_run_task(temp_dir, pipeline_off):
    """A ValueError out of _run_task must terminalise the task, not kill the worker.

    Pre-fix the thread died with the task still RUNNING, and nothing reclaimed it:
    startup recovery is latched and _cleanup_locked only evicts terminal rows.
    """
    service = make_service(temp_dir)
    try:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("normalize_multi_image_mode got a list")

        service._run_task = boom  # type: ignore[method-assign]
        key = seed_task(service, "crash-value-error")
        service.start_background()
        worker = service._submit_threads[0]

        task = wait_for_status(service, key, TASK_STATUS_ERROR)

        assert task["status"] in TERMINAL_STATUSES
        assert "ValueError" in str(task.get("error"))
        assert worker.is_alive(), "the submit worker thread died instead of failing the task"
    finally:
        service.stop()


def test_submit_worker_survives_non_queue_full_runtime_error(temp_dir, pipeline_on):
    """The explicit re-raise sits *before* the try/finally, so nothing covered it."""
    from services.image_task_service import image_pipeline_scheduler

    service = make_service(temp_dir)
    try:
        with mock.patch.object(
            image_pipeline_scheduler,
            "begin_run",
            side_effect=RuntimeError("image pipeline pools are wedged"),
        ):
            key = seed_task(service, "crash-runtime-error")
            service.start_background()
            worker = service._submit_threads[0]

            task = wait_for_status(service, key, TASK_STATUS_ERROR)

            assert "RuntimeError" in str(task.get("error"))
            assert worker.is_alive(), "the submit worker thread died instead of failing the task"
    finally:
        service.stop()


def test_submit_worker_survives_non_runtime_error_from_begin_run(temp_dir, pipeline_on):
    """begin_run's except clause only catches ImagePoolStarvedError / RuntimeError."""
    from services.image_task_service import image_pipeline_scheduler

    service = make_service(temp_dir)
    try:
        with mock.patch.object(
            image_pipeline_scheduler,
            "begin_run",
            side_effect=ValueError("invalid literal for int() with base 10: 'four'"),
        ):
            key = seed_task(service, "crash-begin-run-value")
            service.start_background()
            worker = service._submit_threads[0]

            wait_for_status(service, key, TASK_STATUS_ERROR)
            assert worker.is_alive()
    finally:
        service.stop()


def test_submit_worker_still_dispatches_after_a_crash(temp_dir, pipeline_off):
    """A surviving worker must keep draining the queue, not just stay technically alive."""
    service = make_service(temp_dir)
    try:
        calls: list[str] = []
        original = service._run_task

        def flaky(key: str, *args: Any, **kwargs: Any) -> None:
            calls.append(key)
            if key.endswith("crash-first"):
                raise ValueError("boom")
            return original(key, *args, **kwargs)

        service._run_task = flaky  # type: ignore[method-assign]
        first = seed_task(service, "crash-first")
        service.start_background()
        wait_for_status(service, first, TASK_STATUS_ERROR)

        second = seed_task(service, "crash-second")
        with service._condition:
            service._condition.notify_all()
        wait_for_status(service, second, "success")
        assert calls.count(second) == 1
    finally:
        service.stop()


def test_poll_worker_survives_exception_from_resume_poll(temp_dir, pipeline_off):
    """_next_poll_task_locked also flips the task to RUNNING before handing it over."""
    service = make_service(temp_dir)
    try:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("resume poll blew up")

        service._run_resume_poll_with_hard_timeout = boom  # type: ignore[method-assign]
        key = seed_task(
            service,
            "crash-poll",
            status=TASK_STATUS_TIMEOUT_PENDING,
            progress="timeout_pending",
            conversation_id="conv-abc123",
            resume_timeout_secs=30.0,
        )
        service.start_background()
        worker = service._poll_threads[0]

        task = wait_for_status(service, key, TASK_STATUS_ERROR)

        assert "ValueError" in str(task.get("error"))
        assert worker.is_alive(), "the poll worker thread died instead of failing the task"
    finally:
        service.stop()


def test_worker_crash_handler_keeps_memory_terminal_when_db_write_fails(temp_dir, pipeline_off):
    """Locked DB / full disk is exactly the case that stranded RUNNING rows."""
    import sqlite3

    service = make_service(temp_dir)
    try:
        key = seed_task(service, "crash-db", status=TASK_STATUS_RUNNING, progress="submitting")
        with mock.patch.object(
            type(service),
            "_save_task_locked",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            service._handle_worker_loop_crash(key, ValueError("boom"), worker="submit")

        with service._lock:
            task = dict(service._tasks[key])
        assert task["status"] == TASK_STATUS_ERROR
        assert "ValueError" in str(task.get("error"))
    finally:
        service.stop()


# ------------------------------------------------------- A3-6 reaper (§B10 half 2)


def test_stuck_running_bound_is_derived_from_hard_timeout(temp_dir, pipeline_off):
    """The bound is `_task_hard_timeout_secs` + margin, not an independent number."""
    service = make_service(temp_dir)
    try:
        generation = {"payload": {"prompt": "a cat", "poll_timeout_secs": 120.0}}
        multi_ref = {"payload": {"prompt": "a cat", "poll_timeout_secs": 360.0}}
        gen_hard = service._task_hard_timeout_secs(generation["payload"])
        multi_hard = service._task_hard_timeout_secs(multi_ref["payload"])

        gen_bound = service._stuck_running_bound_secs(generation)
        multi_bound = service._stuck_running_bound_secs(multi_ref)

        assert gen_bound > gen_hard
        assert multi_bound > multi_hard
        # A multi-reference edit gets a strictly wider bound than a plain generation.
        assert multi_bound > gen_bound
        # The margin never collapses to nothing, and never runs away either.
        assert 60.0 <= (gen_bound - gen_hard) <= 180.0
        assert 60.0 <= (multi_bound - multi_hard) <= 180.0
    finally:
        service.stop()


def test_stuck_running_task_is_reaped_and_releases_resources(temp_dir, pipeline_on):
    """Reaping must release the account slot and the pipeline permit, not just status."""
    service = make_service(temp_dir)
    try:
        scheduler, pools = new_scheduler_with_pools()
        key = seed_task(
            service,
            "stuck-1",
            status=TASK_STATUS_RUNNING,
            progress="submitting",
            age_secs=10_000.0,
            worker_started_ts=time.time() - 10_000.0,
            started_ts=time.time() - 10_000.0,
            resume_access_token="tok-stuck-1",
        )
        run = scheduler.begin_run(task_key=key, mode="generate", payload={"prompt": "a cat"})
        run.bind_account_token("tok-bound-1")
        with service._lock:
            service._active_pipeline_runs[key] = run
        cancel_event = threading.Event()
        with service._lock:
            service._cancel_events[key] = cancel_event
        assert pools._in_flight == 1

        with mock.patch("services.image_task_service.account_service") as account_service:
            reaped = service.reap_stuck_running_tasks()

        assert reaped == [key]
        with service._lock:
            task = dict(service._tasks[key])
        assert task["status"] == TASK_STATUS_ERROR
        assert task["status"] in TERMINAL_STATUSES
        assert "stuck" in str(task.get("error"))
        # pipeline accounting
        assert pools._in_flight == 0, "the reaper must hand the admission permit back"
        with service._lock:
            assert key not in service._active_pipeline_runs
        # account slot
        released = {call.args[0] for call in account_service.release_image_slot.call_args_list}
        assert "tok-stuck-1" in released
        assert "tok-bound-1" in released
        # an orphaned runner thread must be told to stop
        assert cancel_event.is_set()
    finally:
        service.stop()


def test_running_task_inside_its_bound_is_not_reaped(temp_dir, pipeline_off):
    service = make_service(temp_dir)
    try:
        key = seed_task(service, "live-1", status=TASK_STATUS_RUNNING, progress="submitting")
        with service._lock:
            bound = service._stuck_running_bound_secs(service._tasks[key])
        # Just inside the bound: still legitimately running.
        with service._lock:
            service._tasks[key]["updated_ts"] = time.time() - (bound - 5.0)

        with mock.patch("services.image_task_service.account_service") as account_service:
            reaped = service.reap_stuck_running_tasks()

        assert reaped == []
        with service._lock:
            assert service._tasks[key]["status"] == TASK_STATUS_RUNNING
        account_service.release_image_slot.assert_not_called()
    finally:
        service.stop()


def test_reaper_ignores_queued_and_terminal_tasks(temp_dir, pipeline_off):
    service = make_service(temp_dir)
    try:
        queued = seed_task(service, "old-queued", age_secs=10_000.0)
        done = seed_task(service, "old-error", status=TASK_STATUS_ERROR, age_secs=10_000.0)
        pending = seed_task(
            service,
            "old-pending",
            status=TASK_STATUS_TIMEOUT_PENDING,
            age_secs=10_000.0,
            conversation_id="conv-old",
        )

        assert service.reap_stuck_running_tasks() == []
        with service._lock:
            assert service._tasks[queued]["status"] == TASK_STATUS_QUEUED
            assert service._tasks[done]["status"] == TASK_STATUS_ERROR
            assert service._tasks[pending]["status"] == TASK_STATUS_TIMEOUT_PENDING
    finally:
        service.stop()


def test_stuck_resume_polling_task_gets_the_wider_bound(temp_dir, pipeline_off):
    """resume_polling has its own ladder; its bound must cover that, then reap."""
    service = make_service(temp_dir)
    try:
        key = seed_task(
            service,
            "stuck-resume",
            status=TASK_STATUS_RUNNING,
            progress="resume_polling",
            conversation_id="conv-resume",
            resume_timeout_secs=300.0,
        )
        with service._lock:
            task = service._tasks[key]
            resume_bound = service._stuck_running_bound_secs(task)
            plain_bound = service._stuck_running_bound_secs(
                {"payload": task["payload"], "status": TASK_STATUS_RUNNING}
            )
        assert resume_bound > plain_bound

        with service._lock:
            service._tasks[key]["updated_ts"] = time.time() - (resume_bound - 5.0)
        assert service.reap_stuck_running_tasks() == []

        with service._lock:
            service._tasks[key]["updated_ts"] = time.time() - (resume_bound + 5.0)
        with mock.patch("services.image_task_service.account_service"):
            assert service.reap_stuck_running_tasks() == [key]
    finally:
        service.stop()


def test_reaper_is_throttled_between_worker_iterations(temp_dir, pipeline_off):
    """The loops call the reaper every iteration; it must not scan on every pass."""
    service = make_service(temp_dir)
    try:
        with mock.patch.object(type(service), "reap_stuck_running_tasks", return_value=[]) as reap:
            service._maybe_reap_stuck_running_tasks()
            service._maybe_reap_stuck_running_tasks()
            service._maybe_reap_stuck_running_tasks()
        assert reap.call_count == 1
    finally:
        service.stop()


def test_reaper_runs_from_the_worker_loops(temp_dir, pipeline_off, monkeypatch):
    """End to end: a row stranded *after* startup recovery is still reclaimed.

    ``_recover_unfinished_locked`` only runs once, behind the ``_runtime_recovered``
    latch, so the task is seeded after ``start_background()`` — the exact window the
    audit describes. The throttle is disabled so the next loop pass acts instead of the
    one 15s later.
    """
    import services.image_task_service as its

    service = make_service(temp_dir)
    try:
        service.start_background()
        monkeypatch.setattr(its, "_REAP_INTERVAL_SECS", 0.0)
        key = seed_task(
            service,
            "stuck-loop",
            status=TASK_STATUS_RUNNING,
            progress="submitting",
            age_secs=10_000.0,
        )
        with mock.patch("services.image_task_service.account_service"):
            task = wait_for_status(service, key, TASK_STATUS_ERROR)
        assert "stuck" in str(task.get("error"))
    finally:
        service.stop()


def test_reaped_run_finish_and_worker_finalize_decrement_once(temp_dir, pipeline_on):
    """Reaper + the worker's own finally must not double-decrement the same run."""
    service = make_service(temp_dir)
    try:
        scheduler, pools = new_scheduler_with_pools()
        key = seed_task(service, "stuck-double", status=TASK_STATUS_RUNNING, age_secs=10_000.0)
        run = scheduler.begin_run(task_key=key, mode="generate", payload={"prompt": "a cat"})
        with service._lock:
            service._active_pipeline_runs[key] = run
        other = scheduler.begin_run(task_key="other", mode="generate", payload={"prompt": "a cat"})
        assert pools._in_flight == 2

        with mock.patch("services.image_task_service.account_service"):
            service.reap_stuck_running_tasks()
        assert pools._in_flight == 1

        # The late-returning worker still runs its finally-block.
        service._finalize_pipeline_run(key, run)

        assert pools._in_flight == 1, "the worker's finalize double-decremented a reaped run"
        other.finish()
        assert pools._in_flight == 0
    finally:
        service.stop()


# ------------------------------------------------- §8 note: bare _tasks iteration


def test_warm_account_lease_pool_survives_concurrent_task_mutation(temp_dir, pipeline_on):
    """It is called without the lock and used to hand the live dict to the callee."""
    service = make_service(temp_dir)
    try:
        for index in range(200):
            seed_task(service, f"warm-{index}")

        seen: list[int] = []
        stop = threading.Event()

        def churn() -> None:
            index = 1000
            while not stop.is_set():
                key = seed_task(service, f"churn-{index}")
                with service._lock:
                    service._tasks.pop(key, None)
                index += 1

        def snapshot_taker(tasks: dict[str, object]) -> int:
            # Mimic the callee: iterate whatever we were handed.
            seen.append(sum(1 for _ in tasks.values()))
            return 0

        churner = threading.Thread(target=churn, daemon=True)
        churner.start()
        try:
            with mock.patch(
                "services.image_pipeline.account_lease_pool.account_lease_pool.seed_queued_preferences",
                side_effect=snapshot_taker,
            ), mock.patch(
                "services.image_pipeline.account_lease_pool.account_lease_pool.maintain",
                return_value=0,
            ):
                for _ in range(50):
                    service._warm_account_lease_pool_locked()
        finally:
            stop.set()
            churner.join(timeout=5.0)
            assert not churner.is_alive()

        # Pre-fix the RuntimeError was swallowed by the bare except, so the callee simply
        # never ran to completion; post-fix every pass hands over a stable snapshot.
        assert len(seen) == 50
    finally:
        service.stop()
