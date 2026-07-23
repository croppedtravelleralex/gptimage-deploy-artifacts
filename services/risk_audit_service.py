#!/usr/bin/env python3
"""Proactive half-hour risk audit: DeepSeek (NewAPI) draft → L0 GPT short summary."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from services.config import config
from services.log_service import log_llm_ops
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import ConversationRequest, collect_text
from services.proxy_service import proxy_settings
from services.risk_dashboard_service import build_snapshot, record_metrics_tick
from services.risk_metrics_store import append_report, list_reports
from utils.log import logger

SCOPE_ITEMS = [
    "health_breakdown",
    "receive_sticky_soft",
    "scheduler_workload_discipline",
    "lazy_quota_freshness",
    "admission_queue_ewma_burst",
    "streak_cooldown_cohort",
    "nurture_persist_llm_ops",
    "maturity_gaps_proxy",
]


def _risk_settings() -> dict[str, Any]:
    raw = config.data.get("risk_audit") if isinstance(getattr(config, "data", None), dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    # Fall back to ai_review credentials for DeepSeek / NewAPI
    review = config.ai_review if isinstance(config.ai_review, dict) else {}
    base_url = str(raw.get("base_url") or review.get("base_url") or "").strip().rstrip("/")
    api_key = str(raw.get("api_key") or review.get("api_key") or "").strip()
    model = str(raw.get("model") or review.get("model") or "deepseek-chat").strip()
    return {
        "enabled": bool(raw.get("enabled", False)),
        "interval_sec": max(300.0, float(raw.get("interval_sec", 1800.0) or 1800.0)),
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout_sec": max(20.0, float(raw.get("timeout_sec", 90.0) or 90.0)),
        "record_metrics_on_tick": bool(raw.get("record_metrics_on_tick", True)),
    }


KNOWN_LIMITATION_GAPS = {
    "maturity_stage_mostly_empty",
    "workload_shadow",
    "proxy_nodes_table_missing",
    "maturity_auto_fsm_missing",
}


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse first JSON object from LLM output; tolerate trailing garbage (Extra data)."""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    def _as_dict(raw: Any) -> dict[str, Any] | None:
        return raw if isinstance(raw, dict) else None

    # Prefer decoder that stops after first value (handles "Extra data: line N").
    try:
        decoder = json.JSONDecoder()
        obj, _end = decoder.raw_decode(text)
        parsed = _as_dict(obj)
        if parsed is not None:
            return parsed
    except Exception:
        pass

    try:
        parsed = _as_dict(json.loads(text))
        if parsed is not None:
            return parsed
    except Exception:
        pass

    start = text.find("{")
    if start < 0:
        raise json.JSONDecodeError("no_json_object", text, 0)
    try:
        decoder = json.JSONDecoder()
        obj, _end = decoder.raw_decode(text[start:])
        parsed = _as_dict(obj)
        if parsed is not None:
            return parsed
    except Exception:
        pass

    end = text.rfind("}")
    if end > start:
        try:
            parsed = _as_dict(json.loads(text[start : end + 1]))
            if parsed is not None:
                return parsed
        except Exception:
            pass
    raise json.JSONDecodeError("no_json_object", text, 0)


def _deterministic_risk_level(snapshot: dict[str, Any]) -> str:
    """Pool-health first. Known missing features should not force medium."""
    derived = snapshot.get("derived") if isinstance(snapshot.get("derived"), dict) else {}
    breakdown = snapshot.get("breakdown") if isinstance(snapshot.get("breakdown"), dict) else {}
    buckets = breakdown.get("buckets") if isinstance(breakdown.get("buckets"), dict) else {}
    llm = snapshot.get("llm_ops") if isinstance(snapshot.get("llm_ops"), dict) else {}
    sched = int(buckets.get("schedulable") or 0)
    soft = int(derived.get("soft_capped_count") or 0)
    cool = int(derived.get("cooldown_account_count") or 0)
    streak = int(derived.get("fail_streak_ge3") or 0)
    dup = int(derived.get("dup_binding_groups") or 0)
    paused = int((snapshot.get("cohort") or {}).get("paused_cohort_count") or 0) if isinstance(snapshot.get("cohort"), dict) else 0
    pool_err = int(llm.get("error_pool") or 0)
    pool_ok = int(llm.get("ok") or 0)

    if sched <= 0 and soft > 0:
        return "high"
    if paused > 0 or dup > 0 or streak >= 3:
        return "medium"
    if soft > 0 or cool > 0:
        return "low"
    if sched >= 1:
        if pool_err >= 8 and pool_err > pool_ok:
            return "low"
        return "ok"
    return "low"


def _call_deepseek(snapshot: dict[str, Any], settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if not settings["base_url"] or not settings["api_key"]:
        return (
            {
                "findings": [{"item": "deepseek_config", "detail": "base_url/api_key missing — skipped draft"}],
                "risk_level_hint": "low",
                "notes": "DeepSeek unavailable; GPT will summarize snapshot only.",
            },
            0,
        )
    gaps = [g for g in (snapshot.get("gaps") or []) if g not in KNOWN_LIMITATION_GAPS]
    known = [g for g in (snapshot.get("gaps") or []) if g in KNOWN_LIMITATION_GAPS]
    compact = {
        "kpi": {
            "accounts": snapshot.get("account_count"),
            "schedulable": (snapshot.get("breakdown") or {}).get("buckets", {}).get("schedulable")
            if isinstance(snapshot.get("breakdown"), dict)
            else None,
            "isolated": (snapshot.get("derived") or {}).get("receive_state", {}).get("identity_isolated"),
            "soft_capped": (snapshot.get("derived") or {}).get("soft_capped_count"),
            "admission": {
                "inflight": (snapshot.get("admission") or {}).get("admission_inflight"),
                "max": (snapshot.get("admission") or {}).get("admission_max"),
            }
            if isinstance(snapshot.get("admission"), dict)
            else snapshot.get("admission"),
            "queue_depth": (snapshot.get("pipeline") or {}).get("image_queue_depth")
            if isinstance(snapshot.get("pipeline"), dict)
            else None,
            "cohort_paused": (snapshot.get("cohort") or {}).get("paused_cohort_count")
            if isinstance(snapshot.get("cohort"), dict)
            else None,
            "llm_ops_pool": {
                "ok": (snapshot.get("llm_ops") or {}).get("ok"),
                "error": (snapshot.get("llm_ops") or {}).get("error_pool"),
            }
            if isinstance(snapshot.get("llm_ops"), dict)
            else None,
            "action_gaps": gaps,
            "known_limitations": known,
            "dup_binding_groups": (snapshot.get("derived") or {}).get("dup_binding_groups"),
            "cooldown": (snapshot.get("derived") or {}).get("cooldown_account_count"),
            "fail_streak_ge3": (snapshot.get("derived") or {}).get("fail_streak_ge3"),
        },
        "scope": SCOPE_ITEMS,
    }
    prompt = (
        "你是号池风控助手。根据 JSON 快照做检查草稿。"
        "只使用快照中的数字，禁止编造。"
        "known_limitations 是已知未建能力，不算中等以上风险。"
        "若可调度>0、无软熔断、无群组暂停，risk_level_hint 应为 ok 或 low。"
        "输出严格 JSON 对象（不要 markdown）："
        '{"findings":[{"item":"...","detail":"..."}],"risk_level_hint":"ok|low|medium|high|critical","notes":"..."}'
        f"\n\n快照:\n{json.dumps(compact, ensure_ascii=False)[:12000]}"
    )
    started = time.monotonic()
    try:
        payload: dict[str, Any] = {
            "model": settings["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            f"{settings['base_url']}/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
            json=payload,
            timeout=float(settings["timeout_sec"]),
            **proxy_settings.build_session_kwargs(),
        )
        latency = int((time.monotonic() - started) * 1000)
        response.raise_for_status()
        body = response.json()
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        draft = _extract_json_object(str(content))
        log_llm_ops(
            source="risk_audit",
            kind="ops_rca",
            latency_ms=latency,
            outcome="ok",
            prompt_shape={"chars": len(prompt), "model": settings["model"], "role": "deepseek_draft"},
            summary="risk_audit deepseek draft",
        )
        return draft, latency
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        code = "deepseek_json" if isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        log_llm_ops(
            source="risk_audit",
            kind="ops_rca",
            latency_ms=latency,
            outcome="error",
            outcome_code=code,
            prompt_shape={"model": settings["model"], "role": "deepseek_draft"},
            summary="risk_audit deepseek failed",
        )
        return (
            {
                "findings": [{"item": "deepseek_error", "detail": f"{type(exc).__name__}: {exc}"[:200]}],
                "risk_level_hint": "low",
                "notes": "DeepSeek draft unavailable（文案助手挂了 ≠ 号池风险）; use deterministic snapshot level.",
            },
            latency,
        )



def _call_gpt_summary(
    snapshot: dict[str, Any],
    draft: dict[str, Any],
    *,
    det_level: str | None = None,
) -> tuple[str, str, int]:
    from services.account_service import account_service

    det = det_level or _deterministic_risk_level(snapshot)
    draft_hint = str(draft.get("risk_level_hint") or "low").lower()
    # Prefer pool-health deterministic level; ignore DeepSeek fail → medium.
    hint = det if det in {"ok", "low", "medium", "high", "critical"} else draft_hint
    findings = draft.get("findings") if isinstance(draft.get("findings"), list) else []
    finding_text = "; ".join(
        f"{(f or {}).get('item')}:{(f or {}).get('detail')}"[:80] for f in findings[:8] if isinstance(f, dict)
    )
    gaps_raw = snapshot.get("gaps") if isinstance(snapshot.get("gaps"), list) else []
    gaps_action = [g for g in gaps_raw if g not in KNOWN_LIMITATION_GAPS]
    prompt = (
        "用简体中文写不超过200字的号池风控摘要。必须包含：可调度/隔离/熔断或准入要点；"
        "给出风险等级之一 正常/偏低/中等/偏高/严重（或 ok/low/medium/high/critical）；"
        "已知未建能力（成熟度空、工作负载影子、代理节点表缺失）不要抬高到中等；"
        "不要写降封百分比；不要编造数字；不要输出 JSON。\n"
        f"建议风险等级={hint}（请优先采用，除非数字明显更差）\n"
        f"schedulable={(snapshot.get('breakdown') or {}).get('buckets', {}).get('schedulable')}\n"
        f"isolated={(snapshot.get('derived') or {}).get('receive_state', {}).get('identity_isolated')}\n"
        f"soft={(snapshot.get('derived') or {}).get('soft_capped_count')}\n"
        f"admission_inflight={(snapshot.get('admission') or {}).get('admission_inflight') if isinstance(snapshot.get('admission'), dict) else snapshot.get('admission')}\n"
        f"findings={finding_text}\n"
        f"action_gaps={gaps_action}\n"
        "格式：第一行「风险等级：正常」这类中文；随后两三句结论。"
    )
    started = time.monotonic()
    try:
        token = account_service.get_text_access_token()
        backend = OpenAIBackendAPI(token)
        # Prefer persist-off temporary chat for ops summary
        text = collect_text(
            backend,
            ConversationRequest(model="auto", messages=[{"role": "user", "content": prompt}]),
        )
        latency = int((time.monotonic() - started) * 1000)
        summary = str(text or "").strip()[:500]
        level = hint
        level_map = {
            "正常": "ok",
            "偏低": "low",
            "中等": "medium",
            "偏高": "high",
            "严重": "critical",
            "ok": "ok",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "critical": "critical",
        }
        for line in summary.splitlines():
            raw = line.strip()
            upper = raw.upper()
            if upper.startswith("RISK="):
                level = raw.split("=", 1)[-1].strip().lower() or hint
                break
            if "风险等级" in raw:
                for zh, en in level_map.items():
                    if zh in raw:
                        level = en
                        break
                break
        if level not in {"ok", "low", "medium", "high", "critical"}:
            level = hint if hint in {"ok", "low", "medium", "high", "critical"} else "low"
        # Strip machine prefix lines for UI
        cleaned_lines = []
        for line in summary.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.upper().startswith("RISK="):
                continue
            cleaned_lines.append(s)
        summary = "\n".join(cleaned_lines) or summary
        log_llm_ops(
            source="risk_audit",
            kind="summarize",
            access_token=token,
            latency_ms=latency,
            outcome="ok",
            prompt_shape={"chars": len(prompt), "role": "gpt_summary"},
            summary="risk_audit gpt summary",
        )
        return summary, level, latency
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        log_llm_ops(
            source="risk_audit",
            kind="summarize",
            latency_ms=latency,
            outcome="error",
            outcome_code=type(exc).__name__,
            prompt_shape={"role": "gpt_summary"},
            summary="risk_audit gpt failed",
        )
        # Deterministic fallback
        bd = (snapshot.get("breakdown") or {}).get("buckets") or {}
        level_zh = {"ok": "正常", "low": "偏低", "medium": "中等", "high": "偏高", "critical": "严重"}.get(hint, "中等")
        summary = (
            f"风险等级：{level_zh}\n"
            f"可派发 {bd.get('schedulable')}；隔离 "
            f"{(snapshot.get('derived') or {}).get('receive_state', {}).get('identity_isolated')}；"
            f"软熔断 {(snapshot.get('derived') or {}).get('soft_capped_count')}。"
            f" GPT 总结失败（{type(exc).__name__}），以上为快照直出。"
        )
        return summary[:500], hint if hint in {"ok", "low", "medium", "high", "critical"} else "medium", latency


class RiskAuditService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_error = ""
        self._last_ok_at = 0.0
        self._last_report: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        settings = _risk_settings()
        with self._lock:
            return {
                "enabled": settings["enabled"],
                "interval_sec": settings["interval_sec"],
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "running": self._running,
                "deepseek_configured": bool(settings["base_url"] and settings["api_key"]),
                "model": settings["model"],
                "last_error": self._last_error,
                "last_ok_at": self._last_ok_at or None,
                "last_report_id": (self._last_report or {}).get("id"),
                "recent": list_reports(limit=5),
            }

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="risk-audit", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def run_once(self, *, source: str = "manual") -> dict[str, Any]:
        settings = _risk_settings()
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.time()
        with self._lock:
            self._running = True
            self._last_error = ""
        try:
            if settings.get("record_metrics_on_tick"):
                try:
                    record_metrics_tick()
                except Exception as exc:
                    logger.warning({"event": "risk_metrics_tick_failed", "error": str(exc)[:160]})
            snapshot = build_snapshot()
            draft, ds_ms = _call_deepseek(snapshot, settings)
            det_level = _deterministic_risk_level(snapshot)
            summary, gpt_level, gpt_ms = _call_gpt_summary(snapshot, draft, det_level=det_level)
            # Prefer deterministic pool health; only keep GPT escalate for high/critical.
            level = det_level
            if gpt_level in {"high", "critical"} and det_level in {"ok", "low"}:
                level = "low" if gpt_level == "high" else "medium"
            elif gpt_level == "medium" and det_level == "ok":
                level = "ok"
            elif det_level in {"medium", "high", "critical"}:
                level = det_level
            report = {
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.time() - t0) * 1000),
                "source": source,
                "scope": list(SCOPE_ITEMS),
                "models": {
                    "deepseek": {"model": settings["model"], "latency_ms": ds_ms},
                    "gpt_l0": {"latency_ms": gpt_ms},
                },
                "risk_level": level,
                "risk_level_gpt": gpt_level,
                "risk_level_deterministic": det_level,
                "summary": summary,
                "findings": draft.get("findings") if isinstance(draft.get("findings"), list) else [],
                "outcome": "ok",
                "snapshot_kpi": {
                    "account_count": snapshot.get("account_count"),
                    "schedulable": (snapshot.get("breakdown") or {}).get("buckets", {}).get("schedulable")
                    if isinstance(snapshot.get("breakdown"), dict)
                    else None,
                    "admission": snapshot.get("admission"),
                    "gaps": snapshot.get("gaps"),
                },
            }
            saved = append_report(report)
            with self._lock:
                self._last_ok_at = time.time()
                self._last_report = saved
            return saved
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"[:240]
            err_report = {
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "scope": list(SCOPE_ITEMS),
                "risk_level": "low",
                "summary": f"巡检失败：{type(exc).__name__}: {exc}"[:200],
                "findings": [],
                "outcome": "error",
                "models": {},
            }
            return append_report(err_report)
        finally:
            with self._lock:
                self._running = False

    def _loop(self) -> None:
        # stagger startup
        self._stop.wait(15.0)
        while not self._stop.is_set():
            settings = _risk_settings()
            interval = float(settings["interval_sec"])
            if settings["enabled"]:
                try:
                    self.run_once(source="schedule")
                except Exception as exc:
                    logger.warning({"event": "risk_audit_loop_error", "error": str(exc)[:200]})
            # wait in slices for responsive stop
            waited = 0.0
            while waited < interval and not self._stop.is_set():
                step = min(5.0, interval - waited)
                self._stop.wait(step)
                waited += step
                # allow enable flip mid-wait without full interval when disabled
                if not _risk_settings()["enabled"] and settings["enabled"]:
                    break


risk_audit_service = RiskAuditService()
