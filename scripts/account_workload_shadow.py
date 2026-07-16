from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.account_workload_policy import (  # noqa: E402
    AccountWorkloadCapabilities,
    WorkloadSnapshot,
    decide_account_workload,
    image_reserve_count,
    pick_equal_priority_text_task,
)


def evaluate_shadow(payload: dict[str, Any], *, seed: int) -> dict[str, Any]:
    """读取静态快照并输出决策，全程不连接服务、不发送请求。"""

    snapshot = WorkloadSnapshot(**payload["snapshot"])
    decisions: list[dict[str, Any]] = []
    for raw_account in payload.get("accounts", []):
        account = AccountWorkloadCapabilities(
            text_healthy=raw_account["text_healthy"],
            image_eligible=raw_account["image_eligible"],
            node_bound=raw_account["node_bound"],
        )
        decision = decide_account_workload(snapshot, account)
        decisions.append(
            {
                "account_id": raw_account.get("account_id"),
                "action": decision.action.value,
                "reason": decision.reason,
                "image_reserve": decision.image_reserve,
                "dispatch_delay_seconds": decision.dispatch_delay_seconds,
            }
        )

    tasks = payload.get("equal_priority_text_tasks", [])
    selected_task = pick_equal_priority_text_task(
        tasks,
        tie_break=random.Random(seed).random,
    )
    return {
        "mode": "shadow",
        "image_reserve": image_reserve_count(snapshot.dispatchable_image_accounts),
        "decisions": decisions,
        "selected_equal_priority_text_task": selected_task,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="离线验证账号工作负载调度策略")
    parser.add_argument("snapshot", type=Path, help="JSON 快照文件")
    parser.add_argument("--seed", type=int, default=0, help="文本同优先级打散种子")
    args = parser.parse_args()

    with args.snapshot.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    result = evaluate_shadow(payload, seed=args.seed)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
