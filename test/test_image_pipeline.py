from __future__ import annotations

import threading
import time

from services.image_pipeline.prompt import normalize_multi_image_mode, ps_rounds_for_request, should_need_ps
from services.image_pipeline.aci_ranker import aci_score
from services.image_pipeline.pools import PipelinePools, SlotPool
from services.image_pipeline.ready_buffer import ReadyBufferTracker
from services.image_pipeline.types import ImagePoolStarvedError, MultiImageMode


def test_should_need_ps_default_off():
    assert should_need_ps(prompt_enhance=False, prompt="cat") is False


def test_should_need_ps_short_when_enabled():
    assert should_need_ps(prompt_enhance=True, prompt="a cute cat") is True


def test_should_need_ps_long_skip():
    assert should_need_ps(prompt_enhance=True, prompt="x" * 300) is False


def test_ps_rounds_fast_vs_diverse():
    assert ps_rounds_for_request(n=3, multi_image_mode=MultiImageMode.FAST, needs_ps=True) == 1
    assert ps_rounds_for_request(n=3, multi_image_mode=MultiImageMode.DIVERSE, needs_ps=True) == 3
    assert ps_rounds_for_request(n=3, multi_image_mode=MultiImageMode.FAST, needs_ps=False) == 0


def test_normalize_multi_image_mode():
    assert normalize_multi_image_mode("diverse") == MultiImageMode.DIVERSE
    assert normalize_multi_image_mode(None) == MultiImageMode.FAST


def test_slot_pool_limits_concurrency():
    pool = SlotPool("test", 2)
    first_slot, _ = pool.acquire("a")
    second_slot, _ = pool.acquire("b")
    assert first_slot != second_slot

    started = threading.Event()
    holder: dict[str, int] = {}

    def waiter() -> None:
        slot, _ = pool.acquire("c")
        holder["slot"] = slot
        started.set()
        time.sleep(0.05)
        pool.release(slot, "c")

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.05)
    assert not started.is_set()

    pool.release(first_slot, "a")
    thread.join(timeout=1.0)
    assert started.is_set()
    pool.release(second_slot, "b")


def test_pipeline_pools_snapshot():
    pools = PipelinePools(prompt_slots=2, sse_slots=3, download_concurrency=4, upload_concurrency=2)
    pools.admit(10)
    snap = pools.snapshot()
    assert snap["in_flight"] == 1
    assert snap["ps"]["limit"] == 2
    assert snap["ss"]["limit"] == 3
    pools.finish()
    assert pools.snapshot()["in_flight"] == 0


def test_aci_score_bounds():
    score = aci_score({"quota": 10, "success": 5, "fail": 0})
    assert 0 <= score <= 100


def test_ready_buffer_pause_and_resume():
    tracker = ReadyBufferTracker()
    tracker.admit("a", bytes_estimate=tracker._max_bytes() + 1)
    assert tracker.should_pause_ss() is True
    tracker.release("a")
    tracker.wait_for_ss_slot(timeout=1.0)


def test_image_pool_starved():
    import pytest

    from services.image_pipeline.guards import ensure_dispatchable_pool

    with pytest.raises(ImagePoolStarvedError):
        ensure_dispatchable_pool(min_count=10_000)
