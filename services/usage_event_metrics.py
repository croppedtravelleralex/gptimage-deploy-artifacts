"""Classify usage log events and aggregate binding-slot heatmaps."""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.account_identity import binding_key_for_account
from services.log_service import LOG_TYPE_CALL, LOG_TYPE_LLM_OPS, log_service

_USAGE_KINDS = frozenset({"images_api", "images_chat", "dialogues_real", "dialogues_nurture"})
_BINDING_SLOTS_TTL_SEC = 60.0
_binding_slots_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_binding_slots_lock = threading.Lock()
SLOTS_PER_DAY = 12
DAYS_PER_WEEK = 7

TIMEZONE_LABELS: dict[str, str] = {
    "Asia/Shanghai": "北京时间",
    "Asia/Singapore": "新加坡时间",
}
DEFAULT_TIMEZONE = "Asia/Shanghai"
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")


def _blank_matrix() -> list[list[int]]:
    return [[0 for _ in range(SLOTS_PER_DAY)] for _ in range(DAYS_PER_WEEK)]


def _blank_binding_payload() -> dict[str, list[list[int]]]:
    return {kind: _blank_matrix() for kind in _USAGE_KINDS}


def resolve_timezone(name: str | None) -> ZoneInfo:
    key = str(name or "").strip()
    if key in TIMEZONE_LABELS:
        return ZoneInfo(key)
    return ZoneInfo(DEFAULT_TIMEZONE)


def week_bounds(*, week_offset: int = 0, tz_name: str | None = None) -> tuple[date, date]:
    """Natural week Mon–Sun in the given timezone."""
    tz = resolve_timezone(tz_name)
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    start = monday + timedelta(weeks=int(week_offset or 0))
    end = start + timedelta(days=6)
    return start, end


def format_week_range(start: date, end: date) -> str:
    return f"{start.month}.{start.day}-{end.month}.{end.day}"


def day_labels_for_week(start: date) -> list[str]:
    labels: list[str] = []
    for offset in range(DAYS_PER_WEEK):
        current = start + timedelta(days=offset)
        labels.append(f"{current.month}.{current.day}")
    return labels


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
    if kind == "chat":
        return "dialogues_real"
    if kind == "nurture":
        return "dialogues_nurture"
    return None


def _event_ok(summary: str, detail: dict[str, Any]) -> bool:
    status = str(detail.get("status") or "").lower()
    return status in {"success", "ok", ""} and "失败" not in summary and "超时" not in summary


def _slot_index_for_week(
    iso_time: str,
    *,
    week_start: date,
    week_end: date,
    tz: ZoneInfo,
) -> tuple[int, int] | None:
    raw = str(iso_time or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(tz)
    except (TypeError, ValueError):
        return None
    local_date = local.date()
    if local_date < week_start or local_date > week_end:
        return None
    day = (local_date - week_start).days
    if day < 0 or day >= DAYS_PER_WEEK:
        return None
    hour_slot = min(SLOTS_PER_DAY - 1, max(0, local.hour // 2))
    return day, hour_slot


def get_binding_usage_slots(
    *,
    week_offset: int = 0,
    timezone: str | None = None,
    account_service: Any,
    days: int | None = None,
) -> dict[str, Any]:
    """Aggregate 7×12 heatmap for one natural week (Mon–Sun)."""
    tz_name = str(timezone or DEFAULT_TIMEZONE).strip()
    if tz_name not in TIMEZONE_LABELS:
        tz_name = DEFAULT_TIMEZONE
    tz = resolve_timezone(tz_name)
    offset = int(week_offset or 0)

    # Legacy `days` callers fall back to rolling window ending today.
    if days is not None and days > 0:
        return _get_binding_usage_slots_rolling(
            days=max(7, min(int(days), 90)),
            tz=tz,
            tz_name=tz_name,
            account_service=account_service,
        )

    cache_key = f"{offset}:{tz_name}"
    now = time.time()
    with _binding_slots_lock:
        cached = _binding_slots_cache.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    week_start, week_end = week_bounds(week_offset=offset, tz_name=tz_name)
    start_date = week_start.isoformat()
    end_date = week_end.isoformat()

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
        slot = _slot_index_for_week(
            when,
            week_start=week_start,
            week_end=week_end,
            tz=tz,
        )
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
        "week_offset": offset,
        "week_start": start_date,
        "week_end": end_date,
        "week_label": format_week_range(week_start, week_end),
        "weekday_labels": list(WEEKDAY_LABELS),
        "day_labels": day_labels_for_week(week_start),
        "timezone": tz_name,
        "timezone_label": TIMEZONE_LABELS.get(tz_name, tz_name),
        "by_binding": by_binding,
    }
    with _binding_slots_lock:
        _binding_slots_cache[cache_key] = (now + _BINDING_SLOTS_TTL_SEC, result)
    return result


def _get_binding_usage_slots_rolling(
    *,
    days: int,
    tz: ZoneInfo,
    tz_name: str,
    account_service: Any,
) -> dict[str, Any]:
    """Backward-compatible rolling window (maps events by weekday, not calendar week)."""
    now = time.time()
    cache_key = f"rolling:{days}:{tz_name}"
    with _binding_slots_lock:
        cached = _binding_slots_cache.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    today = datetime.now(tz).date()
    start = today.toordinal() - (days - 1)
    start_date = datetime.fromordinal(start).strftime("%Y-%m-%d")
    end_date = today.isoformat()
    week_start = datetime.fromordinal(start).date()
    week_end = today

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
        raw = str(when or "").strip()
        if not raw:
            return
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(tz)
        except (TypeError, ValueError):
            return
        day = local.weekday()
        hour_slot = min(SLOTS_PER_DAY - 1, max(0, local.hour // 2))
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
        "week_offset": 0,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "week_label": format_week_range(week_start, week_end),
        "weekday_labels": list(WEEKDAY_LABELS),
        "day_labels": day_labels_for_week(week_start) if days == 7 else [],
        "timezone": tz_name,
        "timezone_label": TIMEZONE_LABELS.get(tz_name, tz_name),
        "by_binding": by_binding,
    }
    with _binding_slots_lock:
        _binding_slots_cache[cache_key] = (now + _BINDING_SLOTS_TTL_SEC, result)
    return result
