"""Binding 四段日历 — 对外 facade，算法在 Rust `image_schedule_core`。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from services.image_pipeline.binding_calendar import (
    PRIME_SALT,
    REFRESH_SALT,
    account_key_for_account,
    compute_account_phase_slot,
    compute_next_account_slot,
    engine_info,
    evaluate_schedule_pick,
    local_date_for_account,
    resolve_tz_for_account,
)

__all__ = [
    "PRIME_SALT",
    "REFRESH_SALT",
    "PhaseSlot",
    "account_key_for_account",
    "compute_account_phase_slot",
    "compute_next_account_slot",
    "current_phase_index",
    "engine_info",
    "evaluate_schedule_pick",
    "list_due_phase_indices",
    "local_date_for_account",
    "resolve_tz_for_account",
]


@dataclass(frozen=True, slots=True)
class PhaseSlot:
    phase_index: int
    binding_slot_utc: datetime
    account_slot_utc: datetime


def _slot_dict_to_phase(slot: dict[str, Any]) -> PhaseSlot:
    return PhaseSlot(
        phase_index=int(slot["phase_index"]),
        binding_slot_utc=slot["binding_slot_utc"],
        account_slot_utc=slot["account_slot_utc"],
    )


def compute_account_phase_slot_typed(**kwargs: Any) -> PhaseSlot:
    return _slot_dict_to_phase(compute_account_phase_slot(**kwargs))


def current_phase_index(now_utc: datetime, tz_name: str) -> int:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo

    hour = now_utc.astimezone(ZoneInfo(tz_name)).hour
    from services.image_pipeline.binding_calendar import PHASE_HOUR_BOUNDS

    for index, (start, end) in enumerate(PHASE_HOUR_BOUNDS):
        if start <= hour < end:
            return index
    return len(PHASE_HOUR_BOUNDS) - 1


def list_due_phase_indices(
    *,
    now_utc: datetime,
    tz_name: str,
    phases_done: Sequence[int],
    local_day: date,
) -> list[int]:
    current = current_phase_index(now_utc, tz_name)
    done = {int(x) for x in phases_done}
    return [idx for idx in range(current + 1) if idx not in done]
