"""sS slot lifecycle regressions from audit 28 §B1 / §B2 (backlog A1-1..A1-4).

Covered:
  A1-1  the 75s sS wall must not cover the post-SSE poll/download phase
  A1-2  timeout -> ImageGenerationError must carry the known conversation_id
  A1-3  a re-acquired sS slot must be releasable again (no permanent leak)
  A1-4  pool acquire must be bounded, and slots released on the exception path
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from services.config import config
from services.image_pipeline.orchestrator import ImagePipelineScheduler, PipelineRun
from services.image_pipeline.pools import PipelinePools
from services.image_pipeline.types import MultiImageMode, PipelineRunState


def _make_run(task_key: str, *, sse_slots: int = 2) -> PipelineRun:
    pools = PipelinePools(
        prompt_slots=2,
        sse_slots=sse_slots,
        download_concurrency=1,
        upload_concurrency=1,
    )
    return PipelineRun(
        scheduler=ImagePipelineScheduler(),
        state=PipelineRunState(task_key=task_key, mode="generate"),
        pools=pools,
        settings={},
        payload={},
        needs_ps=False,
        multi_image_mode=MultiImageMode.FAST,
        n=1,
        account_provider=MagicMock(),
        started_at=time.monotonic(),
    )


def _ss_active(run: PipelineRun) -> int:
    return run._pools.ss.snapshot().active


# --------------------------------------------------------------------------- A1-3


def test_reacquired_ss_slot_can_be_released_again():
    """acquire -> release -> acquire -> release must return every slot to the pool.

    `_generate_single_image` is a `while True:` retry loop and `continue` runs the
    `finally` that calls release_ss, so the same image_index legitimately re-acquires.
    Fails against the pre-fix code, which gated release on a monotonic index set.

    The pool is sized so the second acquire can never block: a regression must show up
    as a leaked slot, not as a hung test.
    """
    run = _make_run("t-a13", sse_slots=4)
    free_before = run._pools.ss.slots - _ss_active(run)

    first_slot, _ = run.acquire_ss(image_index=1)
    run.release_ss(image_index=1, slot=first_slot)

    second_slot, _ = run.acquire_ss(image_index=1)
    run.release_ss(image_index=1, slot=second_slot)

    assert run._pools.ss.slots - _ss_active(run) == free_before
    assert _ss_active(run) == 0


def test_retry_loop_does_not_exhaust_ss_pool():
    """Repeated acquire/release cycles on one index must not drain the pool.

    Pool is deliberately larger than the cycle count so a leak surfaces as
    `active > 0` rather than as a blocked acquire.
    """
    run = _make_run("t-a13-loop", sse_slots=8)
    for _ in range(6):
        slot, _ = run.acquire_ss(image_index=1)
        run.release_ss(image_index=1, slot=slot)
    assert _ss_active(run) == 0


def test_release_ss_is_still_idempotent_for_one_acquisition():
    """The original intent survives: one logical acquisition, one effective release."""
    run = _make_run("t-a13-idem", sse_slots=4)
    slot, _ = run.acquire_ss(image_index=1)
    run.release_ss(image_index=1, slot=slot)
    assert _ss_active(run) == 0

    other_slot, _ = run.acquire_ss(image_index=2)
    # A stale second release for index 1 must not steal index 2's slot.
    run.release_ss(image_index=1, slot=slot)
    assert _ss_active(run) == 1
    assert run._ss_holders.get(run._holder("ss-2")) == other_slot


def test_sediment_early_release_then_finally_release_is_single_release():
    """on_sediment_captured releases early; the finally-block release is a no-op."""
    run = _make_run("t-a13-sed")
    slot, _ = run.acquire_ss(image_index=1)
    assert _ss_active(run) == 1

    run.on_sediment_captured(image_index=1, sediment_ids=["sed-1"])
    assert _ss_active(run) == 0

    run.release_ss(image_index=1, slot=slot)
    assert _ss_active(run) == 0


# --------------------------------------------------------------------------- A1-1


class _FakeClock:
    """Injectable time.monotonic replacement — advance without sleeping."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def test_ss_wall_does_not_fire_after_sse_phase_end(monkeypatch):
    """A 200s poll (inside the 300/360s budget) must not trip the 75s wall."""
    from services.image_pipeline import orchestrator as orch

    clock = _FakeClock()
    monkeypatch.setattr(orch.time, "monotonic", clock)

    run = _make_run("t-a11", sse_slots=4)
    slot, _ = run.acquire_ss(image_index=1)
    try:
        # SSE stream took 20s, then stream_image_outputs signals the phase boundary.
        clock.advance(20.0)
        run.note_ss_stream_phase_end(image_index=1)
        # 200s of legitimate conversation polling + resolve + download follows.
        clock.advance(200.0)
        run.assert_ss_wall_ok(image_index=1)  # must not raise
    finally:
        run.release_ss(image_index=1, slot=slot)


def test_ss_wall_still_fires_during_sse_phase(monkeypatch):
    """Positive control: the fast-fail intent is preserved before the phase ends."""
    from services.image_pipeline import orchestrator as orch

    clock = _FakeClock()
    monkeypatch.setattr(orch.time, "monotonic", clock)

    run = _make_run("t-a11-pos", sse_slots=4)
    slot, _ = run.acquire_ss(image_index=1)
    try:
        clock.advance(config.image_ss_stage_wall_timeout_secs + 5.0)
        with pytest.raises(TimeoutError, match="sS stage wall timeout"):
            run.assert_ss_wall_ok(image_index=1)
    finally:
        run.release_ss(image_index=1, slot=slot)


def test_note_ss_stream_phase_end_preserves_full_stage_timing(monkeypatch):
    """Disarming the wall must not shorten timings.ss_ms (the gantt depends on it)."""
    from services.image_pipeline import orchestrator as orch

    clock = _FakeClock()
    monkeypatch.setattr(orch.time, "monotonic", clock)

    run = _make_run("t-a11-timing", sse_slots=4)
    slot, _ = run.acquire_ss(image_index=1)
    clock.advance(20.0)
    run.note_ss_stream_phase_end(image_index=1)
    clock.advance(180.0)
    run.release_ss(image_index=1, slot=slot)
    # ss_ms must still span the whole stage (20s SSE + 180s poll), not just the SSE part.
    assert run.state.timings.ss_ms == 200_000


def test_note_ss_stream_phase_end_is_idempotent_and_index_safe():
    run = _make_run("t-a11-idem", sse_slots=4)
    run.note_ss_stream_phase_end(image_index=7)  # never armed
    slot, _ = run.acquire_ss(image_index=1)
    run.note_ss_stream_phase_end(image_index=1)
    run.note_ss_stream_phase_end(image_index=1)
    run.assert_ss_wall_ok(image_index=1)
    run.release_ss(image_index=1, slot=slot)


def test_ss_wall_rearms_on_retry(monkeypatch):
    """A retry re-arms a fresh wall rather than inheriting the previous attempt's."""
    from services.image_pipeline import orchestrator as orch

    clock = _FakeClock()
    monkeypatch.setattr(orch.time, "monotonic", clock)

    run = _make_run("t-a11-rearm", sse_slots=4)
    slot, _ = run.acquire_ss(image_index=1)
    clock.advance(200.0)
    run.note_ss_stream_phase_end(image_index=1)
    run.release_ss(image_index=1, slot=slot)

    slot2, _ = run.acquire_ss(image_index=1)
    try:
        # Fresh attempt: the wall restarts from zero, and is armed again.
        run.assert_ss_wall_ok(image_index=1)
        clock.advance(config.image_ss_stage_wall_timeout_secs + 5.0)
        with pytest.raises(TimeoutError, match="sS stage wall timeout"):
            run.assert_ss_wall_ok(image_index=1)
    finally:
        run.release_ss(image_index=1, slot=slot2)


# --------------------------------------------------------------------------- A1-2


class _FakePipelineRun:
    """Minimal PipelineRun stand-in that trips the sS wall at the success yield."""

    needs_ps = False
    multi_image_mode = MultiImageMode.FAST

    def __init__(self) -> None:
        self.account_provider = MagicMock()
        self.account_provider.acquire_for_ss.return_value = MagicMock(access_token="tok-1")
        self.released: list[int] = []

    def mark_account_wait_start(self) -> None:
        pass

    def mark_account_acquired(self) -> None:
        pass

    def bind_account_token(self, token: str) -> None:
        pass

    def acquire_ss(self, *, image_index: int) -> tuple[int, int]:
        return 0, 0

    def assert_ss_wall_ok(self, *, image_index: int) -> None:
        raise TimeoutError("sS stage wall timeout (75s)")

    def release_ss(self, *, image_index: int, slot: int | None = None) -> None:
        self.released.append(image_index)


def _run_single_image_with_wall_timeout():
    from services.protocol import conversation as conv

    request = conv.ConversationRequest(model="gpt-4o-image", prompt="a cat", n=1)
    pipeline_run = _FakePipelineRun()
    request = conv.replace(request, pipeline_run=pipeline_run)

    def fake_stream(backend, req, index, total):
        yield conv.ImageOutput(
            kind="result",
            model=req.model,
            index=index,
            total=total,
            data=[{"b64_json": "AAAA"}],
            conversation_id="conv-abc12345",
        )

    account_service = MagicMock()
    account_service.get_account.return_value = {"email": "who@example.com"}

    with patch.object(conv, "stream_image_outputs", fake_stream), \
            patch.object(conv, "OpenAIBackendAPI", MagicMock()), \
            patch.object(conv, "account_service", account_service), \
            patch.object(conv, "is_codex_image_model", return_value=False):
        with pytest.raises(conv.ImageGenerationError) as excinfo:
            conv._generate_single_image(request, 1, 1)
    return excinfo.value, pipeline_run


def test_wall_timeout_error_keeps_conversation_id():
    """The blanked conversation_id was what short-circuited the resume ladder."""
    exc, _ = _run_single_image_with_wall_timeout()
    assert exc.conversation_id == "conv-abc12345"


def test_wall_timeout_error_routes_to_timeout_pending():
    """image_task_service:1668 needs conversation_id AND a timeout-looking message."""
    from services.image_task_service import _looks_like_timeout

    exc, _ = _run_single_image_with_wall_timeout()
    assert bool(exc.conversation_id) and _looks_like_timeout(str(exc))


def test_wall_timeout_error_keeps_access_token_for_resume():
    exc, _ = _run_single_image_with_wall_timeout()
    assert exc.access_token == "tok-1"


# --------------------------------------------------------------------------- A1-4


def test_ss_acquire_on_exhausted_pool_times_out_instead_of_blocking():
    """A wedged pool must raise inside the configured timeout, not wait forever.

    Wrapped in a thread so a regression fails the assertion instead of hanging
    the whole suite.
    """
    run = _make_run("t-a14", sse_slots=1)
    held_slot, _ = run.acquire_ss(image_index=1)
    outcome: dict[str, object] = {}

    def waiter() -> None:
        try:
            run.acquire_ss(image_index=2)
        except BaseException as exc:  # noqa: BLE001 - record whatever comes back
            outcome["error"] = exc
        else:
            outcome["error"] = None

    with patch.object(
        type(config), "image_pool_acquire_timeout_secs", property(lambda self: 0.3)
    ):
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "acquire_ss blocked past the configured timeout"
    assert isinstance(outcome.get("error"), TimeoutError)
    run.release_ss(image_index=1, slot=held_slot)


def test_download_acquire_timeout_clears_holder():
    """A timed-out acquire must not leave a holder that later decrements the pool."""
    run = _make_run("t-a14-dl")
    run._pools.download.acquire("outsider")
    outcome: dict[str, object] = {}

    def waiter() -> None:
        try:
            run.acquire_download()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    with patch.object(
        type(config), "image_pool_acquire_timeout_secs", property(lambda self: 0.3)
    ):
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "acquire_download blocked past the configured timeout"
    assert isinstance(outcome.get("error"), TimeoutError)
    assert run._download_holder == ""

    active_before = run._pools.download.snapshot().active
    run.release_download()  # must be a no-op, not an under-count
    assert run._pools.download.snapshot().active == active_before


def test_upload_acquire_timeout_clears_holder():
    run = _make_run("t-a14-up")
    run._pools.upload.acquire("outsider")
    outcome: dict[str, object] = {}

    def waiter() -> None:
        try:
            run.acquire_upload()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    with patch.object(
        type(config), "image_pool_acquire_timeout_secs", property(lambda self: 0.3)
    ):
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "acquire_upload blocked past the configured timeout"
    assert isinstance(outcome.get("error"), TimeoutError)
    assert run._upload_holder == ""

    active_before = run._pools.upload.snapshot().active
    run.release_upload()
    assert run._pools.upload.snapshot().active == active_before


def test_ps_acquire_on_exhausted_pool_times_out_instead_of_blocking():
    run = _make_run("t-a14-ps")
    run.needs_ps = True
    run._ps_rounds_remaining = 4
    run._pools.ps.acquire("outsider-a")
    run._pools.ps.acquire("outsider-b")
    outcome: dict[str, object] = {}

    def waiter() -> None:
        try:
            run.acquire_ps()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    with patch.object(
        type(config), "image_pool_acquire_timeout_secs", property(lambda self: 0.3)
    ):
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "acquire_ps blocked past the configured timeout"
    assert isinstance(outcome.get("error"), TimeoutError)


def test_ss_acquire_timeout_rolls_back_ledger_lease():
    """No ledger entry may survive an acquire we never completed."""
    run = _make_run("t-a14-ledger", sse_slots=1)
    held_slot, _ = run.acquire_ss(image_index=1)
    holder = run._holder("ss-2")
    outcome: dict[str, object] = {}

    with patch("services.image_pipeline.orchestrator.slot_ledger") as ledger, \
            patch.object(
                type(config), "image_pool_acquire_timeout_secs", property(lambda self: 0.3)
            ):
        def waiter() -> None:
            try:
                run.acquire_ss(image_index=2)
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "acquire_ss blocked past the configured timeout"
        assert isinstance(outcome.get("error"), TimeoutError)
        ledger.try_acquire_ss.assert_called_once()
        ledger.release_ss.assert_called_once_with(holder)

    assert holder not in run._ss_holders
    run.release_ss(image_index=1, slot=held_slot)


def test_slots_released_on_exception_path():
    """An exception between acquire and release must still return the slot."""
    run = _make_run("t-a14-exc")

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with run.hold_ss_slot(1):
            raise _Boom("upstream blew up mid-stage")

    assert _ss_active(run) == 0
    assert run._ss_holders == {}


def test_finish_sweeps_leaked_ss_slot_and_account_ledger():
    """finish() is the last-resort sweep for anything an exception path skipped."""
    run = _make_run("t-a14-sweep")
    with patch("services.image_pipeline.orchestrator.slot_ledger") as ledger:
        run.bind_account_token("access-token-xyz")
        run.acquire_ss(image_index=1)
        assert _ss_active(run) == 1

        run.finish()

        assert _ss_active(run) == 0
        ledger.release_account.assert_called_once_with(run._account_ledger_holder())


def test_pool_acquire_timeout_config_defaults_and_override():
    assert config.image_pool_acquire_timeout_secs >= 5.0
    assert config.image_ss_slot_deadline_secs >= 30.0
    # The slot-hold deadline must not be shorter than the widest poll budget it wraps.
    assert config.image_ss_slot_deadline_secs > config.image_multi_reference_poll_timeout_secs

    settings = config.get_image_pipeline_settings()
    assert settings["pool_acquire_timeout_secs"] >= 5
    assert settings["ss_slot_deadline_secs"] >= 30

    original = config.data.get("image_pipeline")
    try:
        config.data["image_pipeline"] = {
            "ss_stage_wall_timeout_secs": 90,
            "ss_slot_deadline_secs": 1200,
            "pool_acquire_timeout_secs": 45,
        }
        assert config.image_ss_stage_wall_timeout_secs == 90.0
        assert config.image_ss_slot_deadline_secs == 1200.0
        assert config.image_pool_acquire_timeout_secs == 45.0
    finally:
        if original is None:
            config.data.pop("image_pipeline", None)
        else:
            config.data["image_pipeline"] = original
