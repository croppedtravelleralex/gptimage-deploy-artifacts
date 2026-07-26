import unittest

from utils.image_gantt_segments import build_image_task_gantt_segments


class ImageGanttSegmentsTest(unittest.TestCase):
    def test_non_overlapping_segments_with_new_timings(self) -> None:
        segments = build_image_task_gantt_segments(
            {
                "admit_queue_ms": 100,
                "ss_queue_ms": 500,
                "account_queue_ms": 2000,
                "sse_stream_ms": 30000,
                "ss_ms": 35000,
                "download_ms": 800,
            }
        )
        keys = [item["key"] for item in segments]
        self.assertEqual(keys, ["queue_wait", "sse_active", "poll_resolve", "download_ms"])
        self.assertEqual(segments[0]["ms"], 2600)
        self.assertEqual(segments[1]["ms"], 30000)
        self.assertEqual(segments[2]["ms"], 3000)
        self.assertEqual(segments[0]["label"], "排队 Queue")
        self.assertEqual(segments[1]["label_en"], "SSE")

    def test_legacy_fallback_uses_started_ts_without_double_counting_ss_ms(self) -> None:
        segments = build_image_task_gantt_segments(
            {"ss_queue_ms": 0, "ss_ms": 38092, "download_ms": 1100},
            created_ts=1000.0,
            started_ts=1031.0,
        )
        keys = [item["key"] for item in segments]
        self.assertIn("sse_active", keys)
        self.assertNotIn("ss_ms", keys)
        self.assertNotIn("pre_ss_wait", keys)
        sse = next(item for item in segments if item["key"] == "sse_active")
        self.assertEqual(sse["ms"], 31000)
        total = sum(item["ms"] for item in segments)
        self.assertLessEqual(total, 38092 + 1100)


if __name__ == "__main__":
    unittest.main()
