"""Rust binding_calendar FFI（单一真相源）+ Python fallback。"""
from __future__ import annotations

import ctypes
import json
import logging
import platform
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from services.humanlike_scheduler import _stable_u, resolve_account_tz_name

logger = logging.getLogger(__name__)

REFRESH_SALT = "quota-refresh-v1"
PRIME_SALT = "quota-prime-v1"
PHASE_HOUR_BOUNDS: tuple[tuple[int, int], ...] = ((0, 6), (6, 12), (12, 18), (18, 24))


class _RustBindingCalendar:
    def __init__(self) -> None:
        self._lib = None
        self._prime_available = False
        self._load()

    def available(self) -> bool:
        return self._lib is not None

    def _lib_path(self) -> Path | None:
        root = Path(__file__).resolve().parents[2]
        name = "image_schedule_core.dll" if platform.system() == "Windows" else "libimage_schedule_core.so"
        for candidate in (
            root / "native" / name,
            root / "crates" / "image_schedule_core" / "target" / "release" / name,
            root / "crates" / "image_schedule_core" / "target" / "debug" / name,
        ):
            if candidate.is_file():
                return candidate
        return None

    def _load(self) -> None:
        path = self._lib_path()
        if path is None:
            return
        try:
            lib = ctypes.CDLL(str(path))
            lib.isc_binding_calendar_account_slot.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_int64),
                ctypes.POINTER(ctypes.c_int64),
            ]
            lib.isc_binding_calendar_account_slot.restype = ctypes.c_uint8
            lib.isc_binding_calendar_next_slot_unix.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_int64,
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_char_p,
            ]
            lib.isc_binding_calendar_next_slot_unix.restype = ctypes.c_int64
            lib.isc_quota_schedule_evaluate.argtypes = [ctypes.c_char_p]
            lib.isc_quota_schedule_evaluate.restype = ctypes.c_void_p
            lib.isc_free_string.argtypes = [ctypes.c_void_p]
            self._lib = lib
            self._prime_available = False
            for name in ("isc_quota_prime_evaluate", "isc_quota_prime_list_eligible"):
                if not hasattr(lib, name):
                    logger.warning("binding_calendar rust missing symbol: %s", name)
                    break
            else:
                lib.isc_quota_prime_evaluate.argtypes = [ctypes.c_char_p]
                lib.isc_quota_prime_evaluate.restype = ctypes.c_void_p
                lib.isc_quota_prime_list_eligible.argtypes = [ctypes.c_char_p]
                lib.isc_quota_prime_list_eligible.restype = ctypes.c_void_p
                self._prime_available = True
            logger.info("binding_calendar rust engine loaded: %s", path)
        except (OSError, AttributeError) as exc:
            logger.warning("binding_calendar rust load failed: %s", exc)
            self._lib = None
            self._prime_available = False

    def _free_string(self, ptr: ctypes.c_void_p) -> None:
        if self._lib is not None and ptr:
            self._lib.isc_free_string(ptr)

    def account_phase_slot_unix(
        self,
        *,
        account_key: str,
        binding_key: str,
        local_day: date,
        phase_index: int,
        tz_name: str,
        jitter_min_minutes: int,
        jitter_max_minutes: int,
        salt: str,
    ) -> tuple[int, int] | None:
        if self._lib is None:
            return None
        binding_out = ctypes.c_int64()
        account_out = ctypes.c_int64()
        ok = self._lib.isc_binding_calendar_account_slot(
            account_key.encode("utf-8"),
            binding_key.encode("utf-8"),
            local_day.isoformat().encode("utf-8"),
            int(phase_index),
            tz_name.encode("utf-8"),
            int(jitter_min_minutes),
            int(jitter_max_minutes),
            salt.encode("utf-8"),
            ctypes.byref(binding_out),
            ctypes.byref(account_out),
        )
        if not ok:
            return None
        return int(binding_out.value), int(account_out.value)

    def next_slot_unix(
        self,
        *,
        account_key: str,
        binding_key: str,
        now_unix: int,
        tz_name: str,
        jitter_min_minutes: int,
        jitter_max_minutes: int,
        salt: str,
    ) -> int | None:
        if self._lib is None:
            return None
        value = int(
            self._lib.isc_binding_calendar_next_slot_unix(
                account_key.encode("utf-8"),
                binding_key.encode("utf-8"),
                int(now_unix),
                tz_name.encode("utf-8"),
                int(jitter_min_minutes),
                int(jitter_max_minutes),
                salt.encode("utf-8"),
            )
        )
        return value if value >= 0 else None

    def evaluate_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._lib is None:
            return _py_evaluate_schedule(payload)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ptr = self._lib.isc_quota_schedule_evaluate(raw)
        if not ptr:
            return _py_evaluate_schedule(payload)
        try:
            text = ctypes.cast(ptr, ctypes.c_char_p).value
            body = json.loads(text.decode("utf-8") if text else "{}")
            return body if isinstance(body, dict) else {}
        finally:
            self._free_string(ptr)

    def evaluate_prime(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._lib is None or not self._prime_available:
            return _py_evaluate_prime(payload)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ptr = self._lib.isc_quota_prime_evaluate(raw)
        if not ptr:
            return _py_evaluate_prime(payload)
        try:
            text = ctypes.cast(ptr, ctypes.c_char_p).value
            body = json.loads(text.decode("utf-8") if text else "{}")
            return body if isinstance(body, dict) else {}
        finally:
            self._free_string(ptr)

    def list_prime_eligible(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._lib is None or not self._prime_available:
            return _py_list_prime_eligible(payload)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ptr = self._lib.isc_quota_prime_list_eligible(raw)
        if not ptr:
            return _py_list_prime_eligible(payload)
        try:
            text = ctypes.cast(ptr, ctypes.c_char_p).value
            body = json.loads(text.decode("utf-8") if text else "{}")
            return body if isinstance(body, dict) else {}
        finally:
            self._free_string(ptr)


_RUST = _RustBindingCalendar()


def engine_info() -> dict[str, str]:
    path = _RUST._lib_path()
    engine = "rust" if _RUST.available() else "python"
    if engine == "rust" and not _RUST._prime_available:
        engine = "rust-calendar-only"
    return {
        "engine": engine,
        "lib_path": str(path) if path else "",
        "prime_engine": "rust" if _RUST._prime_available else "python",
    }


def resolve_tz_for_account(account: dict[str, Any] | None, *, default_tz: str, timezone_from_egress: bool) -> str:
    return resolve_account_tz_name(
        account,
        timezone_from_egress=timezone_from_egress,
        default_tz=default_tz,
    )


def local_date_for_account(
    now_utc: datetime,
    account: dict[str, Any] | None,
    *,
    default_tz: str,
    timezone_from_egress: bool,
) -> date:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(resolve_tz_for_account(account, default_tz=default_tz, timezone_from_egress=timezone_from_egress))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(tz).date()


def account_key_for_account(account: dict[str, Any]) -> str:
    for field in ("token_hash", "email", "user_id", "access_token"):
        value = str(account.get(field) or "").strip()
        if value:
            return value[:64]
    return "unknown"


def compute_account_phase_slot(
    *,
    account_key: str,
    binding_key: str,
    local_day: date,
    phase_index: int,
    tz_name: str,
    jitter_min_minutes: int = 30,
    jitter_max_minutes: int = 60,
    salt: str = REFRESH_SALT,
) -> dict[str, Any]:
    rust = _RUST.account_phase_slot_unix(
        account_key=account_key,
        binding_key=binding_key,
        local_day=local_day,
        phase_index=phase_index,
        tz_name=tz_name,
        jitter_min_minutes=jitter_min_minutes,
        jitter_max_minutes=jitter_max_minutes,
        salt=salt,
    )
    if rust is not None:
        binding_unix, account_unix = rust
        return {
            "phase_index": phase_index,
            "binding_slot_utc": datetime.fromtimestamp(binding_unix, tz=timezone.utc),
            "account_slot_utc": datetime.fromtimestamp(account_unix, tz=timezone.utc),
        }
    return _py_compute_account_phase_slot(
        account_key=account_key,
        binding_key=binding_key,
        local_day=local_day,
        phase_index=phase_index,
        tz_name=tz_name,
        jitter_min_minutes=jitter_min_minutes,
        jitter_max_minutes=jitter_max_minutes,
        salt=salt,
    )


def compute_next_account_slot(
    account: dict[str, Any],
    *,
    now_utc: datetime,
    settings: dict[str, object],
    salt: str = REFRESH_SALT,
) -> datetime | None:
    default_tz = str(settings.get("default_timezone") or "Asia/Singapore")
    tz_name = resolve_tz_for_account(
        account,
        default_tz=default_tz,
        timezone_from_egress=bool(settings.get("timezone_from_egress")),
    )
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    binding_key = str(account.get("_binding_key") or account.get("proxy_binding_hash") or "default")
    account_key = account_key_for_account(account)
    jitter_min = int(settings.get("account_jitter_min_minutes") or 30)
    jitter_max = int(settings.get("account_jitter_max_minutes") or 60)
    next_unix = _RUST.next_slot_unix(
        account_key=account_key,
        binding_key=binding_key,
        now_unix=int(now_utc.timestamp()),
        tz_name=tz_name,
        jitter_min_minutes=jitter_min,
        jitter_max_minutes=jitter_max,
        salt=salt,
    )
    if next_unix is not None:
        return datetime.fromtimestamp(next_unix, tz=timezone.utc)
    return _py_compute_next_account_slot(account, now_utc=now_utc, settings=settings, salt=salt)


def evaluate_schedule_pick(payload: dict[str, Any]) -> dict[str, Any]:
    return _RUST.evaluate_schedule(payload)


def evaluate_prime_eligibility(payload: dict[str, Any]) -> dict[str, Any]:
    return _RUST.evaluate_prime(payload)


def list_prime_eligible(payload: dict[str, Any]) -> dict[str, Any]:
    return _RUST.list_prime_eligible(payload)


def account_timestamp_unix(account: dict[str, Any], field: str) -> int | None:
    raw = account.get(field)
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return int(raw)
        from datetime import datetime, timezone

        text = str(raw).strip().replace("Z", "+00:00")
        if not text:
            return None
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def prime_account_input(
    account: dict[str, Any],
    *,
    index: int = 0,
    image_schedulable: bool | None = None,
) -> dict[str, Any]:
    schedulable = image_schedulable
    if schedulable is None:
        from services.account_service import account_service

        schedulable = bool(account_service._is_image_account_schedulable(account))
    primed_at = account.get("quota_window_primed_at")
    return {
        "index": index,
        "quota": int(account.get("quota") or 0),
        "success": int(account.get("success") or 0),
        "prime_state": str(account.get("quota_window_prime_state") or "none"),
        "primed_at": str(primed_at or ""),
        "attempts": int(account.get("quota_window_prime_attempts") or 0),
        "image_quota_unknown": bool(account.get("image_quota_unknown")),
        "account_type": str(account.get("type") or ""),
        "image_schedulable": schedulable,
        "panda_sync_state": str(account.get("panda_sync_state") or ""),
        "panda_receive_state": str(account.get("panda_receive_state") or ""),
        "maturity_stage": str(account.get("maturity_stage") or ""),
        "created_at_unix": account_timestamp_unix(account, "created_at"),
        "imported_at_unix": account_timestamp_unix(account, "imported_at"),
        "first_seen_at_unix": account_timestamp_unix(account, "first_seen_at"),
        "registered_at_unix": account_timestamp_unix(account, "registered_at"),
    }


def prime_settings_input(settings: dict[str, object]) -> dict[str, Any]:
    skip = settings.get("skip_panda_sync_states") or ["staging", "ready"]
    return {
        "enabled": bool(settings.get("enabled")),
        "full_quota": int(settings.get("full_quota") or 25),
        "min_account_age_days": float(settings.get("min_account_age_days") or 7),
        "skip_panda_sync_states": list(skip) if isinstance(skip, list) else ["staging", "ready"],
    }


# ---- Python fallback（Rust 未编译时） ----


def _py_compute_account_phase_slot(**kwargs: Any) -> dict[str, Any]:
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo

    account_key = str(kwargs["account_key"])
    binding_key = str(kwargs["binding_key"])
    local_day: date = kwargs["local_day"]
    phase_index = int(kwargs["phase_index"])
    tz_name = str(kwargs["tz_name"])
    jitter_min_minutes = int(kwargs["jitter_min_minutes"])
    jitter_max_minutes = int(kwargs["jitter_max_minutes"])
    salt = str(kwargs["salt"])
    tz = ZoneInfo(tz_name)
    start_hour, end_hour = PHASE_HOUR_BOUNDS[max(0, min(phase_index, len(PHASE_HOUR_BOUNDS) - 1))]
    phase_start = datetime.combine(local_day, time(start_hour, 0), tzinfo=tz)
    phase_end = datetime.combine(local_day, time(end_hour % 24, 0), tzinfo=tz)
    if phase_end <= phase_start:
        phase_end = phase_start + timedelta(hours=6)
    span_sec = max(1.0, (phase_end - phase_start).total_seconds())
    u = _stable_u([binding_key, local_day.isoformat(), str(phase_index), salt, "binding"])
    binding_slot = (phase_start + timedelta(seconds=span_sec * u)).astimezone(timezone.utc)
    lo = max(0, jitter_min_minutes)
    hi = max(lo, jitter_max_minutes)
    jitter = lo + int(_stable_u([account_key, local_day.isoformat(), str(phase_index), salt, "jitter"]) * (hi - lo))
    account_slot = binding_slot + timedelta(minutes=jitter)
    return {
        "phase_index": phase_index,
        "binding_slot_utc": binding_slot,
        "account_slot_utc": account_slot,
    }


def _py_compute_next_account_slot(
    account: dict[str, Any],
    *,
    now_utc: datetime,
    settings: dict[str, object],
    salt: str,
) -> datetime | None:
    from zoneinfo import ZoneInfo

    default_tz = str(settings.get("default_timezone") or "Asia/Singapore")
    tz_name = resolve_tz_for_account(
        account,
        default_tz=default_tz,
        timezone_from_egress=bool(settings.get("timezone_from_egress")),
    )
    tz = ZoneInfo(tz_name)
    local_day = now_utc.astimezone(tz).date()
    hour = now_utc.astimezone(tz).hour
    current = next((i for i, (s, e) in enumerate(PHASE_HOUR_BOUNDS) if s <= hour < e), 3)
    binding_key = str(account.get("_binding_key") or account.get("proxy_binding_hash") or "default")
    account_key = account_key_for_account(account)
    jitter_min = int(settings.get("account_jitter_min_minutes") or 30)
    jitter_max = int(settings.get("account_jitter_max_minutes") or 60)
    for phase in range(current, len(PHASE_HOUR_BOUNDS)):
        slot = _py_compute_account_phase_slot(
            account_key=account_key,
            binding_key=binding_key,
            local_day=local_day,
            phase_index=phase,
            tz_name=tz_name,
            jitter_min_minutes=jitter_min,
            jitter_max_minutes=jitter_max,
            salt=salt,
        )
        if slot["account_slot_utc"] > now_utc:
            return slot["account_slot_utc"]
    from datetime import timedelta

    next_day = local_day + timedelta(days=1)
    slot = _py_compute_account_phase_slot(
        account_key=account_key,
        binding_key=binding_key,
        local_day=next_day,
        phase_index=0,
        tz_name=tz_name,
        jitter_min_minutes=jitter_min,
        jitter_max_minutes=jitter_max,
        salt=salt,
    )
    return slot["account_slot_utc"]


def _py_evaluate_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    now_unix = int(payload.get("now_unix") or 0)
    gap = float(payload.get("binding_gap_sec") or 7200.0)
    binding_last = payload.get("binding_last_refresh_unix") or {}
    jitter_min = int(payload.get("jitter_min_minutes") or 30)
    jitter_max = int(payload.get("jitter_max_minutes") or 60)
    accounts = payload.get("accounts") or []
    best: tuple[int, dict[str, Any]] | None = None
    for index, row in enumerate(accounts):
        if not isinstance(row, dict) or not row.get("schedulable"):
            continue
        account_key = str(row.get("account_key") or "")
        binding_key = str(row.get("binding_key") or "default")
        tz_name = str(row.get("tz_name") or "Asia/Singapore")
        local_date = str(row.get("local_date") or "")
        try:
            local_day = date.fromisoformat(local_date)
        except ValueError:
            continue
        phases_done = [int(x) for x in (row.get("phases_done") or [])]
        hour = datetime.fromtimestamp(now_unix, tz=timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(tz_name)).hour
        current = next((i for i, (s, e) in enumerate(PHASE_HOUR_BOUNDS) if s <= hour < e), 3)
        done = set(phases_done)
        due = [p for p in range(current + 1) if p not in done]
        if not due:
            continue
        last = float(binding_last.get(binding_key) or 0.0)
        if now_unix - last < gap:
            continue
        for phase in due:
            slot = _py_compute_account_phase_slot(
                account_key=account_key,
                binding_key=binding_key,
                local_day=local_day,
                phase_index=phase,
                tz_name=tz_name,
                jitter_min_minutes=jitter_min,
                jitter_max_minutes=jitter_max,
                salt=REFRESH_SALT,
            )
            account_unix = int(slot["account_slot_utc"].timestamp())
            if account_unix <= now_unix:
                pick = {
                    "index": int(row.get("index", index)),
                    "phase_index": phase,
                    "account_slot_unix": account_unix,
                    "binding_key": binding_key,
                }
                if best is None or account_unix < best[0]:
                    best = (account_unix, pick)
                break
    return {"picked": best[1] if best else None}


def _py_evaluate_prime(payload: dict[str, Any]) -> dict[str, Any]:
    from services.humanlike_scheduler import is_new_image_account

    mode = str(payload.get("mode") or "auto")
    now_unix = int(payload.get("now_unix") or 0)
    settings = payload.get("settings") or {}
    account_row = payload.get("account") or {}
    if not isinstance(settings, dict) or not isinstance(account_row, dict):
        return {"eligible": False, "reason": "invalid_input"}

    if not bool(settings.get("enabled")):
        return {"eligible": False, "reason": "disabled"}

    state = str(account_row.get("prime_state") or "none").strip().lower()
    if mode == "force":
        if state in {"pending", "running"}:
            return {"eligible": False, "reason": f"state:{state}"}
        return {"eligible": True, "reason": "force"}

    if state in {"pending", "running", "done"}:
        return {"eligible": False, "reason": f"state:{state}"}

    account_type = str(account_row.get("account_type") or "")
    from services.account_service import AccountService

    if AccountService._is_true_unlimited_image_account({"type": account_type}):
        return {"eligible": False, "reason": "unlimited"}
    if bool(account_row.get("image_quota_unknown")):
        return {"eligible": False, "reason": "unknown_quota"}
    if not bool(account_row.get("image_schedulable")):
        return {"eligible": False, "reason": "not_schedulable"}
    full_quota = int(settings.get("full_quota") or 25)
    if int(account_row.get("quota") or 0) != full_quota:
        return {"eligible": False, "reason": "quota_not_full"}
    if int(account_row.get("success") or 0) > 0:
        return {"eligible": False, "reason": "already_imaged"}
    if str(account_row.get("primed_at") or "").strip():
        return {"eligible": False, "reason": "already_primed"}
    skip_states = {str(x).strip().lower() for x in (settings.get("skip_panda_sync_states") or [])}
    sync_state = str(account_row.get("panda_sync_state") or "").strip().lower()
    if sync_state in skip_states and sync_state != "synced":
        return {"eligible": False, "reason": "panda_sync"}
    if str(account_row.get("panda_receive_state") or "").strip().lower() == "incoming":
        return {"eligible": False, "reason": "incoming"}
    account_dict = {
        "maturity_stage": account_row.get("maturity_stage"),
        "created_at": account_row.get("created_at_unix"),
        "imported_at": account_row.get("imported_at_unix"),
        "first_seen_at": account_row.get("first_seen_at_unix"),
        "registered_at": account_row.get("registered_at_unix"),
    }
    min_age = float(settings.get("min_account_age_days") or 7)
    if is_new_image_account(account_dict, max_age_days=min_age):
        return {"eligible": False, "reason": "new_account"}
    return {"eligible": True, "reason": "eligible"}


def _py_list_prime_eligible(payload: dict[str, Any]) -> dict[str, Any]:
    settings = payload.get("settings") or {}
    accounts = payload.get("accounts") or []
    now_unix = int(payload.get("now_unix") or 0)
    max_attempts = int(payload.get("max_attempts") or 3)
    indices: list[int] = []
    if not isinstance(accounts, list):
        return {"indices": indices}
    for row in accounts:
        if not isinstance(row, dict):
            continue
        if int(row.get("attempts") or 0) >= max_attempts:
            continue
        result = _py_evaluate_prime(
            {
                "mode": "auto",
                "now_unix": now_unix,
                "settings": settings,
                "account": row,
            }
        )
        if result.get("eligible"):
            indices.append(int(row.get("index") or 0))
    return {"indices": indices}

