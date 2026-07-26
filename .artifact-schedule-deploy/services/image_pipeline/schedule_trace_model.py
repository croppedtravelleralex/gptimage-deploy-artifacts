"""Python fallback for schedule trace phase model (mirrors Rust model.rs)."""
from __future__ import annotations

from typing import Any

KIND = {
    1: "task_queued",
    2: "task_worker_start",
    3: "pipeline_admit",
    4: "account_wait_start",
    5: "account_acquired",
    6: "ready_buffer_wait_start",
    7: "ready_buffer_wait_end",
    8: "ss_queue_enter",
    9: "ss_slot_acquired",
    10: "ss_slot_released",
    11: "sse_stream_end",
    12: "poll_resolve_end",
    13: "download_start",
    14: "download_end",
    15: "pipeline_finish",
    16: "ps_queue_enter",
    17: "ps_slot_acquired",
    18: "ps_slot_released",
    19: "task_terminal",
    20: "global_concurrency_wait_start",
    21: "global_concurrency_wait_end",
}


def _ns_to_ms(a: int, b: int) -> int:
    if b <= a:
        return 0
    return int(round((b - a) / 1_000_000.0))


def build_model_from_events(events: list[tuple[int, int, int]]) -> dict[str, Any]:
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    ss_enter: tuple[int, int] | None = None
    explanations: list[str] = []

    for kid, mono_ns, aux in events:
        first.setdefault(kid, mono_ns)
        last[kid] = mono_ns
        if kid == 8:
            ss_enter = (mono_ns, aux)

    def pair(start: int, end: int) -> int:
        if start not in first or end not in first:
            return 0
        return _ns_to_ms(first[start], first[end])

    phases_ms: dict[str, int] = {
        "task_queue_ms": pair(1, 2),
        "admit_queue_ms": pair(2, 3),
        "account_queue_ms": pair(4, 5),
        "ready_buffer_wait_ms": pair(6, 7),
        "sse_stream_ms": pair(5, 11),
        "poll_resolve_ms": pair(11, 12),
        "download_ms": pair(13, 14),
        "global_concurrency_wait_ms": pair(20, 21),
        "ps_queue_ms": pair(16, 17),
    }
    if ss_enter and 9 in first:
        phases_ms["ss_queue_ms"] = _ns_to_ms(ss_enter[0], first[9])
        if phases_ms["ss_queue_ms"] > 0:
            active = (ss_enter[1] >> 16) & 0xFFFF
            queued = ss_enter[1] & 0xFFFF
            explanations.append(
                f"ss_queue {phases_ms['ss_queue_ms']}ms: pool active={active} queued={queued} at queue enter"
            )
    else:
        phases_ms["ss_queue_ms"] = 0

    if 1 in first and 15 in last:
        phases_ms["wall_clock_ms"] = _ns_to_ms(first[1], last[15])
    elif 3 in first and 15 in first:
        phases_ms["wall_clock_ms"] = _ns_to_ms(first[3], first[15])

    if phases_ms.get("account_queue_ms", 0) > 5000:
        explanations.append(
            f"account_queue {phases_ms['account_queue_ms']}ms: likely global/binding/account concurrency or preflight"
        )
    if phases_ms.get("task_queue_ms", 0) > 3000:
        explanations.append(
            f"task_queue {phases_ms['task_queue_ms']}ms: submit_workers or per_user_running limit"
        )
    if phases_ms.get("sse_stream_ms", 0) > 45000:
        explanations.append(
            f"sse_stream {phases_ms['sse_stream_ms']}ms: upstream requirements→image_gen dominates"
        )

    checkpoints = {KIND.get(k, str(k)): v for k, v in first.items()}
    return {
        "phases_ms": phases_ms,
        "explanations": explanations,
        "checkpoints": checkpoints,
    }
