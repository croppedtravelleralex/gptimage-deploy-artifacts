"""Classify usage log events and aggregate binding-slot heatmaps."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.account_identity import binding_key_for_account
from services.log_service import LOG_TYPE_CALL, LOG_TYPE_LLM_OPS, log_service

_USAGE_KINDS = frozenset({"images_api", "images_chat", "dialogues_real", "dialogues_nurture"})
_BINDING_SLOTS_TTL_SEC = 60.0
_binding_slots_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_binding_slots_lock = threading.Lock()
SG_TZ = ZoneInfo("Asia/Singapore")
SLOTS_PER_DAY = 12
DAYS_PER_WEEK = 7


def _blank_matrix() -> list[list[int]]:
    return [[0 for _ in range(SLOTS_PER_DAY)] for _ in range(DAYS_PER_WEEK)]


def _blank_binding_payload() -> dict[str, list[list[int]]]:
    return {kind: _blank_matrix() for kind in _USAGE_KINDS}


def classify_call_summary(summary: str, *, ok: bool) -> str | None:
    if not ok:
        return None
    text = str(summary or "")
    if text.startswith("对话生图"):
        return "images_chat"
    if text.startswith("文生图") or text.startswith("图生图"):
        return "images_api"
    if text.startswith("文本生成"):
        return "dialogues_real"
    return None


def classify_llm_ops_detail(detail: dict[str, Any]) -> str | None:
    kind = str(detail.get("kind") or "").lower()
    outcome = str(detail.get("outcome") or "").lower()
    if outcome not in {"ok", "success", ""}:
        return None
    if kind == "nurture":
        return "dialogues_nurture"
    return None


def _event_ok(summary: str, detail: dict[str, Any]) -> bool:
    status = str(detail.get("status") or "").lower()
    return status in {"success", "ok", ""} and "失败" not in summary and "超时" not in summary


def _slot_index(iso_time: str) -> tuple[int, int] | None:
    raw = str(iso_time or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(SG_TZ)
    except (TypeError, ValueError):
        return None
    return local.weekday(), min(SLOTS_PER_DAY - 1, max(0, local.hour // 2))


def get_binding_usage_slots(*, days: int = 28, account_service: Any) -> dict[str, Any]:
    days = max(7, min(int(days or 28), 90))
    now = time.time()
    with _binding_slots_lock:
        cached = _binding_slots_cache.get(days)
        if cached and cached[0] > now:
            return dict(cached[1])

    today = datetime.now(SG_TZ).date()
    start = today.toordinal() - (days - 1)
    start_date = datetime.fromordinal(start).strftime("%Y-%m-%d")
    end_date = today.isoformat()

    email_to_binding: dict[str, str] = {}
    with account_service._lock:
        for account in account_service._accounts.values():
            email = str(account.get("email") or "").strip().lower()
            if not email:
                continue
            email_to_binding[email] = binding_key_for_account(account)

    hash_to_email: dict[str, str] = {}
    with account_service._lock:
        for account in account_service._accounts.values():
            token = str(account.get("access_token") or "")
            email = str(account.get("email") or "").strip().lower()
            if token and email:
                from services.log_service import _account_hash

                hash_to_email[_account_hash(token)] = email

    by_binding: dict[str, dict[str, list[list[int]]]] = {}

    def touch(binding_key: str, metric: str, when: str) -> None:
        slot = _slot_index(when)
        if slot is None:
            return
        day, hour_slot = slot
        payload = by_binding.setdefault(binding_key, _blank_binding_payload())
        payload[metric][day][hour_slot] += 1

    for item in log_service.list(type=LOG_TYPE_CALL, start_date=start_date, end_date=end_date, limit=50000):
        summary = str(item.get("summary") or "")
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        metric = classify_call_summary(summary, ok=_event_ok(summary, detail))
        if not metric:
            continue
        email = str(detail.get("account_email") or "").strip().lower()
        binding = email_to_binding.get(email)
        if not binding:
            continue
        touch(binding, metric, str(item.get("time") or ""))

    for item in log_service.list(type=LOG_TYPE_LLM_OPS, start_date=start_date, end_date=end_date, limit=20000):
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        metric = classify_llm_ops_detail(detail)
        if not metric:
            continue
        mail = str(detail.get("account_email") or detail.get("email") or "").strip().lower()
        if not mail:
            mail = hash_to_email.get(str(detail.get("account_hash") or "").strip(), "")
        binding = email_to_binding.get(mail)
        if not binding:
            continue
        touch(binding, metric, str(item.get("time") or ""))

    result = {
        "days": days,
        "timezone": "Asia/Singapore",
        "by_binding": by_binding,
    }
    with _binding_slots_lock:
        _binding_slots_cache[days] = (now + _BINDING_SLOTS_TTL_SEC, result)
    return result
