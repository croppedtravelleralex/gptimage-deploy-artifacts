from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.image_task_service import ImageTaskDuplicatePromptError, ImageTaskService


class PromptDedupParallelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        path = Path(self._tmpdir.name) / "tasks.db"
        self.svc = ImageTaskService(
            path,
            generation_handler=lambda payload: {"data": [{"b64_json": "x"}]},
            submit_workers_getter=lambda: 0,
            poll_workers_getter=lambda: 0,
        )
        # 防止 max(1, 0) 仍拉起 worker 立刻消费队列
        self.svc._ensure_workers_locked = lambda: None  # type: ignore[method-assign]
        self.identity = {"id": "tester", "role": "admin"}

    def _scheduler(self, **overrides):
        base = {
            "enabled": True,
            "prompt_dedup_window_sec": 120,
            "prompt_dedup_max_parallel": 4,
        }
        base.update(overrides)
        return base

    def _patch_cfg(self, cfg, **scheduler_overrides):
        cfg.get_scheduler_settings.return_value = self._scheduler(**scheduler_overrides)
        cfg.get_image_task_queue_settings.return_value = {
            "enabled": True,
            "global_queue_max": 200,
            "per_user_queue_max": 36,
            "per_user_running_max": 6,
            "submit_workers": 0,
            "poll_workers": 0,
        }
        cfg.data = {"image_generation_paused": False}
        cfg.image_generation_paused = False

    def test_allows_four_same_prompt_then_rejects_fifth(self) -> None:
        with patch("services.image_task_service.config") as cfg, patch(
            "services.image_task_service._image_generation_paused", return_value=False
        ):
            self._patch_cfg(cfg)
            prompt = "元婴修士打斗的场景"
            for i in range(4):
                task = self.svc.submit_generation(
                    self.identity,
                    client_task_id=f"t-{i}",
                    prompt=prompt,
                    model="gpt-image-2",
                    size="1024x1024",
                )
                self.assertEqual(task.get("status"), "queued")
            with self.assertRaises(ImageTaskDuplicatePromptError):
                self.svc.submit_generation(
                    self.identity,
                    client_task_id="t-4",
                    prompt=prompt,
                    model="gpt-image-2",
                    size="1024x1024",
                )

    def test_rejects_resubmit_after_batch_cleared_within_window(self) -> None:
        with patch("services.image_task_service.config") as cfg, patch(
            "services.image_task_service._image_generation_paused", return_value=False
        ):
            self._patch_cfg(cfg)
            prompt = "同一提示词"
            self.svc.submit_generation(
                self.identity, client_task_id="a1", prompt=prompt, model="gpt-image-2", size="1024x1024"
            )
            # 模拟批次结束：取消/标错清空未完成
            self.svc.cancel_task(self.identity, "a1")
            with self.assertRaises(ImageTaskDuplicatePromptError):
                self.svc.submit_generation(
                    self.identity, client_task_id="a2", prompt=prompt, model="gpt-image-2", size="1024x1024"
                )

    def test_cancel_queued_task(self) -> None:
        with patch("services.image_task_service.config") as cfg, patch(
            "services.image_task_service._image_generation_paused", return_value=False
        ):
            self._patch_cfg(cfg, enabled=False)
            self.svc.submit_generation(
                self.identity, client_task_id="c1", prompt="cancel me", model="gpt-image-2", size="1024x1024"
            )
            out = self.svc.cancel_task(self.identity, "c1")
            self.assertEqual(out.get("status"), "error")
            self.assertIn("cancelled", str(out.get("error") or ""))


if __name__ == "__main__":
    unittest.main()
