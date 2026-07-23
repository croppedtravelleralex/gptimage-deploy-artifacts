"""拟人化调度纯函数：间隔抖动、日熔断软带、SG 日历探活。

无 IO、无全局 config；便于单测。默认常数对齐 docs/10 v2.2。
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_submit_gap_seconds(
    *,
    base_sec: float,
    jitter_lo: float = 0.65,
    jitter_hi: float = 1.45,
    poisson_lambda_sec: float = 8.0,
    rng: random.Random | None = None,
) -> float:
    """单号下次可调度间隔（秒）。"""
    base = max(0.0, float(base_sec))
    lo = max(0.05, float(jitter_lo))
    hi = max(lo, float(jitter_hi))
    r = rng or random.Random()
    jitter = r.uniform(lo, hi)
    lam = max(0.0, float(poisson_lambda_sec))
    extra = r.expovariate(1.0 / lam) if lam > 0 else 0.0
    return max(0.0, base * jitter + extra)


def draw_soft_band(
    soft: float,
    *,
    lo_delta: float = 0.05,
    hi_delta: float = 0.03,
    rng: random.Random | None = None,
) -> float:
    soft_v = _clamp(float(soft), 0.05, 0.99)
    r = rng or random.Random()
    return _clamp(r.uniform(soft_v - lo_delta, soft_v + hi_delta), 0.05, 0.99)


@dataclass(frozen=True, slots=True)
class QuotaPeakState:
    peak: int
    reset_at: str | None
    soft_band: float
    used_ratio: float
    soft_capped: bool


def update_quota_peak_state(
    *,
    remaining: int,
    reset_after: str | None,
    prev_peak: int,
    prev_reset_at: str | None,
    prev_soft_band: float | None,
    soft: float = 0.70,
    soft_band_override: float | None = None,
    rng: random.Random | None = None,
) -> QuotaPeakState:
    """用 remaining/reset_after 估窗口 peak，并判定 soft band 熔断。"""
    rem = max(0, int(remaining))
    reset = (reset_after or "").strip() or None
    peak = max(0, int(prev_peak or 0))
    prev_reset = (prev_reset_at or "").strip() or None
    new_window = False
    if reset and reset != prev_reset:
        new_window = True
    if rem > peak:
        new_window = True
        peak = rem
    if new_window and reset != prev_reset:
        peak = rem
    if peak <= 0:
        peak = rem
    if soft_band_override is not None:
        band = _clamp(float(soft_band_override), 0.05, 0.99)
    elif new_window:
        band = draw_soft_band(soft, rng=rng)
    else:
        band = float(prev_soft_band) if prev_soft_band else draw_soft_band(soft, rng=rng)
    used_ratio = 0.0 if peak <= 0 else 1.0 - (rem / float(peak))
    soft_capped = peak > 0 and used_ratio >= band
    return QuotaPeakState(
        peak=peak,
        reset_at=reset,
        soft_band=band,
        used_ratio=used_ratio,
        soft_capped=soft_capped,
    )


def _parse_hhmm(value: str, default: time) -> time:
    text = str(value or "").strip()
    if not text:
        return default
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour % 24, minute % 60)
    except (TypeError, ValueError):
        return default


def _stable_u(seed_parts: Sequence[str]) -> float:
    digest = hashlib.sha256("|".join(seed_parts).encode("utf-8")).hexdigest()
    return (int(digest[:12], 16) % 1_000_000_000) / 1_000_000_000.0


def resolve_tz_name(tz_name: str | None = None) -> str:
    name = str(tz_name or "Asia/Singapore").strip() or "Asia/Singapore"
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return "Asia/Singapore"


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    due: bool
    reason: str
    slot_at: datetime | None = None
    local_date: str | None = None


def decide_proactive_refresh(
    *,
    now_utc: datetime,
    account_key: str,
    done_date: str | None,
    tz_name: str = "Asia/Singapore",
    workdays: Sequence[int] = (1, 2, 3, 4, 5),
    p_work: float = 1.0,
    p_rest: float = 0.35,
    window_work: tuple[str, str] = ("09:00", "17:00"),
    window_rest: tuple[str, str] = ("10:00", "16:00"),
    slot_jitter_minutes: int = 10,
    salt: str = "proactive-v1",
) -> ProactiveDecision:
    """是否应对该账号做一次主动 /me。

    workdays: Monday=1 … Sunday=7（与 datetime.isoweekday 一致）。
    """
    tz = ZoneInfo(resolve_tz_name(tz_name))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(tz)
    local_date = local.date().isoformat()
    if done_date == local_date:
        return ProactiveDecision(False, "already_done_today", local_date=local_date)

    is_work = local.isoweekday() in {int(x) for x in workdays}
    p_day = _clamp(float(p_work if is_work else p_rest), 0.0, 1.0)
    u_trigger = _stable_u([account_key, local_date, salt, "trigger"])
    if u_trigger >= p_day:
        return ProactiveDecision(False, "bernoulli_skip", local_date=local_date)

    if is_work:
        start_s, end_s = window_work
        default_start, default_end = time(9, 0), time(17, 0)
    else:
        start_s, end_s = window_rest
        default_start, default_end = time(10, 0), time(16, 0)
    start_t = _parse_hhmm(start_s, default_start)
    end_t = _parse_hhmm(end_s, default_end)
    w0 = datetime.combine(local.date(), start_t, tzinfo=tz)
    w1 = datetime.combine(local.date(), end_t, tzinfo=tz)
    if w1 <= w0:
        w1 = w0 + timedelta(hours=8)
    span = (w1 - w0).total_seconds()
    u_slot = _stable_u([account_key, local_date, salt, "slot"])
    slot = w0 + timedelta(seconds=span * u_slot)
    jitter_sec = (_stable_u([account_key, local_date, salt, "jitter"]) * 2 - 1) * max(0, int(slot_jitter_minutes)) * 60
    slot = slot + timedelta(seconds=jitter_sec)
    if slot < w0:
        slot = w0
    if slot > w1:
        slot = w1
    if local < slot:
        return ProactiveDecision(False, "before_slot", slot_at=slot, local_date=local_date)
    return ProactiveDecision(True, "due", slot_at=slot, local_date=local_date)


def night_or_lunch_soft_weight(
    now_utc: datetime,
    tz_name: str = "Asia/Singapore",
    *,
    night_weight: float = 0.4,
    lunch_weight: float = 0.85,
) -> float:
    """选号软权重：不丢任务，仅降低优先级。返回值越小越靠后。"""
    tz = ZoneInfo(resolve_tz_name(tz_name))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    hour = now_utc.astimezone(tz).hour
    if 0 <= hour < 6:
        return _clamp(float(night_weight), 0.05, 1.0)
    if 12 <= hour < 13:
        return _clamp(float(lunch_weight), 0.05, 1.0)
    return 1.0


_REGION_TZ = {
    "sg": "Asia/Singapore",
    "sin": "Asia/Singapore",
    "singapore": "Asia/Singapore",
    "hk": "Asia/Hong_Kong",
    "hkg": "Asia/Hong_Kong",
    "jp": "Asia/Tokyo",
    "tyo": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "us": "America/Los_Angeles",
    "usa": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "ny": "America/New_York",
    "gb": "Europe/London",
    "uk": "Europe/London",
    "de": "Europe/Berlin",
}


def map_egress_region_to_tz(region: object, default: str = "Asia/Singapore") -> str:
    text = str(region or "").strip().lower()
    if not text:
        return resolve_tz_name(default)
    if text in _REGION_TZ:
        return resolve_tz_name(_REGION_TZ[text])
    for key, tz in _REGION_TZ.items():
        if key in text:
            return resolve_tz_name(tz)
    return resolve_tz_name(default)


def resolve_account_tz_name(
    account: dict | None,
    *,
    timezone_from_egress: bool = True,
    default_tz: str = "Asia/Singapore",
) -> str:
    if not timezone_from_egress or not isinstance(account, dict):
        return resolve_tz_name(default_tz)
    for field in (
        "proxy_region",
        "proxy_country",
        "node_region",
        "proxy_egress_region",
        "proxy_colo",
        "proxy_provider",
    ):
        raw = str(account.get(field) or "").strip()
        if not raw:
            continue
        return map_egress_region_to_tz(raw, default_tz)
    return resolve_tz_name(default_tz)


def is_new_image_account(account: dict | None, *, max_age_days: float = 7.0) -> bool:
    if not isinstance(account, dict):
        return False
    stage = str(account.get("maturity_stage") or "").strip().lower()
    if stage in {"", "observe", "t0", "t1h", "t6h", "t24h", "t72h", "new", "incoming"}:
        if stage:
            return True
    for field in ("created_at", "imported_at", "first_seen_at", "registered_at"):
        raw = account.get(field)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                created = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            else:
                text = str(raw).strip().replace("Z", "+00:00")
                created = datetime.fromisoformat(text)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 86400.0
            return age_days < float(max_age_days)
        except Exception:
            continue
    return False


def effective_daily_soft(
    base_soft: float,
    account: dict | None,
    *,
    new_account_cap: float = 0.40,
) -> float:
    soft = _clamp(float(base_soft), 0.05, 0.99)
    if is_new_image_account(account):
        return min(soft, _clamp(float(new_account_cap), 0.05, 0.99))
    return soft


def compute_resume_delay_seconds(
    attempts: int,
    *,
    first_delay_sec: float = 5.0,
    backoff_base_sec: float = 5.0,
    backoff_cap_sec: float = 60.0,
    jitter_lo: float = 0.85,
    jitter_hi: float = 1.25,
    rng: random.Random | None = None,
) -> float:
    """Phase C：首次 ≥5s+jitter，之后指数退避至 cap。attempts 为已完成次数（从 1 起）。"""
    n = max(1, int(attempts or 1))
    r = rng or random.Random()
    if n <= 1:
        base = max(5.0, float(first_delay_sec))
    else:
        base = min(float(backoff_cap_sec), float(backoff_base_sec) * (2 ** (n - 1)))
    return max(1.0, base * r.uniform(float(jitter_lo), float(jitter_hi)))


def compute_submit_interval_ms(
    base_ms: int,
    *,
    jitter_lo: float = 0.70,
    jitter_hi: float = 1.30,
    rng: random.Random | None = None,
) -> int:
    r = rng or random.Random()
    lo = max(0.05, float(jitter_lo))
    hi = max(lo, float(jitter_hi))
    return max(0, int(round(max(0, int(base_ms)) * r.uniform(lo, hi))))


def fail_cooldown_seconds(
    *,
    min_sec: float = 1800.0,
    max_sec: float = 5400.0,
    rng: random.Random | None = None,
) -> float:
    r = rng or random.Random()
    lo = max(60.0, float(min_sec))
    hi = max(lo, float(max_sec))
    return r.uniform(lo, hi)
