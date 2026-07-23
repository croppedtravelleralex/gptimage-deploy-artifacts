from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.llm_ops_agent import list_tools, run_agent
from services.text_nurture_service import TextNurtureService, text_task_queue


class TextNurtureTests(unittest.TestCase):
    def test_enqueue_rejects_image_prompts(self) -> None:
        svc = TextNurtureService()
        with self.assertRaises(ValueError):
            svc.enqueue(prompt="please generate an image of a cat")

    def test_enqueue_ok(self) -> None:
        before = text_task_queue.depth()
        svc = TextNurtureService()
        out = svc.enqueue(prompt="Explain UTC briefly.", source="test")
        self.assertIn("item_id", out)
        self.assertGreaterEqual(text_task_queue.depth(), before + 1)
        text_task_queue.dequeue()

    def test_daily_limit_blocks_process_one(self) -> None:
        svc = TextNurtureService()
        settings = {
            "enabled": True,
            "max_per_hour": 0,
            "max_per_account_per_day": 1,
            "daily_reset_tz": "Asia/Singapore",
            "turns_per_session": 1,
            "turn_gap_sec": 0.0,
            "require_persist_history": False,
            "prompts": ["hello"],
            "session_follow_up_prompts": ["more"],
            "model": "auto",
            "count_manual_toward_daily_limit": True,
        }
        account = {
            "access_token": "tok-1",
            "email": "cap@example.com",
            "status": "正常",
            "proxy_binding_hash": "bind-a",
            "chat_persist_history": True,
        }
        svc._increment_daily_count("cap@example.com", settings, amount=1)
        with patch("services.text_nurture_service._settings", return_value=settings), patch(
            "services.text_nurture_service.account_service.get_account", return_value=account
        ), patch("services.text_nurture_service.collect_text", return_value="ok") as collect_mock, patch.object(
            svc, "_slot_allowed", return_value=True
        ):
            with self.assertRaises(RuntimeError):
                svc.process_one(
                    {
                        "prompt": "hello",
                        "access_token": "tok-1",
                        "email": "cap@example.com",
                        "source": "manual",
                    }
                )
            collect_mock.assert_not_called()

    def test_turns_per_session_calls_collect_text_multiple_times(self) -> None:
        svc = TextNurtureService()
        settings = {
            "enabled": True,
            "max_per_hour": 0,
            "max_per_account_per_day": 6,
            "daily_reset_tz": "Asia/Singapore",
            "turns_per_session": 2,
            "turn_gap_sec": 0.0,
            "require_persist_history": False,
            "prompts": ["hello"],
            "session_follow_up_prompts": ["follow up"],
            "model": "auto",
            "count_manual_toward_daily_limit": True,
        }
        account = {
            "access_token": "tok-2",
            "email": "turns@example.com",
            "status": "正常",
            "proxy_binding_hash": "bind-b",
            "chat_persist_history": True,
        }
        backend = MagicMock()
        backend.account = dict(account)
        with patch("services.text_nurture_service._settings", return_value=settings), patch(
            "services.text_nurture_service.account_service.get_account", return_value=account
        ), patch("services.text_nurture_service.OpenAIBackendAPI", return_value=backend), patch(
            "services.text_nurture_service.collect_text", side_effect=["one", "two"]
        ) as collect_mock, patch.object(svc, "_slot_allowed", return_value=True), patch(
            "services.text_nurture_service.log_llm_ops"
        ):
            out = svc.process_one(
                {
                    "prompt": "hello",
                    "access_token": "tok-2",
                    "email": "turns@example.com",
                    "source": "manual",
                }
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["turns"], 2)
        self.assertEqual(collect_mock.call_count, 2)


class LlmOpsAgentTests(unittest.TestCase):
    def test_list_tools_readonly_majority(self) -> None:
        tools = list_tools()
        self.assertTrue(any(t["name"] == "get_schedulable_breakdown" for t in tools))
        mutate = [t for t in tools if t["mutate"]]
        self.assertEqual(mutate, [])

    def test_run_agent_empty_pool_playbook(self) -> None:
        fake_breakdown = {
            "total": 4,
            "buckets": {"schedulable": 0, "excluded_by_status": 2, "excluded_by_receive_state": 2},
            "primary_reason_counts": {"status": 2, "receive_state": 2},
            "runtime": {
                "ready_candidate_count": 0,
                "dispatchable_candidate_count": 0,
                "image_inflight_count": 0,
                "preflight_backoff_count": 0,
            },
        }
        with patch("services.llm_ops_agent.invoke_tool") as inv:
            inv.side_effect = lambda name, args=None: {
                "tool": name,
                "ok": True,
                "result": (
                    fake_breakdown
                    if name == "get_schedulable_breakdown"
                    else {"accounts": {"schedulable": 0}}
                    if name == "get_health"
                    else {"items": []}
                ),
            }
            out = run_agent("为什么空池不可调度")
        self.assertIn("get_schedulable_breakdown", out["plan"])
        self.assertTrue(out["summary"])
        self.assertIn("可调度账号", out["summary"])


if __name__ == "__main__":
    unittest.main()
