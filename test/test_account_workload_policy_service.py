from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from services.account_workload_policy import WorkloadAction
from services.account_workload_policy_service import AccountWorkloadPolicyService, account_node_bound


class AccountWorkloadPolicyServiceTests(unittest.TestCase):
    def test_node_lease_expired_unbounds(self) -> None:
        self.assertTrue(account_node_bound({"proxy": "http://x"}))
        self.assertFalse(
            account_node_bound(
                {
                    "node_lease_id": "lease-1",
                    "node_bound_until": "2000-01-01T00:00:00+00:00",
                }
            )
        )

    def test_live_canary_rimg_exempt_admits_text(self) -> None:
        token = "canary-token-xyz"
        th = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        service = AccountWorkloadPolicyService()
        account = {
            "access_token": token,
            "status": "正常",
            "proxy": "http://user:pass@host:1",
            "panda_receive_state": "verified_ready",
            "quota": 1,
        }

        with patch.object(
            service,
            "settings",
            return_value={
                "mode": "live",
                "text_queue_mode": "off",
                "canary_token_hashes": [th],
                "global_text_inflight": 1,
            },
        ), patch.object(
            service,
            "build_snapshot",
            side_effect=lambda **_: __import__(
                "services.account_workload_policy", fromlist=["WorkloadSnapshot"]
            ).WorkloadSnapshot(
                dispatchable_image_accounts=1,
                free_image_accounts=1,
                image_queue=0,
                text_queue=1,
            ),
        ), patch.object(
            service,
            "capabilities_for",
            return_value=__import__(
                "services.account_workload_policy", fromlist=["AccountWorkloadCapabilities"]
            ).AccountWorkloadCapabilities(
                text_healthy=True,
                image_eligible=True,
                node_bound=True,
            ),
        ):
            gate = service.decide_for_account(account, "text", access_token=token, force_text_demand=True)

        self.assertTrue(gate.admitted)
        self.assertEqual(gate.decision.action, WorkloadAction.TEXT)
        self.assertEqual(gate.decision.reason, "canary_rimg_exempt")


if __name__ == "__main__":
    unittest.main()
