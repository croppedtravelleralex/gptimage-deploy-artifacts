from __future__ import annotations

import time
import unittest

from services.request_phase import RequestPhaseTracker


class RequestPhaseTrackerTests(unittest.TestCase):
    def test_phase_marks_and_fail(self) -> None:
        tracker = RequestPhaseTracker(account_token="tok", node_proxy="http://u:p@h:1", purpose="text")
        first = tracker.mark("auth")
        self.assertEqual(first["phase"], "auth")
        self.assertEqual(first["event"], "request_phase")
        self.assertTrue(first["account_hash"])
        self.assertTrue(first["node_hash"])
        time.sleep(0.01)
        second = tracker.mark("upstream_submit")
        self.assertGreaterEqual(second["since_last_ms"], 0)
        failed = tracker.fail("upstream_submit", error_type="TimeoutError")
        self.assertEqual(failed["failed_phase"], "upstream_submit")
        self.assertEqual(failed["error_type"], "TimeoutError")
        blob = str(failed)
        self.assertNotIn("tok", blob)


if __name__ == "__main__":
    unittest.main()
