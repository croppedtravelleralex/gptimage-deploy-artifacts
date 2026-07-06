from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_maintenance_loop_service import AccountMaintenanceLoopService


class AccountMaintenanceLoopServiceTests(unittest.TestCase):
    def test_select_tokens_uses_persistent_cursor(self) -> None:
        service = AccountMaintenanceLoopService()
        with tempfile.TemporaryDirectory() as tmp_dir:
            service._cursor_file = Path(tmp_dir) / "cursor.txt"
            settings = {
                "stale_after_hours": 1,
                "include_recent": False,
                "delete_invalid": True,
                "delete_after_failures": 3,
                "expired_grace_hours": 1,
            }
            with patch(
                "services.account_maintenance_loop_service.account_refresh_all_service._build_token_queue",
                return_value=(["token-1", "token-2", "token-3", "token-4"], 0),
            ):
                self.assertEqual(service._select_tokens(2, settings), ["token-1", "token-2"])
                self.assertEqual(service._select_tokens(2, settings), ["token-3", "token-4"])
                self.assertEqual(service._select_tokens(2, settings), ["token-1", "token-2"])

    def test_resource_mode_slows_before_hard_pause(self) -> None:
        service = AccountMaintenanceLoopService()
        settings = {
            "min_available_memory_mb": 256,
            "slow_min_available_memory_mb": 512,
            "slow_when_image_inflight": 2,
            "pause_when_image_inflight": 8,
        }
        with (
            patch(
                "services.account_maintenance_loop_service.account_refresh_all_service._resource_ok",
                return_value=(
                    True,
                    "",
                    {
                        "available_memory_mb": 300,
                        "memory_current_mb": 1236,
                        "memory_limit_mb": 1536,
                        "load_1m": 0.3,
                    },
                ),
            ),
            patch(
                "services.account_maintenance_loop_service.account_service.get_total_image_inflight",
                return_value=4,
            ),
        ):
            mode, reason, resource = service._resource_mode(settings)

        self.assertEqual(mode, "slow")
        self.assertIn("available memory 300MB < slow 512MB", reason)
        self.assertIn("image inflight 4 >= slow 2", reason)
        self.assertEqual(resource["image_inflight"], 4)

    def test_resource_mode_does_not_hard_pause_cleanup_by_default(self) -> None:
        service = AccountMaintenanceLoopService()
        settings = {
            "min_available_memory_mb": 512,
            "slow_min_available_memory_mb": 512,
            "slow_when_image_inflight": 2,
            "pause_when_image_inflight": 2,
            "resource_pause_enabled": False,
        }
        with (
            patch(
                "services.account_maintenance_loop_service.account_refresh_all_service._resource_ok",
                return_value=(
                    False,
                    "available memory 100MB < 512MB",
                    {
                        "available_memory_mb": 100,
                        "memory_current_mb": 1436,
                        "memory_limit_mb": 1536,
                        "load_1m": 0.3,
                    },
                ),
            ),
            patch(
                "services.account_maintenance_loop_service.account_service.get_total_image_inflight",
                return_value=3,
            ),
        ):
            mode, reason, resource = service._resource_mode(settings)

        self.assertEqual(mode, "slow")
        self.assertIn("available memory 100MB < 512MB", reason)
        self.assertIn("image inflight 3 >= pause 2", reason)
        self.assertEqual(resource["image_inflight"], 3)

    def test_resource_mode_pauses_when_image_deadlock_guard_tripped(self) -> None:
        service = AccountMaintenanceLoopService()
        settings = {
            "min_available_memory_mb": 512,
            "slow_min_available_memory_mb": 512,
            "resource_pause_enabled": False,
        }
        with (
            patch(
                "services.account_maintenance_loop_service.account_refresh_all_service._resource_ok",
                return_value=(True, "", {"available_memory_mb": 1024}),
            ),
            patch(
                "services.account_maintenance_loop_service.account_service.get_total_image_inflight",
                return_value=0,
            ),
            patch(
                "services.image_deadlock_guard_service.image_deadlock_guard_service.status",
                return_value={"tripped": True, "reason": "image CPU 91% >= 90%"},
            ),
        ):
            mode, reason, resource = service._resource_mode(settings)

        self.assertEqual(mode, "pause")
        self.assertIn("image CPU 91%", reason)
        self.assertTrue(resource["image_deadlock_guard"]["tripped"])


if __name__ == "__main__":
    unittest.main()
