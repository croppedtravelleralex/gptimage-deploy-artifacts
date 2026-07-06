from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from services.image_task_service import (
    ImageTaskService,
    ImageTaskWaitTimeoutError,
    TASK_STATUS_ERROR,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_TIMEOUT_PENDING,
)
from services.openai_backend_api import InvalidAccessTokenError


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None, **kwargs) -> ImageTaskService:
        service = ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
            **kwargs,
        )
        self.addCleanup(service.stop)
        return service

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_constructor_does_not_recover_unfinished_tasks_until_runtime_start(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["queued", "running"])

            service.start_background()
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))

    def test_worker_limit_prevents_submit_thread_fanout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            started: list[str] = []
            release = threading.Event()
            started_event = threading.Event()
            lock = threading.Lock()

            def handler(payload):
                with lock:
                    started.append(str(payload.get("prompt")))
                started_event.set()
                release.wait(timeout=2)
                return {"data": [{"url": f"http://example.test/{payload.get('prompt')}.png"}]}

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
            )
            for index in range(3):
                service.submit_generation(
                    OWNER,
                    client_task_id=f"queued-{index}",
                    prompt=f"cat-{index}",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )

            self.assertTrue(started_event.wait(timeout=1))
            time.sleep(0.1)
            with lock:
                self.assertEqual(started, ["cat-0"])

            queued = service.list_tasks(OWNER, ["queued-1", "queued-2"])["items"]
            self.assertEqual([item["status"] for item in queued], ["queued", "queued"])

            release.set()
            wait_for_task(service, OWNER, "queued-2", "success", timeout=3)

    def test_lightweight_status_omits_result_data_and_reports_queue_position(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            release = threading.Event()
            started_event = threading.Event()

            def handler(_payload):
                started_event.set()
                release.wait(timeout=2)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
                per_user_queue_max_getter=lambda: 36,
                per_user_running_max_getter=lambda: 2,
            )
            service.submit_generation(
                OWNER,
                client_task_id="running-task",
                prompt="cat-0",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            self.assertTrue(started_event.wait(timeout=1))
            service.submit_generation(
                OWNER,
                client_task_id="queued-task",
                prompt="cat-1",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            status = service.list_task_statuses(OWNER, ["running-task", "queued-task"])["items"]
            by_id = {item["id"]: item for item in status}

            self.assertNotIn("data", by_id["running-task"])
            self.assertEqual(by_id["running-task"]["running_limit"], 2)
            self.assertEqual(by_id["running-task"]["accepted_limit"], 36)
            self.assertEqual(by_id["queued-task"]["queue_position"], 1)
            self.assertEqual(by_id["queued-task"]["estimated_start_after_secs"], 0)

            release.set()
            service.stop()


    def test_submit_hard_timeout_marks_error_and_late_handler_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            release = threading.Event()
            started = threading.Event()

            def handler(payload):
                started.set()
                callback = payload.get("progress_callback")
                if callable(callback):
                    callback("getting_account")
                release.wait(timeout=3)
                return {"data": [{"url": "http://example.test/late.png"}]}

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
            )
            key = "owner-1:hard-timeout-task"
            with service._condition:
                service._tasks[key] = {
                    "id": "hard-timeout-task",
                    "owner_id": "owner-1",
                    "status": TASK_STATUS_RUNNING,
                    "mode": "generate",
                    "model": "gpt-image-2",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                }
                service._save_task_locked(key)
            service._run_task(
                key,
                "generate",
                {"prompt": "cat", "poll_timeout_secs": 0.05, "task_hard_timeout_secs": 0.2},
                OWNER,
                "gpt-image-2",
            )
            self.assertTrue(started.wait(timeout=1))
            task = service.list_tasks(OWNER, ["hard-timeout-task"])["items"][0]

            self.assertEqual(task["status"], TASK_STATUS_ERROR)
            self.assertIn("hard timeout", task["error"])
            release.set()
            time.sleep(0.1)
            task_after_late_return = service.list_tasks(OWNER, ["hard-timeout-task"])["items"][0]
            self.assertEqual(task_after_late_return["status"], TASK_STATUS_ERROR)
            self.assertIn("hard timeout", task_after_late_return["error"])

    def test_submit_hard_timeout_force_releases_leased_image_slot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            release = threading.Event()
            started = threading.Event()

            def handler(payload):
                started.set()
                callback = payload.get("progress_callback")
                if callable(callback):
                    callback({"step": "getting_account", "access_token": "token-stuck"})
                release.wait(timeout=3)
                return {"data": [{"url": "http://example.test/late.png"}]}

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
            )
            key = "owner-1:hard-timeout-release-task"
            with service._condition:
                service._tasks[key] = {
                    "id": "hard-timeout-release-task",
                    "owner_id": "owner-1",
                    "status": TASK_STATUS_RUNNING,
                    "mode": "generate",
                    "model": "gpt-image-2",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                }
                service._save_task_locked(key)

            with mock.patch("services.image_task_service.account_service.release_image_slot") as release_slot:
                service._run_task(
                    key,
                    "generate",
                    {"prompt": "cat", "poll_timeout_secs": 0.05, "task_hard_timeout_secs": 0.2},
                    OWNER,
                    "gpt-image-2",
                )

            self.assertTrue(started.wait(timeout=1))
            release_slot.assert_called_once_with("token-stuck")
            with service._condition:
                stored = service._tasks[key]
                self.assertEqual(stored["status"], TASK_STATUS_ERROR)
                self.assertEqual(stored["force_released_inflight_count"], 1)
            release.set()

    def test_timeout_with_conversation_id_becomes_timeout_pending(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            class TimeoutWithConversation(RuntimeError):
                conversation_id = "conv-timeout-1"
                code = "image_timeout_pending"

            def handler(_payload):
                raise TimeoutWithConversation("ChatGPT 生图超时（已等待 120 秒）。")

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
            )
            service.submit_generation(
                OWNER,
                client_task_id="timeout-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            task = wait_for_task(service, OWNER, "timeout-task", TASK_STATUS_TIMEOUT_PENDING)
            self.assertEqual(task["conversation_id"], "conv-timeout-1")
            self.assertIn("超时", task["error"])

    def test_resume_poll_uses_backend_without_proxy_url_kwarg(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            created_args = []

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    created_args.append({"args": args, "kwargs": kwargs})

                def _poll_image_results(self, _conversation_id, _timeout_secs):
                    return ["file-1"], []

                def resolve_conversation_image_urls(self, _conversation_id, _file_ids, _sediment_ids, poll=False):
                    return ["https://example.test/image.png"]

                def download_image_bytes(self, _image_urls):
                    return [b"fake-image"]

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", poll_workers_getter=lambda: 0)
            key = "owner-1:resume-task"
            with service._condition:
                service._tasks[key] = {
                    "id": "resume-task",
                    "owner_id": "owner-1",
                    "status": TASK_STATUS_RUNNING,
                    "mode": "edit",
                    "model": "gpt-image-2",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                    "resume_attempts": 1,
                    "progress": "resume_polling",
                }
                service._save_task_locked(key)

            with mock.patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend):
                service._run_resume_poll(key, "conv-1", 5.0, OWNER, "edit", "gpt-image-2")

            result = service.list_tasks(OWNER, ["resume-task"])["items"][0]
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["data"][0]["b64_json"], "ZmFrZS1pbWFnZQ==")
            self.assertEqual(created_args, [{"args": (), "kwargs": {}}])

    def test_resume_poll_uses_stored_access_token_when_available(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            created_args = []

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    created_args.append({"args": args, "kwargs": kwargs})

                def _poll_image_results(self, _conversation_id, _timeout_secs):
                    return ["file-1"], []

                def resolve_conversation_image_urls(self, _conversation_id, _file_ids, _sediment_ids, poll=False):
                    return ["https://example.test/image.png"]

                def download_image_bytes(self, _image_urls):
                    return [b"fake-image"]

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", poll_workers_getter=lambda: 0)
            key = "owner-1:resume-token-task"
            with service._condition:
                service._tasks[key] = {
                    "id": "resume-token-task",
                    "owner_id": "owner-1",
                    "status": TASK_STATUS_RUNNING,
                    "mode": "edit",
                    "model": "gpt-image-2",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                    "resume_attempts": 1,
                    "resume_access_token": "token-live",
                    "progress": "resume_polling",
                }
                service._save_task_locked(key)

            with mock.patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend):
                service._run_resume_poll(key, "conv-1", 5.0, OWNER, "edit", "gpt-image-2")

            self.assertEqual(created_args, [{"args": (), "kwargs": {"access_token": "token-live"}}])

    def test_resume_poll_token_invalidated_stays_timeout_pending_until_attempts_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    pass

                def _poll_image_results(self, _conversation_id, _timeout_secs):
                    raise InvalidAccessTokenError("token invalidated during image poll task check (conv-1)")

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                poll_workers_getter=lambda: 0,
                timeout_pending_max_attempts_getter=lambda: 3,
            )
            key = "owner-1:resume-invalid-token-task"
            with service._condition:
                service._tasks[key] = {
                    "id": "resume-invalid-token-task",
                    "owner_id": "owner-1",
                    "status": TASK_STATUS_RUNNING,
                    "mode": "edit",
                    "model": "gpt-image-2",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                    "resume_attempts": 1,
                    "resume_access_token": "token-dead",
                    "progress": "resume_polling",
                }
                service._save_task_locked(key)

            with (
                mock.patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend),
                mock.patch("services.image_task_service.account_service.refresh_access_token", return_value="token-new"),
            ):
                service._run_resume_poll(key, "conv-1", 5.0, OWNER, "edit", "gpt-image-2")

            result = service.list_tasks(OWNER, ["resume-invalid-token-task"])["items"][0]
            self.assertEqual(result["status"], TASK_STATUS_TIMEOUT_PENDING)
            with service._condition:
                stored = service._tasks[key]
                self.assertEqual(stored["resume_access_token"], "token-new")
                self.assertGreater(float(stored["next_resume_ts"]), 0)

    def test_timeout_pending_task_stores_resume_timeout_and_access_token_from_exception(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            class TimeoutWithConversation(RuntimeError):
                conversation_id = "conv-timeout-2"
                code = "image_timeout_pending"
                access_token = "token-for-resume"

            def handler(_payload):
                raise TimeoutWithConversation("ChatGPT 生图超时（已等待 300 秒）。")

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
                timeout_pending_poll_secs_getter=lambda: 300,
            )
            service.submit_edit(
                OWNER,
                client_task_id="timeout-resume-meta-task",
                prompt="edit",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
                images=[(b"img", "img.png", "image/png")],
            )

            task = wait_for_task(service, OWNER, "timeout-resume-meta-task", TASK_STATUS_TIMEOUT_PENDING)
            self.assertEqual(task["conversation_id"], "conv-timeout-2")
            with service._condition:
                stored = service._tasks["owner-1:timeout-resume-meta-task"]
                self.assertGreaterEqual(float(stored["resume_timeout_secs"]), 300.0)
                self.assertEqual(stored["resume_access_token"], "token-for-resume")

    def test_edit_asset_ids_are_resolved_by_worker(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            seen_payloads = []

            class FakeAssetService:
                def read_assets(self, _identity, asset_ids):
                    self.asset_ids = list(asset_ids)
                    return [(b"asset-bytes", "asset.png", "image/png")]

            fake_asset_service = FakeAssetService()

            def handler(payload):
                seen_payloads.append(payload)
                return {"data": [{"url": "http://example.test/asset-edit.png"}]}

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
            )
            with mock.patch("services.image_asset_service.image_asset_service", fake_asset_service):
                service.submit_edit(
                    OWNER,
                    client_task_id="asset-edit-task",
                    prompt="edit",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                    image_asset_ids=["asset-1"],
                )
                task = wait_for_task(service, OWNER, "asset-edit-task", "success")

            self.assertEqual(task["data"][0]["url"], "http://example.test/asset-edit.png")
            self.assertEqual(fake_asset_service.asset_ids, ["asset-1"])
            self.assertEqual(seen_payloads[0]["images"], [(b"asset-bytes", "asset.png", "image/png")])
            self.assertNotIn("image_asset_ids", seen_payloads[0])

    def test_wait_for_result_returns_success_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json", submit_workers_getter=lambda: 1, poll_workers_getter=lambda: 0)
            service.submit_generation(
                OWNER,
                client_task_id="wait-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "wait-task", TASK_STATUS_SUCCESS)
            task = service.wait_for_result(OWNER, "wait-task", timeout_secs=2.0, poll_interval_secs=0.05)
            self.assertEqual(task["status"], TASK_STATUS_SUCCESS)
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")

    def test_wait_for_result_raises_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            def slow_handler(_payload):
                time.sleep(0.2)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(
                Path(tmp_dir) / "image_tasks.json",
                slow_handler,
                submit_workers_getter=lambda: 1,
                poll_workers_getter=lambda: 0,
            )
            service.submit_generation(
                OWNER,
                client_task_id="slow-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            with self.assertRaises(ImageTaskWaitTimeoutError) as ctx:
                service.wait_for_result(OWNER, "slow-task", timeout_secs=0.05, poll_interval_secs=0.02)
            self.assertEqual(ctx.exception.task_id, "slow-task")
            # The timeout path intentionally leaves the task running in the
            # background.  Wait for the worker before the tempdir is removed so
            # the test does not produce a false sqlite "unable to open database"
            # thread warning during teardown.
            wait_for_task(service, OWNER, "slow-task", TASK_STATUS_SUCCESS)


if __name__ == "__main__":
    unittest.main()
