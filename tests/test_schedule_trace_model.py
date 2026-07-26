"""Tests for schedule trace model (Python fallback)."""

from services.image_pipeline.schedule_trace_model import build_model_from_events


def test_ss_queue_phase():
    events = [
        (8, 1_000_000_000, (10 << 16) | 3),
        (9, 1_025_000_000, 2),  # +25ms
    ]
    model = build_model_from_events(events)
    assert model["phases_ms"]["ss_queue_ms"] == 25


def test_full_happy_path():
    events = [
        (1, 0, 0),
        (2, 1_000_000, 0),
        (3, 2_000_000, 0),
        (4, 3_000_000, 0),
        (5, 20_000_000, 0),
        (8, 21_000_000, 0),
        (9, 22_000_000, 1),
        (11, 50_000_000, 0),
        (12, 56_000_000, 0),
        (13, 57_000_000, 0),
        (14, 58_000_000, 0),
        (15, 59_000_000, 0),
    ]
    model = build_model_from_events(events)
    assert model["phases_ms"]["task_queue_ms"] == 1
    assert model["phases_ms"]["account_queue_ms"] == 17
    assert model["phases_ms"]["sse_stream_ms"] == 30
    assert model["phases_ms"]["poll_resolve_ms"] == 6
