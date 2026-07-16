from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module
from services.image_task_service import (
    ImageTaskService,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_TIMEOUT_PENDING,
)


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


class SyncAdmissionEtaUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.service = ImageTaskService(
            Path(self._tmpdir.name) / "tasks.db",
            submit_workers_getter=lambda: 1,
            poll_workers_getter=lambda: 0,
            global_queue_max_getter=lambda: 100,
            per_user_running_max_getter=lambda: 2,
            per_user_queue_max_getter=lambda: 50,
            deadlock_guard=None,
        )
        self.identity = {"id": "admin", "name": "管理员", "role": "admin"}

    def test_ewma_updates_after_success_duration(self):
        before = self.service.success_duration_ewma_secs()
        self.service.note_success_duration_ms(120_000)
        after = self.service.success_duration_ewma_secs()
        self.assertGreater(after, before)
        self.assertLessEqual(after, 180.0)

    def test_eta_grows_with_unfinished_and_waiters(self):
        with self.service._condition:
            for index in range(4):
                task_id = f"t-{index}"
                key = f"admin:{task_id}"
                self.service._tasks[key] = {
                    "id": task_id,
                    "owner_id": "admin",
                    "status": TASK_STATUS_QUEUED if index else TASK_STATUS_RUNNING,
                    "mode": "generate",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                }
        with mock.patch.object(self.service, "_effective_per_user_running_max_locked", return_value=2):
            with mock.patch("services.image_task_service.config") as cfg:
                cfg.image_global_concurrency = 6
                eta = self.service.estimate_sync_eta_secs(self.identity, extra_waiters=2)
        # ahead=4 unfinished + 2 waiters = 6; slots=2; batches=3; ewma≈60 => ~180
        self.assertGreaterEqual(eta, 150)
        self.assertLessEqual(eta, 180)

    def test_eta_ignores_timeout_pending_and_resume_polling_tasks(self):
        with self.service._condition:
            self.service._tasks["admin:queued"] = {
                "id": "queued",
                "owner_id": "admin",
                "status": TASK_STATUS_QUEUED,
                "created_ts": time.time(),
                "updated_ts": time.time(),
            }
            for index in range(4):
                self.service._tasks[f"admin:pending-{index}"] = {
                    "id": f"pending-{index}",
                    "owner_id": "admin",
                    "status": TASK_STATUS_TIMEOUT_PENDING,
                    "conversation_id": f"conv-pending-{index}",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                }
                self.service._tasks[f"admin:resume-{index}"] = {
                    "id": f"resume-{index}",
                    "owner_id": "admin",
                    "status": TASK_STATUS_RUNNING,
                    "progress": "resume_polling",
                    "conversation_id": f"conv-resume-{index}",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                }

        with mock.patch.object(self.service, "_effective_per_user_running_max_locked", return_value=2):
            with mock.patch("services.image_task_service.config") as cfg:
                cfg.image_global_concurrency = 6
                eta = self.service.estimate_sync_eta_secs(self.identity)

        self.assertEqual(eta, 60)

    def test_owner_queue_limit_ignores_recovery_backlog(self):
        self.service.per_user_queue_max_getter = lambda: 1
        with self.service._condition:
            for index in range(2):
                self.service._tasks[f"admin:pending-{index}"] = {
                    "id": f"pending-{index}",
                    "owner_id": "admin",
                    "status": TASK_STATUS_TIMEOUT_PENDING,
                    "conversation_id": f"conv-pending-{index}",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                }
            self.service._tasks["admin:resume"] = {
                "id": "resume",
                "owner_id": "admin",
                "status": TASK_STATUS_RUNNING,
                "progress": "resume_polling",
                "conversation_id": "conv-resume",
                "created_ts": time.time(),
                "updated_ts": time.time(),
            }

            self.service._enforce_queue_limits_locked("admin")

    def test_submit_pacing_blocks_second_start(self):
        self.service._last_submit_start_ts = time.time()
        with self.service._condition:
            self.service._tasks["admin:pace-1"] = {
                "id": "pace-1",
                "owner_id": "admin",
                "status": TASK_STATUS_QUEUED,
                "mode": "generate",
                "payload": {"model": "gpt-image-2"},
                "identity": dict(self.identity),
                "created_ts": time.time(),
                "updated_ts": time.time(),
            }
            with mock.patch.object(
                self.service,
                "_queue_settings",
                return_value={"submit_start_min_interval_ms": 1500, "burst_enabled": False},
            ):
                first = self.service._next_submit_task_locked()
        self.assertIsNone(first)

    def test_soft_burst_raises_when_queue_and_dispatchable_ready(self):
        with self.service._condition:
            for index in range(4):
                self.service._tasks[f"admin:q-{index}"] = {
                    "id": f"q-{index}",
                    "owner_id": "admin",
                    "status": TASK_STATUS_QUEUED,
                    "created_ts": time.time(),
                }
            with mock.patch.object(
                self.service,
                "_queue_settings",
                return_value={
                    "per_user_running_base": 2,
                    "per_user_running_max": 2,
                    "per_user_running_burst": 3,
                    "burst_enabled": True,
                    "burst_min_queued": 4,
                    "burst_min_dispatchable_candidates": 8,
                    "burst_max_preflight_backoff": 0,
                },
            ):
                with mock.patch.object(self.service, "_deadlock_guard_tripped_locked", return_value=False):
                    with mock.patch(
                        "services.image_task_service.account_service.get_image_candidate_runtime_stats",
                        return_value={
                            "dispatchable_candidate_count": 10,
                            "preflight_backoff_count": 0,
                            "image_inflight_count": 1,
                        },
                    ):
                        limit = self.service._effective_per_user_running_max_locked()
        self.assertEqual(limit, 3)


class SyncAdmissionApiTests(unittest.TestCase):
    def setUp(self):
        self._original = {
            "image_generation_paused": ai_module.config.data.get("image_generation_paused"),
            "image_task_queue": ai_module.config.data.get("image_task_queue"),
            "newapi_image_sync_admission_max": ai_module.config.data.get("newapi_image_sync_admission_max"),
            "newapi_image_sync_admission_max_eta_secs": ai_module.config.data.get("newapi_image_sync_admission_max_eta_secs"),
        }
        ai_module.config.data["image_generation_paused"] = False
        ai_module.config.data["image_task_queue"] = {"enabled": True}
        ai_module.config.data["newapi_image_sync_admission_max"] = 12
        ai_module.config.data["newapi_image_sync_admission_max_eta_secs"] = 180
        self.addCleanup(self._restore)
        ai_module._IMAGE_SYNC_WAIT_INFLIGHT = 0

        def fake_sync(identity, **kwargs):
            return {"created": 1, "data": [{"b64_json": "ZmFrZQ=="}], "task_id": "sync-x"}

        self.sync_patcher = mock.patch.object(ai_module, "run_generation_sync", fake_sync)
        self.filter_patcher = mock.patch.object(ai_module, "filter_or_log", mock.AsyncMock())
        self.sync_patcher.start()
        self.filter_patcher.start()
        self.addCleanup(self.sync_patcher.stop)
        self.addCleanup(self.filter_patcher.stop)
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def _restore(self):
        for key, value in self._original.items():
            if value is None:
                ai_module.config.data.pop(key, None)
            else:
                ai_module.config.data[key] = value
        ai_module._IMAGE_SYNC_WAIT_INFLIGHT = 0

    def test_inflight_busy_still_admits_when_eta_ok(self):
        with mock.patch.object(ai_module.image_task_service, "estimate_sync_eta_secs", return_value=30):
            admitted, eta = ai_module._try_enter_image_sync_admission({"id": "admin"})
        self.assertTrue(admitted)
        self.assertEqual(eta, 30)
        ai_module._leave_image_sync_admission()

    def test_eta_exceeded_returns_429(self):
        with mock.patch.object(ai_module.image_task_service, "estimate_sync_eta_secs", return_value=240):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={"model": "gpt-image-2", "prompt": "eta too high"},
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["error"]["code"], "image_service_busy")
        self.assertEqual(response.headers.get("Retry-After"), "240")

    def test_seats_full_returns_429(self):
        ai_module.config.data["newapi_image_sync_admission_max"] = 1
        ai_module._IMAGE_SYNC_WAIT_INFLIGHT = 1
        with mock.patch.object(ai_module.image_task_service, "estimate_sync_eta_secs", return_value=90):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={"model": "gpt-image-2", "prompt": "seats full"},
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["error"]["code"], "image_service_busy")


if __name__ == "__main__":
    unittest.main()
