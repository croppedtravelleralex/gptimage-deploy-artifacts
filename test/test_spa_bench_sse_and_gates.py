from __future__ import annotations

import time
import unittest

from scripts.spa_bench_sse import (
    classify_image_sse_failure,
    consume_image_sse,
    redact_sse_timeline_event,
)
from scripts.spa_acceptance_gates import (
    concurrent4_allowed,
    serial5_passed,
    should_stop_serial5,
    summarize_failure_classes,
)


class _FakeClock:
    def __init__(self, stamps: list[float]) -> None:
        self._stamps = list(stamps)
        self._idx = 0

    def now(self) -> float:
        if self._idx < len(self._stamps):
            value = self._stamps[self._idx]
            self._idx += 1
            return value
        return self._stamps[-1]


class SpaBenchSseTest(unittest.TestCase):
    def test_parse_line_before_deadline_boundary(self) -> None:
        lines = [
            b'data: {"conversation_id":"cid-1","message":{"recipient":"other"}}',
            b'data: {"message":{"author":{"name":"image_gen"},"recipient":"image_gen"}}',
        ]
        stamps = [0.0, 44.5, 44.95]
        clock = _FakeClock(stamps)

        def fake_time() -> float:
            return clock.now()

        original = time.time
        time.time = fake_time  # type: ignore[method-assign]
        try:
            result = consume_image_sse(iter(lines), t0=0.0, gate_secs=45.0, total_read_secs=90.0)
        finally:
            time.time = original  # type: ignore[method-assign]

        self.assertTrue(result.has_image_gen_within_gate)
        self.assertFalse(result.gate_failed)

    def test_late_image_gen_after_gate(self) -> None:
        payload = '{"message":{"author":{"name":"image_gen"},"recipient":"image_gen"}}'
        lines = [f"data: {payload}".encode("utf-8")]
        original = time.time
        time.time = lambda: 0.0  # type: ignore[method-assign]
        try:
            # t0=-46 makes elapsed=46 at first parsed line without fragile multi-call clocks.
            result = consume_image_sse(iter(lines), t0=-46.0, gate_secs=45.0, total_read_secs=90.0)
        finally:
            time.time = original  # type: ignore[method-assign]

        self.assertTrue(result.late_image_gen_seen)
        self.assertTrue(result.gate_failed)
        failure = classify_image_sse_failure(
            has_image_gen_within_gate=False,
            gate_failed=result.gate_failed,
            late_image_gen_seen=result.late_image_gen_seen,
            tool_args_like_seen=False,
            quiet_stream=False,
            chunks=result.chunks,
        )
        self.assertEqual(failure, "late_image_gen_after_gate")

    def test_tool_args_as_text_classification(self) -> None:
        failure = classify_image_sse_failure(
            has_image_gen_within_gate=False,
            gate_failed=True,
            late_image_gen_seen=False,
            tool_args_like_seen=True,
            quiet_stream=False,
            chunks=3,
        )
        self.assertEqual(failure, "tool_args_as_text")

    def test_timeline_redacts_sensitive_content(self) -> None:
        event = redact_sse_timeline_event(
            '{"prompt":"secret prompt","access_token":"abc","message":{"author":{"role":"assistant"}}}',
            1200,
        )
        self.assertEqual(event["arrival_ms"], 1200)
        self.assertIn(event["payload_hint"], ("tool_args", "other"))


class SpaAcceptanceGatesTest(unittest.TestCase):
    def test_serial5_passed_requires_five_ok(self) -> None:
        self.assertFalse(
            serial5_passed(
                {
                    "summary": {
                        "planned": 5,
                        "attempted": 4,
                        "ok": 3,
                        "no_image_gen": 1,
                        "stopped_early": True,
                    }
                }
            )
        )
        self.assertTrue(
            serial5_passed(
                {
                    "summary": {
                        "planned": 5,
                        "attempted": 5,
                        "ok": 5,
                        "no_image_gen": 0,
                        "cf403_propagated": 0,
                        "stopped_early": False,
                        "serial5_passed": True,
                    }
                }
            )
        )

    def test_should_stop_on_no_image_gen(self) -> None:
        stop, reason = should_stop_serial5(
            [{"ok": False, "error": "no_image_gen_within_45s", "failure_class": "tool_args_as_text"}]
        )
        self.assertTrue(stop)
        self.assertIn("no_image_gen", reason)

    def test_failure_class_summary(self) -> None:
        counts = summarize_failure_classes(
            [{"ok": False, "failure_class": "tool_args_as_text"}, {"ok": False, "failure_class": "late_image_gen_after_gate"}]
        )
        self.assertEqual(counts["tool_args_as_text"], 1)
        self.assertEqual(counts["late_image_gen_after_gate"], 1)

    def test_concurrent4_blocked_without_serial5(self) -> None:
        allowed, reason = concurrent4_allowed("/nonexistent/serial5.json")
        self.assertFalse(allowed)
        self.assertIn("missing", reason)


if __name__ == "__main__":
    unittest.main()
