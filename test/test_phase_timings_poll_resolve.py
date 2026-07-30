from __future__ import annotations

from services.image_pipeline.types import PhaseTimingsMs


def test_phase_timings_exports_poll_resolve_ms() -> None:
  timings = PhaseTimingsMs(sse_stream_ms=12000, poll_resolve_ms=3400, wall_clock_ms=16000)
  payload = timings.to_dict()
  assert payload["poll_resolve_ms"] == 3400
  roundtrip = PhaseTimingsMs.from_dict(payload)
  assert roundtrip.poll_resolve_ms == 3400
