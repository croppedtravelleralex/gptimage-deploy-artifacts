from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar


TaskT = TypeVar("TaskT")


class WorkloadAction(str, Enum):
    """账号下一步可承载的真实工作类型。"""

    IMAGE = "image"
    TEXT = "text"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class WorkloadSnapshot:
    """某一调度时刻的只读队列快照。

    ``dispatchable_image_accounts``（D）只统计当前可承载生图的账号；
    ``free_image_accounts``（F）是其中尚未占用的账号。文本专用账号不占 D/F；
    两个队列字段均只统计真实外部任务。
    """

    dispatchable_image_accounts: int
    free_image_accounts: int
    image_queue: int
    text_queue: int

    def __post_init__(self) -> None:
        for name in (
            "dispatchable_image_accounts",
            "free_image_accounts",
            "image_queue",
            "text_queue",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.free_image_accounts > self.dispatchable_image_accounts:
            raise ValueError(
                "free_image_accounts must not exceed dispatchable_image_accounts"
            )


@dataclass(frozen=True, slots=True)
class AccountWorkloadCapabilities:
    """待决策账号的能力；调用方应只传入当前空闲账号。"""

    text_healthy: bool
    image_eligible: bool
    node_bound: bool

    def __post_init__(self) -> None:
        for name in ("text_healthy", "image_eligible", "node_bound"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class WorkloadDecision:
    """纯策略输出，不执行请求，也不改变队列。"""

    action: WorkloadAction
    reason: str
    image_reserve: int
    dispatch_delay_seconds: float = 0.0


def image_reserve_count(dispatchable_image_accounts: int) -> int:
    """计算生图账号硬保留量 Rimg。"""

    if (
        isinstance(dispatchable_image_accounts, bool)
        or not isinstance(dispatchable_image_accounts, int)
        or dispatchable_image_accounts < 0
    ):
        raise ValueError("dispatchable_image_accounts must be a non-negative integer")
    if dispatchable_image_accounts < 10:
        return dispatchable_image_accounts
    return (4 * dispatchable_image_accounts + 4) // 5


def decide_account_workload(
    snapshot: WorkloadSnapshot,
    account: AccountWorkloadCapabilities,
) -> WorkloadDecision:
    """为单个空闲账号给出 shadow 决策。

    生图队列对可生图账号始终优先且零延迟。文本只使用文本专用账号，
    或使用无生图排队时高于 Rimg 的空闲生图余量。
    """

    reserve = image_reserve_count(snapshot.dispatchable_image_accounts)

    if not account.node_bound:
        return WorkloadDecision(WorkloadAction.IDLE, "node_not_bound", reserve)

    if snapshot.image_queue > 0 and account.image_eligible:
        return WorkloadDecision(WorkloadAction.IMAGE, "image_queue_priority", reserve)

    if snapshot.text_queue == 0:
        reason = "queues_empty" if snapshot.image_queue == 0 else "no_compatible_work"
        return WorkloadDecision(WorkloadAction.IDLE, reason, reserve)

    if not account.text_healthy:
        return WorkloadDecision(WorkloadAction.IDLE, "text_unhealthy", reserve)

    if not account.image_eligible:
        return WorkloadDecision(WorkloadAction.TEXT, "text_only_capacity", reserve)

    if snapshot.image_queue > 0:
        return WorkloadDecision(WorkloadAction.IDLE, "image_queue_reserved", reserve)

    if snapshot.free_image_accounts > reserve:
        return WorkloadDecision(WorkloadAction.TEXT, "capacity_above_image_reserve", reserve)

    return WorkloadDecision(WorkloadAction.IDLE, "image_reserve_protected", reserve)


def pick_equal_priority_text_task(
    tasks: Sequence[TaskT],
    *,
    tie_break: Callable[[], float],
) -> TaskT | None:
    """仅在同优先级真实文本任务之间使用调用方注入的打散值。"""

    if not tasks:
        return None

    selected_index = min(
        range(len(tasks)),
        key=lambda index: (float(tie_break()), index),
    )
    return tasks[selected_index]
