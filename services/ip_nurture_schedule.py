"""IP 出口养号时段表：7 天 × 12 个 2 小时槽（Asia/Singapore）。

预设模板供 binding_key（proxy_binding_hash / proxy_egress_ip）绑定；
权重 0.0–1.0，text_nurture 仅在当前槽权重 >= 0.15（SLOT_ALLOW_THRESHOLD）时允许出队。

A1-6：闸门原为**严格大于**，权重恰好等于阈值（如 `sg_remote` 的 0.15 底值）会被误挡；
且 `business_hours` / `extended_business` 只填了周一至周五，周末保留 0.05 / 0.08 底值 →
周末对全部 binding 全盲（`business_hours` 还是未配置 binding 的默认预设）。现在：
闸门改为 `>=`（"权重达到阈值即放行"），并给工作日型预设补一档**明显低于工作日办公时段**
的周末白天权重（减量而非停摆）。真要周末停摆的场景请用 `weekday_only` /
`rest_weekend` / `rest_day_sun`，它们保持原语义不变。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.config import config
from services.humanlike_scheduler import resolve_tz_name

SLOTS_PER_DAY = 12
DAYS_PER_WEEK = 7
DEFAULT_TZ = "Asia/Singapore"
SLOT_ALLOW_THRESHOLD = 0.15


def _clamp_weight(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _blank_matrix(default: float = 0.0) -> list[list[float]]:
    v = _clamp_weight(default)
    return [[v for _ in range(SLOTS_PER_DAY)] for _ in range(DAYS_PER_WEEK)]


def _fill_weekdays(
    matrix: list[list[float]],
    *,
    slots: dict[int, float] | None = None,
    value: float = 0.0,
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4),
) -> list[list[float]]:
    out = copy.deepcopy(matrix)
    slot_map = dict(slots or {})
    for day in weekdays:
        if day < 0 or day >= DAYS_PER_WEEK:
            continue
        for slot in range(SLOTS_PER_DAY):
            out[day][slot] = _clamp_weight(slot_map.get(slot, value))
    return out


def _with_slots(
    *,
    weekday_values: dict[int, dict[int, float]] | None = None,
    default: float = 0.0,
) -> list[list[float]]:
    matrix = _blank_matrix(default)
    for day, slot_map in (weekday_values or {}).items():
        if day < 0 or day >= DAYS_PER_WEEK:
            continue
        for slot, weight in slot_map.items():
            if 0 <= int(slot) < SLOTS_PER_DAY:
                matrix[day][int(slot)] = _clamp_weight(weight)
    return matrix


def _office_day(slots: tuple[int, ...], weight: float = 0.85) -> dict[int, float]:
    return {slot: weight for slot in slots}


# weekday index: 0=Mon … 6=Sun; slot index: hour // 2 (0=00-02 … 11=22-24)
_OFFICE = _office_day((4, 5, 6, 7, 8))  # 08:00–18:00
_EXTENDED = _office_day((3, 4, 5, 6, 7, 8, 9))  # 06:00–20:00
_MORNING = _office_day((4, 5, 6))  # 08:00–14:00
_AFTERNOON = _office_day((6, 7, 8, 9))  # 12:00–20:00
_EVENING = _office_day((8, 9, 10, 11))  # 16:00–24:00
_NIGHT = _office_day((0, 1, 2, 10, 11))  # 00-06 + 20-24
_LUNCH_DIP = {6: 0.25}  # 12:00–14:00

# A1-6：工作日型预设的周末白天档（10:00–20:00）。权重必须 >= SLOT_ALLOW_THRESHOLD
# 才能放行，但要明显低于工作日办公时段的 0.85；深夜仍留在阈值以下 → 周末夜间照旧关闭。
_WEEKEND_LIGHT_SLOTS = (5, 6, 7, 8, 9)  # 10:00–20:00
_WEEKEND_LIGHT_WEIGHT = 0.25
_WEEKEND_LIGHT_EXTENDED_WEIGHT = 0.30


def _with_weekend_light(matrix: list[list[float]], *, weight: float, floor: float) -> list[list[float]]:
    """给已填好工作日的矩阵补周末白天低权重档；周末其余槽压回 floor（低于阈值）。"""
    return _fill_weekdays(
        matrix,
        slots=_office_day(_WEEKEND_LIGHT_SLOTS, weight),
        value=floor,
        weekdays=(5, 6),
    )


def _preset(preset_id: str, label: str, matrix: list[list[float]]) -> dict[str, Any]:
    return {"id": preset_id, "label": label, "weights": matrix}


_PRESETS: tuple[dict[str, Any], ...] = (
    _preset("uniform", "全天均匀", _blank_matrix(0.75)),
    _preset(
        "business_hours",
        "工作日办公时段（周末减量）",
        _with_weekend_light(
            _fill_weekdays(_blank_matrix(0.05), slots=_OFFICE, weekdays=(0, 1, 2, 3, 4)),
            weight=_WEEKEND_LIGHT_WEIGHT,
            floor=0.05,
        ),
    ),
    _preset(
        "extended_business",
        "工作日加长班（周末减量）",
        _with_weekend_light(
            _fill_weekdays(_blank_matrix(0.08), slots=_EXTENDED, weekdays=(0, 1, 2, 3, 4)),
            weight=_WEEKEND_LIGHT_EXTENDED_WEIGHT,
            floor=0.08,
        ),
    ),
    _preset("weekday_only", "仅工作日", _fill_weekdays(_blank_matrix(0.05), slots=_OFFICE, weekdays=(0, 1, 2, 3, 4))),
    _preset("weekend_only", "仅周末", _fill_weekdays(_blank_matrix(0.05), slots=_EXTENDED, weekdays=(5, 6))),
    _preset("night_owl", "夜猫子", _fill_weekdays(_blank_matrix(0.1), slots=_EVENING)),
    _preset("early_bird", "早起型", _fill_weekdays(_blank_matrix(0.1), slots=_MORNING)),
    _preset("off_peak", "错峰（夜间偏高）", _fill_weekdays(_blank_matrix(0.2), slots=_NIGHT)),
    _preset("lunch_break", "午休降权", _with_slots(weekday_values={d: {**_OFFICE, **_LUNCH_DIP} for d in range(5)})),
    _preset("sg_office", "新加坡办公室", _with_slots(weekday_values={d: {**_OFFICE, **_LUNCH_DIP} for d in range(5)} | {5: {4: 0.35, 5: 0.45, 6: 0.35}})),
    # sg_remote 底值恰为 SLOT_ALLOW_THRESHOLD：闸门改 `>=` 后即按"弹性低频常开"生效。
    _preset("sg_remote", "新加坡远程弹性", _fill_weekdays(_blank_matrix(0.15), slots=_EXTENDED)),
    # A1-6：原为 0.12（全天低于阈值 → 任何时刻都跑不起来）。抬到刚过阈值，
    # 保持 minimal < conservative(0.22) < light_touch(0.28) 的低频梯度。
    _preset("minimal", "极低频", _blank_matrix(0.18)),
    _preset("aggressive", "高频全覆盖", _blank_matrix(0.95)),
    _preset("conservative", "保守低频", _blank_matrix(0.22)),
    _preset("monday_ramp", "周一缓启动", _with_slots(weekday_values={0: {4: 0.25, 5: 0.45, 6: 0.65, 7: 0.75, 8: 0.75}, **{d: _OFFICE for d in range(1, 5)}})),
    _preset("friday_winddown", "周五收工早", _with_slots(weekday_values={4: {4: 0.75, 5: 0.65, 6: 0.45, 7: 0.25, 8: 0.15}} | {d: _OFFICE for d in range(4)})),
    _preset("tue_thu_peak", "周二周四高峰", _with_slots(weekday_values={1: _OFFICE, 3: _OFFICE} | {d: {k: v * 0.55 for k, v in _OFFICE.items()} for d in (0, 2, 4)})),
    _preset("morning_focus", "上午专注", _fill_weekdays(_blank_matrix(0.08), slots=_MORNING)),
    _preset("afternoon_focus", "下午专注", _fill_weekdays(_blank_matrix(0.08), slots=_AFTERNOON)),
    _preset("evening_only", "仅傍晚", _fill_weekdays(_blank_matrix(0.06), slots=_EVENING)),
    _preset("split_shift", "早晚双班", _fill_weekdays(_blank_matrix(0.05), slots={**_MORNING, **_EVENING})),
    _preset("student", "学生作息（晚起）", _with_slots(weekday_values={d: {5: 0.55, 6: 0.7, 7: 0.75, 8: 0.65, 9: 0.45} for d in range(7)})),
    _preset("night_shift", "夜班", _fill_weekdays(_blank_matrix(0.05), slots={0: 0.8, 1: 0.85, 2: 0.8, 3: 0.55, 10: 0.45, 11: 0.4})),
    _preset("balanced_week", "全周均衡", _with_slots(weekday_values={d: {slot: 0.45 + (slot % 3) * 0.1 for slot in range(SLOTS_PER_DAY)} for d in range(7)})),
    _preset(
        "rest_day_sun",
        "周日休息",
        _with_slots(weekday_values={d: dict(_OFFICE) for d in range(6)} | {6: {slot: 0.05 for slot in range(SLOTS_PER_DAY)}}),
    ),
    _preset(
        "rest_weekend",
        "周末休息",
        _with_slots(
            weekday_values={d: dict(_OFFICE) for d in range(5)}
            | {5: {slot: 0.05 for slot in range(SLOTS_PER_DAY)}, 6: {slot: 0.05 for slot in range(SLOTS_PER_DAY)}}
        ),
    ),
    _preset("midweek_core", "周中核心", _with_slots(weekday_values={d: _OFFICE for d in (1, 2, 3)})),
    # 错峰 A/B 的错峰语义靠"关槽低于阈值"实现。闸门改 `>=` 后 0.15 会变成常开，
    # 把关槽压到 0.12 以保住分时互补的本意（仍是低值，不是删除档位）。
    _preset("staggered_a", "错峰 A（奇数槽）", _with_slots(weekday_values={d: {slot: 0.75 if slot % 2 else 0.12 for slot in range(SLOTS_PER_DAY)} for d in range(7)})),
    _preset("staggered_b", "错峰 B（偶数槽）", _with_slots(weekday_values={d: {slot: 0.75 if slot % 2 == 0 else 0.12 for slot in range(SLOTS_PER_DAY)} for d in range(7)})),
    _preset("pulse_2h", "两小时脉冲", _with_slots(weekday_values={d: {slot: 0.85 if slot in (5, 8) else 0.18 for slot in range(SLOTS_PER_DAY)} for d in range(5)})),
    _preset("light_touch", "轻触达", _blank_matrix(0.28)),
)

_PRESET_BY_ID: dict[str, dict[str, Any]] = {str(p["id"]): p for p in _PRESETS}


def _normalize_matrix(matrix: object, *, fallback: list[list[float]] | None = None) -> list[list[float]]:
    base = copy.deepcopy(fallback or _blank_matrix(0.0))
    if not isinstance(matrix, list):
        return base
    out: list[list[float]] = []
    for day in range(DAYS_PER_WEEK):
        row_in = matrix[day] if day < len(matrix) else None
        row: list[float] = []
        for slot in range(SLOTS_PER_DAY):
            if isinstance(row_in, list) and slot < len(row_in):
                row.append(_clamp_weight(row_in[slot]))
            else:
                row.append(base[day][slot] if day < len(base) else 0.0)
        out.append(row)
    return out


def list_presets() -> list[dict[str, Any]]:
    return [{"id": p["id"], "label": p["label"]} for p in _PRESETS]


def get_preset(preset_id: str) -> dict[str, Any] | None:
    key = str(preset_id or "").strip()
    if not key:
        return None
    found = _PRESET_BY_ID.get(key)
    if found is None:
        return None
    return {
        "id": found["id"],
        "label": found["label"],
        "weights": copy.deepcopy(found["weights"]),
    }


def current_slot_index(now_utc: datetime | None = None, tz_name: str = DEFAULT_TZ) -> tuple[int, int]:
    tz = ZoneInfo(resolve_tz_name(tz_name))
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    weekday = local.isoweekday() - 1  # Mon=0
    slot = local.hour // 2
    return weekday, slot


def current_slot_weight(matrix: list[list[float]], tz_name: str = DEFAULT_TZ, *, now_utc: datetime | None = None) -> float:
    normalized = _normalize_matrix(matrix)
    day, slot = current_slot_index(now_utc=now_utc, tz_name=tz_name)
    return _clamp_weight(normalized[day][slot])


def slot_allowed(
    matrix: list[list[float]],
    tz_name: str = DEFAULT_TZ,
    *,
    threshold: float = SLOT_ALLOW_THRESHOLD,
    now_utc: datetime | None = None,
) -> bool:
    # A1-6：`>=` 而非 `>` —— 权重恰好等于阈值表示"允许"（原严格大于会把 0.15 底值误判为关闭）。
    return current_slot_weight(matrix, tz_name=tz_name, now_utc=now_utc) >= float(threshold)


def _bindings_raw() -> dict[str, Any]:
    raw = config.data.get("ip_nurture_bindings") if isinstance(getattr(config, "data", None), dict) else {}
    return dict(raw) if isinstance(raw, dict) else {}


def _resolve_binding_entry(entry: object) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    preset_id = str(entry.get("preset_id") or "").strip()
    custom = entry.get("custom_matrix")
    preset = get_preset(preset_id) if preset_id else None
    if preset is None and not isinstance(custom, list):
        return None
    weights = _normalize_matrix(custom, fallback=preset["weights"] if preset else _blank_matrix(0.0))
    return {
        "preset_id": preset_id or None,
        "preset_label": (preset or {}).get("label"),
        "weights": weights,
        "updated_at": entry.get("updated_at"),
    }


def binding_schedule_from_config() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, entry in _bindings_raw().items():
        binding_key = str(key or "").strip()
        if not binding_key:
            continue
        resolved = _resolve_binding_entry(entry)
        if resolved is None:
            continue
        out[binding_key] = {"binding_key": binding_key, **resolved}
    return out


# A1-6：hash fallback 不得抽到 weekday_only / rest_weekend / pulse_2h 等周末全关预设。
_HASH_FALLBACK_PRESET_IDS: frozenset[str] = frozenset({
    "uniform",
    "business_hours",
    "extended_business",
    "balanced_week",
    "aggressive",
    "minimal",
    "sg_remote",
})


def resolve_binding_matrix(binding_key: str, *, default_preset_id: str = "business_hours") -> list[list[float]]:
    key = str(binding_key or "").strip()
    if key:
        resolved = binding_schedule_from_config().get(key)
        if resolved is not None:
            return copy.deepcopy(resolved["weights"])
        safe_presets = [
            preset for preset in _PRESETS
            if str(preset.get("id") or "") in _HASH_FALLBACK_PRESET_IDS
        ]
        presets = safe_presets or list(_PRESETS)
        if presets:
            tz = ZoneInfo(resolve_tz_name(DEFAULT_TZ))
            week = datetime.now(timezone.utc).astimezone(tz).isocalendar().week
            idx = (hash(key) + int(week)) % len(presets)
            return copy.deepcopy(presets[idx]["weights"])
    preset = get_preset(default_preset_id) or get_preset("uniform")
    return copy.deepcopy((preset or _PRESETS[0])["weights"])


def save_binding_schedule(
    binding_key: str,
    preset_id: str,
    custom_matrix: list[list[float]] | None = None,
) -> dict[str, Any]:
    key = str(binding_key or "").strip()
    if not key:
        raise ValueError("binding_key is required")
    pid = str(preset_id or "").strip()
    preset = get_preset(pid) if pid else None
    if custom_matrix is None and preset is None:
        raise ValueError(f"unknown preset_id: {preset_id}")
    weights = _normalize_matrix(custom_matrix, fallback=(preset or {}).get("weights"))
    entry = {
        "preset_id": pid or None,
        "custom_matrix": weights if custom_matrix is not None else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if entry["custom_matrix"] is None:
        entry.pop("custom_matrix")
    data = dict(getattr(config, "data", {}) or {})
    bindings = dict(data.get("ip_nurture_bindings") or {}) if isinstance(data.get("ip_nurture_bindings"), dict) else {}
    bindings[key] = entry
    data["ip_nurture_bindings"] = bindings
    config.update({"ip_nurture_bindings": bindings})
    return {
        "binding_key": key,
        "preset_id": pid or None,
        "preset_label": (preset or {}).get("label"),
        "weights": weights,
        "updated_at": entry["updated_at"],
    }
