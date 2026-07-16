from __future__ import annotations

import random
import unittest

from services.account_workload_policy import (
    AccountWorkloadCapabilities,
    WorkloadAction,
    WorkloadSnapshot,
    decide_account_workload,
    image_reserve_count,
    pick_equal_priority_text_task,
)


class ImageReservePolicyTests(unittest.TestCase):
    def test_small_pool_reserves_every_image_capable_account(self) -> None:
        self.assertEqual(image_reserve_count(0), 0)
        self.assertEqual(image_reserve_count(5), 5)
        self.assertEqual(image_reserve_count(9), 9)

    def test_pool_of_ten_keeps_twenty_percent_headroom(self) -> None:
        self.assertEqual(image_reserve_count(10), 8)
        self.assertEqual(image_reserve_count(11), 9)

    def test_snapshot_rejects_inconsistent_free_capacity(self) -> None:
        with self.assertRaises(ValueError):
            WorkloadSnapshot(
                dispatchable_image_accounts=5,
                free_image_accounts=6,
                image_queue=0,
                text_queue=0,
            )


class AccountWorkloadPolicyTests(unittest.TestCase):
    def test_five_account_pool_does_not_take_an_image_candidate_for_text(self) -> None:
        snapshot = WorkloadSnapshot(
            dispatchable_image_accounts=5,
            free_image_accounts=5,
            image_queue=0,
            text_queue=2,
        )
        account = AccountWorkloadCapabilities(
            text_healthy=True,
            image_eligible=True,
            node_bound=True,
        )

        decision = decide_account_workload(snapshot, account)

        self.assertEqual(decision.action, WorkloadAction.IDLE)
        self.assertEqual(decision.image_reserve, 5)

    def test_text_only_account_can_handle_text_while_images_are_queued(self) -> None:
        snapshot = WorkloadSnapshot(
            dispatchable_image_accounts=5,
            free_image_accounts=5,
            image_queue=4,
            text_queue=2,
        )
        account = AccountWorkloadCapabilities(
            text_healthy=True,
            image_eligible=False,
            node_bound=True,
        )

        decision = decide_account_workload(snapshot, account)

        self.assertEqual(decision.action, WorkloadAction.TEXT)
        self.assertEqual(decision.dispatch_delay_seconds, 0.0)

    def test_pool_of_ten_can_use_only_capacity_above_image_reserve(self) -> None:
        account = AccountWorkloadCapabilities(
            text_healthy=True,
            image_eligible=True,
            node_bound=True,
        )
        admitted = decide_account_workload(
            WorkloadSnapshot(
                dispatchable_image_accounts=10,
                free_image_accounts=9,
                image_queue=0,
                text_queue=1,
            ),
            account,
        )
        reserved = decide_account_workload(
            WorkloadSnapshot(
                dispatchable_image_accounts=10,
                free_image_accounts=8,
                image_queue=0,
                text_queue=1,
            ),
            account,
        )

        self.assertEqual(admitted.action, WorkloadAction.TEXT)
        self.assertEqual(admitted.image_reserve, 8)
        self.assertEqual(reserved.action, WorkloadAction.IDLE)

    def test_image_queue_has_priority_without_random_delay(self) -> None:
        snapshot = WorkloadSnapshot(
            dispatchable_image_accounts=10,
            free_image_accounts=10,
            image_queue=1,
            text_queue=3,
        )
        account = AccountWorkloadCapabilities(
            text_healthy=True,
            image_eligible=True,
            node_bound=True,
        )

        decision = decide_account_workload(snapshot, account)

        self.assertEqual(decision.action, WorkloadAction.IMAGE)
        self.assertEqual(decision.dispatch_delay_seconds, 0.0)

    def test_empty_queues_leave_account_idle(self) -> None:
        decision = decide_account_workload(
            WorkloadSnapshot(
                dispatchable_image_accounts=5,
                free_image_accounts=5,
                image_queue=0,
                text_queue=0,
            ),
            AccountWorkloadCapabilities(
                text_healthy=True,
                image_eligible=True,
                node_bound=True,
            ),
        )

        self.assertEqual(decision.action, WorkloadAction.IDLE)

    def test_text_requires_healthy_account_and_bound_node(self) -> None:
        snapshot = WorkloadSnapshot(
            dispatchable_image_accounts=0,
            free_image_accounts=0,
            image_queue=0,
            text_queue=1,
        )
        unhealthy = decide_account_workload(
            snapshot,
            AccountWorkloadCapabilities(
                text_healthy=False,
                image_eligible=False,
                node_bound=True,
            ),
        )
        unbound = decide_account_workload(
            snapshot,
            AccountWorkloadCapabilities(
                text_healthy=True,
                image_eligible=False,
                node_bound=False,
            ),
        )

        self.assertEqual(unhealthy.action, WorkloadAction.IDLE)
        self.assertEqual(unbound.action, WorkloadAction.IDLE)

    def test_randomness_only_breaks_ties_between_real_text_tasks(self) -> None:
        tasks = ["task-a", "task-b", "task-c"]
        tie_break = random.Random(7).random

        selected = pick_equal_priority_text_task(tasks, tie_break=tie_break)

        self.assertEqual(selected, "task-b")
        self.assertIsNone(pick_equal_priority_text_task([], tie_break=tie_break))


if __name__ == "__main__":
    unittest.main()
