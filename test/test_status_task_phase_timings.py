from __future__ import annotations

from services.image_task_service import _status_task


def test_status_task_exposes_phase_timings_without_data_blob() -> None:
  task = {
    "id": "task-1",
    "status": "success",
    "mode": "generate",
    "model": "gpt-image-2",
    "created_at": "2026-07-30 00:00:00",
    "updated_at": "2026-07-30 00:01:00",
    "data": [{"url": "http://example.test/image.png"}],
    "phase_timings_ms": {
      "sse_stream_ms": 24000,
      "poll_resolve_ms": 3500,
      "wall_clock_ms": 28000,
      "task_queue_ms": 0,
    },
  }
  item = _status_task(task)
  assert "data" not in item
  assert item["phase_timings_ms"]["sse_stream_ms"] == 24000
  assert item["phase_timings_ms"]["poll_resolve_ms"] == 3500
  assert item["wall_clock_ms"] == 28000
  assert "task_queue_ms" not in item["phase_timings_ms"]
