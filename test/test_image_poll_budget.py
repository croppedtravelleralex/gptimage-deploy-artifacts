from __future__ import annotations

import time
import unittest
from unittest import mock

from services.image_poll_budget import ImagePollBudget
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from services.config import config


class ImagePollBudgetTests(unittest.TestCase):
    def test_tasks_only_on_first_and_every_n(self) -> None:
        budget = ImagePollBudget.create(
            timeout_secs=60,
            max_conversation_gets=10,
            max_tasks_gets=3,
            tasks_every_n_attempts=4,
        )
        self.assertTrue(budget.begin_attempt())
        self.assertTrue(budget.should_query_tasks())
        budget.record_tasks_get()
        self.assertTrue(budget.begin_attempt())
        self.assertFalse(budget.should_query_tasks())
        self.assertTrue(budget.begin_attempt())
        self.assertFalse(budget.should_query_tasks())
        self.assertTrue(budget.begin_attempt())
        self.assertTrue(budget.should_query_tasks())

    def test_conversation_get_hard_cap(self) -> None:
        budget = ImagePollBudget.create(
            timeout_secs=60,
            max_conversation_gets=2,
            max_tasks_gets=0,
            tasks_every_n_attempts=4,
        )
        self.assertTrue(budget.begin_attempt())
        budget.record_conversation_get()
        self.assertTrue(budget.begin_attempt())
        budget.record_conversation_get()
        self.assertFalse(budget.begin_attempt())
        self.assertEqual(budget.exhausted_reason, "conversation_get_budget")

    def test_poll_respects_max_upstream_gets(self) -> None:
        class FakeBackend(OpenAIBackendAPI):
            def __init__(self) -> None:
                self.calls = 0
                self.cancel_event = None

            def _get_conversation(self, conversation_id: str) -> dict:
                self.calls += 1
                return {"mapping": {}}

            def _query_backend_tasks(self, conversation_id: str = "", task_id: str = "", timeout_secs: float = 30.0):
                return []

            def _extract_image_tool_records(self, conversation):
                return []

            def _find_content_policy_error_in_conversation(self, conversation):
                return ""

        backend = FakeBackend()
        with (
            mock.patch.dict(
                config.data,
                {
                    "image_poll_initial_wait_secs": 0,
                    "image_poll_interval_secs": 0,
                    "image_poll_max_upstream_gets": 3,
                    "image_poll_max_tasks_gets": 1,
                    "image_poll_tasks_every_n_attempts": 4,
                    "image_settle_enabled": False,
                    "image_check_before_hit_enabled": False,
                },
            ),
            mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None),
            self.assertRaises(ImagePollTimeoutError) as ctx,
        ):
            backend._poll_image_results("conv-budget", timeout_secs=30)

        self.assertEqual(backend.calls, 3)
        self.assertEqual(getattr(ctx.exception, "poll_budget", {}).get("conversation_gets"), 3)


if __name__ == "__main__":
    unittest.main()
