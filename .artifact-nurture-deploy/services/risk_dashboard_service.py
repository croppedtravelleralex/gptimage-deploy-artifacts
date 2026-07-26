#!/usr/bin/env python3
"""Build humanlike / pipeline risk dashboard snapshots and calendar."""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# datetime imported once at module level

from services.account_service import account_service
from services.config import config
from services.image_task_service import image_task_service
from services.log_service import LOG_TYPE_LLM_OPS, log_service
from services.risk_metrics_store import append_metrics_point, list_metrics, list_reports
from services.text_nurture_service import text_nurture_service


def _mask_email(email: str) -> str:
    raw = str(email or "").strip()
    if "@" not in raw:
        return raw[:3] + "***" if len(raw) > 3 else raw
    local, _, domain = raw.partition("@")
    if len(local) <= 2:
        return f"{local}***@{domain}"
    return f"{local[:2]}***@{domain}"


def _admission_snapshot() -> dict[str, Any]:
    try:
        from api import ai as ai_api
        from services.image_poll_budget import poll_exhaust_snapshot

        inflight = int(ai_api._image_sync_wait_inflight())
        limit = int(ai_api._image_sync_admission_limit())
        max_eta = float(ai_api._image_sync_admission_max_eta_secs())
        busy_429 = int(ai_api._image_sync_busy_429_count())
        try:
            eta = int(image_task_service.estimate_sync_eta_secs({}, extra_waiters=inflight))
        except Exception:
            eta = 0
        return {
            "admission_inflight": inflight,
            "admission_max": limit,
            "eta_secs": eta,
            "max_eta_secs": max_eta,
            "busy_429_count": busy_429,
            "poll_exhausted": poll_exhaust_snapshot(),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:160]}


def _burst_snapshot() -> dict[str, Any]:
    raw = config.data if isinstance(getattr(config, "data", None), dict) else {}
    base = int(raw.get("per_user_running_base") or raw.get("per_user_running_max") or 2)
    burst_max = int(raw.get("per_user_running_burst") or raw.get("per_user_running_max") or base)
    enabled = bool(raw.get("burst_enabled", False))
    try:
        with image_task_service._lock:  # type: ignore[attr-defined]
            effective = int(image_task_service._effective_per_user_running_max_locked())
    except Exception:
        effective = int(raw.get("per_user_running_max") or base)
    return {
        "burst_enabled": enabled,
        "per_user_running_base": base,
        "per_user_running_burst": burst_max,
        "effective_per_user_running_max": effective,
        "burst_active": bool(enabled and effective > base),
    }


def _cohort_snapshot(accounts: list[dict]) -> dict[str, Any]:
    hits = getattr(account_service, "_cohort_terminal_hits", {}) or {}
    pause = getattr(account_service, "_cohort_pause_until", {}) or {}
    now = time.time()
    by_id: Counter[str] = Counter()
    for a in accounts:
        cid = str(a.get("cohort_id") or "").strip()
        if cid:
            by_id[cid] += 1
    rows = []
    paused_count = 0
    for cid, n in by_id.most_common(20):
        until = float(pause.get(cid) or 0)
        paused = until > now
        if paused:
            paused_count += 1
        rows.append(
            {
                "cohort_id": cid,
                "accounts": n,
                "terminals": int(hits.get(cid) or 0),
                "paused": paused,
                "pause_until": until if paused else None,
            }
        )
    # orphan pause keys
    for cid, until in list(pause.items()):
        if cid in by_id:
            continue
        if float(until or 0) > now:
            paused_count += 1
            rows.append(
                {
                    "cohort_id": cid,
                    "accounts": 0,
                    "terminals": int(hits.get(cid) or 0),
                    "paused": True,
                    "pause_until": float(until),
                }
            )
    return {
        "cohorts": rows,
        "cohort_paused": 1 if paused_count else 0,
        "cohort_terminal_hits_sum": int(sum(int(v or 0) for v in hits.values())),
        "paused_cohort_count": paused_count,
    }


def _llm_ops_window(*, hours: float = 6.0) -> dict[str, Any]:
    cutoff = time.time() - hours * 3600.0
    by_outcome: Counter[str] = Counter()
    by_hour: dict[str, Counter[str]] = defaultdict(Counter)
    try:
        # Prefer recent scan of jsonl via log_service internals if available
        path = getattr(log_service, "path", None)
        if path is None:
            return {"ok": 0, "error": 0, "reject": 0, "hourly": []}
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return {"ok": 0, "error": 0, "reject": 0, "hourly": []}
        # read last ~2MB
        data = p.read_bytes()
        if len(data) > 2_000_000:
            data = data[-2_000_000:]
        text = data.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            if '"llm_ops"' not in line and f'"{LOG_TYPE_LLM_OPS}"' not in line:
                continue
            try:
                import json

                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != LOG_TYPE_LLM_OPS:
                continue
            # created may be iso or missing
            created = item.get("created_at") or item.get("time") or item.get("ts")
            ts = 0.0
            if isinstance(created, (int, float)):
                ts = float(created)
            elif isinstance(created, str) and created:
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = 0.0
            if ts and ts < cutoff:
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            outcome = str(detail.get("outcome") or "ok").strip() or "ok"
            source = str(detail.get("source") or item.get("source") or "").strip().lower()
            by_outcome[outcome] += 1
            if outcome == "error" and source == "risk_audit":
                by_outcome["error_ops"] += 1
            elif outcome == "error":
                by_outcome["error_pool"] += 1
            hour = time.strftime("%H", time.gmtime(ts or time.time()))
            by_hour[hour][outcome] += 1
            if outcome == "error" and source == "risk_audit":
                by_hour[hour]["error_ops"] += 1
            elif outcome == "error":
                by_hour[hour]["error_pool"] += 1
    except Exception:
        pass
    hourly = []
    for hour in sorted(by_hour.keys()):
        c = by_hour[hour]
        hourly.append(
            {
                "hour": hour,
                "ok": c.get("ok", 0),
                "error": c.get("error", 0),
                "error_pool": c.get("error_pool", 0),
                "error_ops": c.get("error_ops", 0),
                "reject": c.get("reject", 0),
            }
        )
    return {
        "ok": int(by_outcome.get("ok", 0)),
        "error": int(by_outcome.get("error", 0)),
        "error_pool": int(by_outcome.get("error_pool", 0)),
        "error_ops": int(by_outcome.get("error_ops", 0)),
        "reject": int(by_outcome.get("reject", 0)),
        "hourly": hourly[-12:],
    }


def collect_account_derived(accounts: list[dict]) -> dict[str, Any]:
    now = time.time()
    receive: Counter[str] = Counter()
    soft_capped = 0
    fail_streak_ge3 = 0
    cooldown = 0
    lazy_due = 0
    maturity_set = 0
    maturity_empty = 0
    fp_ok = 0
    binding_counts: Counter[str] = Counter()
    traffic_rows: list[dict[str, Any]] = []
    soft_rows: list[dict[str, Any]] = []
    streak_hist: Counter[str] = Counter()

    for a in accounts:
        if not isinstance(a, dict):
            continue
        rs = str(a.get("panda_receive_state") or "").strip() or "unknown"
        receive[rs] += 1
        if bool(a.get("image_soft_capped")):
            soft_capped += 1
        streak = int(a.get("image_fail_streak") or 0)
        if streak >= 3:
            fail_streak_ge3 += 1
            streak_hist["≥3"] += 1
        elif streak <= 0:
            streak_hist["0"] += 1
        else:
            streak_hist[str(streak)] += 1
        until = float(a.get("image_fail_cooldown_until") or 0)
        if until > now:
            cooldown += 1
        try:
            if account_service._quota_window_due_for_lazy_refresh(a):  # noqa: SLF001
                lazy_due += 1
        except Exception:
            pass
        if str(a.get("maturity_stage") or "").strip():
            maturity_set += 1
        else:
            maturity_empty += 1
        fp = a.get("fp")
        if isinstance(fp, dict) and (fp.get("user-agent") or fp.get("impersonate")):
            fp_ok += 1
        bh = str(a.get("proxy_binding_hash") or "").strip()
        if bh:
            binding_counts[bh] += 1
        traffic_rows.append(
            {
                "email_mask": _mask_email(str(a.get("email") or "")),
                "traffic_total_bytes": int(a.get("traffic_total_bytes") or 0),
            }
        )
        soft_rows.append(
            {
                "email_mask": _mask_email(str(a.get("email") or "")),
                "soft_band": a.get("image_soft_band"),
                "used_ratio": a.get("image_soft_used_ratio"),
                "soft_capped": bool(a.get("image_soft_capped")),
                "quota": a.get("quota"),
            }
        )

    dup = sum(1 for _, n in binding_counts.items() if n > max(1, int(getattr(config, "proxy_binding_max_accounts", 5) or 5)))
    traffic_rows.sort(key=lambda x: int(x["traffic_total_bytes"] or 0), reverse=True)
    return {
        "receive_state": dict(receive),
        "soft_capped_count": soft_capped,
        "fail_streak_ge3": fail_streak_ge3,
        "cooldown_account_count": cooldown,
        "lazy_due_count": lazy_due,
        "maturity_set": maturity_set,
        "maturity_empty": maturity_empty,
        "fp_complete": fp_ok,
        "fp_total": len(accounts),
        "sticky_unique_bindings": len(binding_counts),
        "dup_binding_groups": dup,
        "traffic_top": traffic_rows[:8],
        "soft_band_rows": soft_rows[:12],
        "streak_hist": [
            {"bucket": k, "n": int(streak_hist.get(k) or 0)} for k in ("0", "1", "2", "≥3")
        ],
    }


def build_snapshot() -> dict[str, Any]:
    account_service.reload_from_storage()
    accounts = [a for a in account_service.list_accounts() if isinstance(a, dict)]
    try:
        breakdown = account_service.get_schedulable_breakdown()
    except Exception as exc:
        breakdown = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        stats = account_service.get_stats() if hasattr(account_service, "get_stats") else {}
    except Exception:
        stats = {}
    derived = collect_account_derived(accounts)
    admission = _admission_snapshot()
    burst = _burst_snapshot()
    cohort = _cohort_snapshot(accounts)
    llm = _llm_ops_window(hours=6.0)
    nurture = text_nurture_service.status()
    scheduler = config.get_scheduler_settings() if hasattr(config, "get_scheduler_settings") else {}
    workload = config.get_workload_settings() if hasattr(config, "get_workload_settings") else {}
    proactive: dict[str, Any] = {}
    try:
        from services.proactive_refresh_loop_service import proactive_refresh_loop_service

        for name in ("status", "get_status", "snapshot"):
            fn = getattr(proactive_refresh_loop_service, name, None)
            if callable(fn):
                proactive = fn() or {}
                break
        if not proactive:
            proactive = {
                "enabled": bool((config.data.get("proactive_refresh") or {}).get("enabled"))
                if isinstance(config.data.get("proactive_refresh"), dict)
                else False
            }
    except Exception as exc:
        proactive = {"error": f"{type(exc).__name__}: {exc}"[:120]}
    maintenance = bool((config.data.get("account_maintenance_loop") or {}).get("enabled")) if isinstance(config.data.get("account_maintenance_loop"), dict) else False
    recovery = bool((config.data.get("outlook_auto_recovery") or {}).get("enabled")) if isinstance(config.data.get("outlook_auto_recovery"), dict) else False

    gaps = []
    if not any(str(a.get("maturity_stage") or "").strip() for a in accounts):
        gaps.append("maturity_stage_mostly_empty")
    if str(workload.get("mode") or "shadow") != "live":
        gaps.append("workload_shadow")
    gaps.append("proxy_nodes_table_missing")
    gaps.append("maturity_auto_fsm_missing")

    snap: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "account_count": len(accounts),
        "stats": stats if isinstance(stats, dict) else {},
        "breakdown": breakdown,
        "derived": derived,
        "admission": admission,
        "burst": burst,
        "cohort": cohort,
        "llm_ops": llm,
        "nurture": {
            "enabled": nurture.get("enabled"),
            "depth": (nurture.get("queue") or {}).get("depth") if isinstance(nurture.get("queue"), dict) else nurture.get("queue"),
            "completed_in_hour": nurture.get("completed_in_hour"),
            "max_per_hour": nurture.get("max_per_hour"),
            "max_per_account_per_day": nurture.get("max_per_account_per_day"),
            "turns_per_session": nurture.get("turns_per_session"),
            "today_completed_total": nurture.get("today_completed_total"),
            "accounts_at_cap": nurture.get("accounts_at_cap"),
        },
        "pipeline": {
            "image_queue_depth": int(image_task_service.queue_depth()),
            "ewma_success_secs": float(image_task_service.success_duration_ewma_secs()),
            "image_inflight": int((breakdown.get("runtime") or {}).get("image_inflight_count") or 0) if isinstance(breakdown, dict) else 0,
        },
        "scheduler": scheduler,
        "workload": workload,
        "proactive": proactive,
        "discipline": {
            "maintenance_enabled": maintenance,
            "recovery_enabled": recovery,
            "maintenance_expected_off": not maintenance,
            "recovery_expected_off": not recovery,
        },
        "gaps": gaps,
        "shape_hash_cardinality_hint": None,  # phase-2 hook
    }
    return snap


def metrics_point_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    bd = snap.get("breakdown") if isinstance(snap.get("breakdown"), dict) else {}
    buckets = bd.get("buckets") if isinstance(bd.get("buckets"), dict) else {}
    derived = snap.get("derived") if isinstance(snap.get("derived"), dict) else {}
    admission = snap.get("admission") if isinstance(snap.get("admission"), dict) else {}
    cohort = snap.get("cohort") if isinstance(snap.get("cohort"), dict) else {}
    llm = snap.get("llm_ops") if isinstance(snap.get("llm_ops"), dict) else {}
    nurture = snap.get("nurture") if isinstance(snap.get("nurture"), dict) else {}
    pipeline = snap.get("pipeline") if isinstance(snap.get("pipeline"), dict) else {}
    burst = snap.get("burst") if isinstance(snap.get("burst"), dict) else {}
    stats = snap.get("stats") if isinstance(snap.get("stats"), dict) else {}
    runtime = bd.get("runtime") if isinstance(bd.get("runtime"), dict) else {}
    return {
        "schedulable": int(buckets.get("schedulable") or stats.get("schedulable") or 0),
        "dispatchable": int(runtime.get("dispatchable_candidate_count") or buckets.get("schedulable") or 0),
        "excluded_by_receive_state": int(buckets.get("excluded_by_receive_state") or 0),
        "excluded_by_quota": int(buckets.get("excluded_by_quota") or 0),
        "soft_capped_count": int(derived.get("soft_capped_count") or 0),
        "cooldown_account_count": int(derived.get("cooldown_account_count") or 0),
        "fail_streak_ge3": int(derived.get("fail_streak_ge3") or 0),
        "lazy_due_count": int(derived.get("lazy_due_count") or 0),
        "admission_inflight": int(admission.get("admission_inflight") or 0),
        "admission_max": int(admission.get("admission_max") or 0),
        "eta_secs": int(admission.get("eta_secs") or 0),
        "busy_429_count": int(admission.get("busy_429_count") or 0),
        "poll_exhausted_wall": int((admission.get("poll_exhausted") or {}).get("wall") or 0)
        if isinstance(admission.get("poll_exhausted"), dict)
        else 0,
        "poll_exhausted_conversation_get": int((admission.get("poll_exhausted") or {}).get("conversation_get") or 0)
        if isinstance(admission.get("poll_exhausted"), dict)
        else 0,
        "poll_exhausted_tasks": int((admission.get("poll_exhausted") or {}).get("tasks") or 0)
        if isinstance(admission.get("poll_exhausted"), dict)
        else 0,
        "image_queue_depth": int(pipeline.get("image_queue_depth") or 0),
        "ewma_success_secs": float(pipeline.get("ewma_success_secs") or 0),
        "burst_active": 1 if burst.get("burst_active") else 0,
        "cohort_paused": int(cohort.get("cohort_paused") or 0),
        "cohort_terminal_hits_sum": int(cohort.get("cohort_terminal_hits_sum") or 0),
        "llm_ops_ok": int(llm.get("ok") or 0),
        "llm_ops_error": int(llm.get("error_pool") or llm.get("error") or 0),
        "llm_ops_error_ops": int(llm.get("error_ops") or 0),
        "llm_ops_reject": int(llm.get("reject") or 0),
        "stale_quota_count": int(stats.get("stale_quota_count") or 0),
        "incoming": int((derived.get("receive_state") or {}).get("incoming") or 0),
        "verified_ready": int((derived.get("receive_state") or {}).get("verified_ready") or 0),
        "identity_isolated": int((derived.get("receive_state") or {}).get("identity_isolated") or 0),
        "nurture_depth": int(nurture.get("depth") or 0) if str(nurture.get("depth") or "").isdigit() or isinstance(nurture.get("depth"), int) else int(nurture.get("depth") or 0),
        "nurture_completed_in_hour": int(nurture.get("completed_in_hour") or 0),
        "preflight_backoff": int(runtime.get("preflight_backoff_count") or stats.get("preflight_backoff_count") or 0),
        "image_inflight": int(pipeline.get("image_inflight") or runtime.get("image_inflight_count") or 0),
    }


def record_metrics_tick() -> dict[str, Any]:
    snap = build_snapshot()
    point = metrics_point_from_snapshot(snap)
    return append_metrics_point(point)


# 自动化「风险」趋势因子权重（合计 1.0；分越高越危险）
AUTOMATION_WEIGHTS = {
    "detection": 0.30,  # 官方检测/身份隔离压力
    "soft_risk": 0.25,  # 软熔断风控
    "fail_risk": 0.20,  # 失败连击
    "cohort_risk": 0.15,  # 群组暂停/终态
    "edge_risk": 0.10,  # 边缘限流/业务 LLM 失败
}


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def automation_factors_from_point(p: dict[str, Any]) -> dict[str, float]:
    """从半小时 metrics 点计算「被官方检测 / 被风控」风险因子（0–100%，越高越差）。"""
    sched = float(p.get("schedulable") or 0)
    soft = float(p.get("soft_capped_count") or 0)
    isolated = float(p.get("identity_isolated") or 0)
    incoming = float(p.get("incoming") or 0)
    streak = float(p.get("fail_streak_ge3") or 0)
    cool = float(p.get("cooldown_account_count") or 0)
    cohort_paused = float(p.get("cohort_paused") or 0)
    cohort_term = float(p.get("cohort_terminal_hits_sum") or 0)
    busy429 = float(p.get("busy_429_count") or 0)
    llm_ok = float(p.get("llm_ops_ok") or 0)
    llm_err = float(p.get("llm_ops_error") or 0)
    pool = max(1.0, sched + soft + isolated + incoming)

    # 官方检测压力：隔离观察号占比 + 入库观察轻度计入
    detection = _clamp_pct(100.0 * (isolated + 0.35 * incoming) / pool)

    # 风控软熔断：软熔断 + 冷却
    soft_risk = _clamp_pct(100.0 * (soft + 0.5 * cool) / pool)

    # 失败连击风险
    fail_risk = _clamp_pct(100.0 * streak / pool)

    # 群组风控：有暂停直接抬高；终态命中按池归一
    cohort_risk = 80.0 if cohort_paused > 0 else _clamp_pct(min(100.0, cohort_term * 12.0))

    # 边缘/限流：429 忙拒与业务 LLM 失败率
    llm_fail_rate = 100.0 * llm_err / max(1.0, llm_ok + llm_err) if (llm_ok + llm_err) > 0 else 0.0
    edge_risk = _clamp_pct(min(100.0, busy429 * 8.0) * 0.55 + llm_fail_rate * 0.45)

    composite = (
        AUTOMATION_WEIGHTS["detection"] * detection
        + AUTOMATION_WEIGHTS["soft_risk"] * soft_risk
        + AUTOMATION_WEIGHTS["fail_risk"] * fail_risk
        + AUTOMATION_WEIGHTS["cohort_risk"] * cohort_risk
        + AUTOMATION_WEIGHTS["edge_risk"] * edge_risk
    )
    return {
        "detection": round(detection, 1),
        "soft_risk": round(soft_risk, 1),
        "fail_risk": round(fail_risk, 1),
        "cohort_risk": round(cohort_risk, 1),
        "edge_risk": round(edge_risk, 1),
        "composite": round(_clamp_pct(composite), 1),
    }


def build_automation_daily(*, days: int = 30) -> list[dict[str, Any]]:
    """按日平均自动化风险因子（来自 risk_metrics 半小时点）。"""
    days = max(1, min(90, int(days)))
    series = list_metrics(limit=2000)
    by_day: dict[str, list[dict[str, float]]] = defaultdict(list)
    for p in series:
        day = str(p.get("ts") or "")[:10]
        if len(day) < 10:
            continue
        by_day[day].append(automation_factors_from_point(p))

    today = datetime.now(timezone.utc).date()
    out: list[dict[str, Any]] = []
    keys = ["detection", "soft_risk", "fail_risk", "cohort_risk", "edge_risk", "composite"]
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        rows = by_day.get(d) or []
        if not rows:
            out.append({"date": d, **{k: None for k in keys}, "samples": 0})
            continue
        avg = {k: round(sum(float(r[k]) for r in rows) / len(rows), 1) for k in keys}
        out.append({"date": d, **avg, "samples": len(rows)})
    return out


def build_dashboard() -> dict[str, Any]:
    snap = build_snapshot()
    series = list_metrics(limit=336)
    reports = list_reports(limit=24)
    return {
        "ok": True,
        "snapshot": snap,
        "series": series,
        "automation_daily": build_automation_daily(days=30),
        "automation_weights": dict(AUTOMATION_WEIGHTS),
        "recent_checks": reports,
        "kpi": {
            "schedulable": metrics_point_from_snapshot(snap).get("schedulable"),
            "identity_isolated": metrics_point_from_snapshot(snap).get("identity_isolated"),
            "soft_capped": metrics_point_from_snapshot(snap).get("soft_capped_count"),
            "admission": f"{metrics_point_from_snapshot(snap).get('admission_inflight')}/{metrics_point_from_snapshot(snap).get('admission_max')}",
            "workload_mode": (snap.get("workload") or {}).get("mode"),
            "cohort_paused": metrics_point_from_snapshot(snap).get("cohort_paused"),
        },
    }


def build_calendar(*, days: int = 112) -> dict[str, Any]:
    days = max(7, min(200, int(days)))
    series = list_metrics(limit=2000)
    reports = list_reports(limit=200)
    by_day: dict[str, dict[str, float]] = defaultdict(
        lambda: {"load": 0.0, "fail": 0.0, "llm_err": 0.0, "critical": 0.0, "points": 0}
    )
    level_rank = {"ok": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    day_level: dict[str, str] = {}

    for p in series:
        ts = p.get("ts") or ""
        day = str(ts)[:10]
        if len(day) < 10:
            continue
        bucket = by_day[day]
        bucket["points"] += 1
        bucket["load"] += float(p.get("admission_inflight") or 0) + float(p.get("image_queue_depth") or 0)
        bucket["fail"] += float(p.get("soft_capped_count") or 0) + float(p.get("fail_streak_ge3") or 0)
        bucket["llm_err"] += float(p.get("llm_ops_error") or 0)
        if int(p.get("cohort_paused") or 0):
            bucket["load"] += 2

    for r in reports:
        day = str(r.get("finished_at") or r.get("started_at") or "")[:10]
        if len(day) < 10:
            continue
        level = str(r.get("risk_level") or "").lower()
        if level in {"high", "critical"}:
            by_day[day]["critical"] += 2 if level == "critical" else 1
        if level in level_rank:
            prev = day_level.get(day, "ok")
            if level_rank[level] >= level_rank.get(prev, 0):
                day_level[day] = level

    today = datetime.now(timezone.utc).date()
    cells = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        b = by_day.get(key) or {"load": 0, "fail": 0, "llm_err": 0, "critical": 0, "points": 0}
        score = b["load"] + b["fail"] * 1.5 + b["llm_err"] * 2 + b["critical"] * 3
        # normalize to 0-4 by volume
        intensity = 0
        if score > 0:
            intensity = 1
        if score >= 3:
            intensity = 2
        if score >= 8:
            intensity = 3
        if score >= 16:
            intensity = 4
        risk_level = day_level.get(key) or ("ok" if score <= 0 else "low" if intensity <= 1 else "medium" if intensity <= 2 else "high" if intensity <= 3 else "critical")
        # color_level：信息量 intensity 与风险等级取高
        color_level = max(intensity, level_rank.get(risk_level, 0))
        cells.append(
            {
                "date": key,
                "intensity": intensity,
                "color_level": color_level,
                "risk_level": risk_level,
                "score": round(score, 2),
                "detail": {
                    "load": round(float(b["load"]), 2),
                    "fail": round(float(b["fail"]), 2),
                    "llm_err": round(float(b["llm_err"]), 2),
                    "critical": round(float(b["critical"]), 2),
                    "points": int(b["points"]),
                },
            }
        )
    return {"ok": True, "days": days, "cells": cells}

