"""L2 read-only ops tool facade + deterministic RCA playbooks.

Mutating actions are suggestions only (HITL), except explicit pause_register
when confirm=true.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from services.account_service import account_service
from services.config import config
from services.log_service import LOG_TYPE_LLM_OPS, log_llm_ops, log_service
from services.proxy_service import proxy_settings
from services.text_nurture_service import text_nurture_service
from utils.log import logger


ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


def _tool_health(_: dict[str, Any]) -> dict[str, Any]:
    stats = account_service.get_stats()
    runtime = account_service.get_image_candidate_runtime_stats()
    return {
        "accounts": {
            "total": stats.get("total"),
            "active": stats.get("active"),
            "schedulable": stats.get("schedulable"),
            **runtime,
        },
        "workload": {
            "mode": config.get_workload_settings().get("mode"),
            "text_queue_mode": config.get_workload_settings().get("text_queue_mode"),
        },
        "text_nurture": text_nurture_service.status(),
    }


def _tool_breakdown(_: dict[str, Any]) -> dict[str, Any]:
    return account_service.get_schedulable_breakdown()


def _tool_proxy_runtime(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime": config.get_public_proxy_runtime_settings(),
        "status": proxy_settings.get_runtime_status(),
    }


def _tool_llm_ops(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(200, int(args.get("limit") or 50)))
    source = str(args.get("source") or "").strip()
    outcome = str(args.get("outcome") or "").strip()
    items = log_service.list(type=LOG_TYPE_LLM_OPS, limit=limit * 3)
    filtered = []
    for item in items:
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        if source and str(detail.get("source") or "") != source:
            continue
        if outcome and str(detail.get("outcome") or "") != outcome:
            continue
        filtered.append(
            {
                "id": item.get("id"),
                "time": item.get("time"),
                "summary": item.get("summary"),
                "source": detail.get("source"),
                "kind": detail.get("kind"),
                "outcome": detail.get("outcome"),
                "outcome_code": detail.get("outcome_code"),
                "latency_ms": detail.get("latency_ms"),
                "account_hash": detail.get("account_hash"),
                "prompt_shape": detail.get("prompt_shape"),
            }
        )
        if len(filtered) >= limit:
            break
    return {"items": filtered, "count": len(filtered)}


def _tool_nurture_status(_: dict[str, Any]) -> dict[str, Any]:
    return text_nurture_service.status()


def _tool_probe_me(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only /me probe for one account by email (no raw token in response)."""
    email = str(args.get("email") or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    account = None
    for item in account_service.list_accounts():
        if str(item.get("email") or "").strip().lower() == email:
            account = item
            break
    if not account:
        raise ValueError("account not found")
    token = str(account.get("access_token") or "")
    from services.openai_backend_api import OpenAIBackendAPI

    backend = OpenAIBackendAPI(access_token=token)
    started = time.monotonic()
    try:
        me = backend._get_me()  # noqa: SLF001 — intentional read-only probe
        ok = isinstance(me, dict)
        return {
            "ok": ok,
            "email": email,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "plan_type": str((me or {}).get("plan_type") or "") if ok else "",
            "error": "" if ok else "me_failed",
        }
    except Exception as exc:
        return {
            "ok": False,
            "email": email,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


TOOLS: dict[str, dict[str, Any]] = {
    "get_health": {
        "description": "Account health + runtime candidate stats + nurture status",
        "mutate": False,
        "fn": _tool_health,
    },
    "get_schedulable_breakdown": {
        "description": "SCHED-001 excluded_by_* buckets",
        "mutate": False,
        "fn": _tool_breakdown,
    },
    "get_proxy_runtime": {
        "description": "Public proxy runtime settings and status",
        "mutate": False,
        "fn": _tool_proxy_runtime,
    },
    "list_llm_ops": {
        "description": "Recent llm_ops log events (optional source/outcome)",
        "mutate": False,
        "fn": _tool_llm_ops,
    },
    "get_text_nurture_status": {
        "description": "TEXT-NURTURE worker/queue status",
        "mutate": False,
        "fn": _tool_nurture_status,
    },
    "probe_account_me": {
        "description": "Read-only /me probe by email",
        "mutate": False,
        "fn": _tool_probe_me,
    },
}


def list_tools() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": meta["description"], "mutate": bool(meta["mutate"])}
        for name, meta in TOOLS.items()
    ]


def invoke_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = TOOLS.get(str(name or "").strip())
    if not meta:
        raise ValueError(f"unknown tool: {name}")
    started = time.monotonic()
    outcome = "ok"
    outcome_code = ""
    try:
        result = meta["fn"](dict(args or {}))
        return {"tool": name, "ok": True, "result": result}
    except Exception as exc:
        outcome = "error"
        outcome_code = type(exc).__name__
        raise
    finally:
        try:
            log_llm_ops(
                source="L2",
                kind="ops_rca",
                latency_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome,
                outcome_code=outcome_code or name,
                prompt_shape={"tool": name, "mutate": bool(meta.get("mutate"))},
            )
        except Exception:
            pass


def _narrative_empty_pool(health: dict[str, Any], breakdown: dict[str, Any]) -> str:
    buckets = breakdown.get("buckets") if isinstance(breakdown.get("buckets"), dict) else {}
    primary = breakdown.get("primary_reason_counts") if isinstance(breakdown.get("primary_reason_counts"), dict) else {}
    sched = int(buckets.get("schedulable") or 0)
    lines = [
        f"可调度账号 {sched} / 总 {breakdown.get('total')}.",
        f"主因分布: {primary}.",
    ]
    ranked = sorted(
        ((k, int(v or 0)) for k, v in buckets.items() if str(k).startswith("excluded_by_") and int(v or 0) > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )
    if ranked:
        top = ", ".join(f"{k}={v}" for k, v in ranked[:5])
        lines.append(f"排除桶 Top: {top}.")
    else:
        lines.append("当前无明显排除桶。")
    runtime = breakdown.get("runtime") if isinstance(breakdown.get("runtime"), dict) else {}
    lines.append(
        f"runtime: ready={runtime.get('ready_candidate_count')} "
        f"dispatchable={runtime.get('dispatchable_candidate_count')} "
        f"inflight={runtime.get('image_inflight_count')} "
        f"backoff={runtime.get('preflight_backoff_count')}."
    )
    return " ".join(lines)


def run_agent(query: str, *, max_tools: int = 4) -> dict[str, Any]:
    """Deterministic multi-step RCA; no autonomous mutate."""
    q = str(query or "").strip().lower()
    started = time.monotonic()
    steps: list[dict[str, Any]] = []
    plan: list[str] = []

    if any(k in q for k in ("空池", "调度", "quota", "schedul", "breakdown", "不可用", "候选")):
        plan = ["get_health", "get_schedulable_breakdown", "list_llm_ops"]
    elif any(k in q for k in ("代理", "proxy", "egress", "出口")):
        plan = ["get_proxy_runtime", "get_health"]
    elif any(k in q for k in ("养号", "nurture", "文本队列", "text")):
        plan = ["get_text_nurture_status", "list_llm_ops"]
    else:
        plan = ["get_health", "get_schedulable_breakdown"]

    plan = plan[: max(1, int(max_tools))]
    results: dict[str, Any] = {}
    for name in plan:
        args: dict[str, Any] = {}
        if name == "list_llm_ops":
            args = {"limit": 20}
        try:
            out = invoke_tool(name, args)
            steps.append(out)
            results[name] = out.get("result")
        except Exception as exc:
            steps.append({"tool": name, "ok": False, "error": str(exc)[:240]})

    summary = ""
    if "get_schedulable_breakdown" in results and "get_health" in results:
        summary = _narrative_empty_pool(results["get_health"], results["get_schedulable_breakdown"])
    elif "get_text_nurture_status" in results:
        st = results["get_text_nurture_status"] or {}
        summary = (
            f"养号 enabled={st.get('enabled')} queue={st.get('queue')} "
            f"completed_in_hour={st.get('completed_in_hour')}/{st.get('max_per_hour')} "
            f"last_error={st.get('last_error') or '-'}."
        )
    else:
        summary = f"已执行工具: {', '.join(plan)}."

    payload = {
        "query": query,
        "plan": plan,
        "steps": steps,
        "summary": summary,
        "suggestions": [
            "调度进出 / OTP 恢复 / 清失败证据：人工 HITL",
            "pause_register 需 confirm=true",
            "养号须账号 chat_persist_history=true，禁止机械假聊",
        ],
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
    try:
        log_llm_ops(
            source="L2",
            kind="ops_rca",
            latency_ms=payload["latency_ms"],
            outcome="ok",
            prompt_shape={"chars": len(query or ""), "tools": len(plan)},
            summary=f"llm_ops L2/ops_rca {summary[:80]}",
        )
    except Exception:
        logger.debug("llm_ops L2 log failed", exc_info=True)
    return payload
