"""Acceptance gate helpers for PROTO-PURE-HTTP Panda serial5 / concurrent4."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _summary_block(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("summary"), dict):
        return data["summary"]
    return data


def serial5_passed(data: dict[str, Any]) -> bool:
    summary = _summary_block(data)
    planned = int(summary.get("planned") or summary.get("requested") or summary.get("total") or 0)
    if planned < 5:
        planned = 5
    ok = int(summary.get("ok") or summary.get("ok_count") or 0)
    no_image_gen = int(summary.get("no_image_gen") or 0)
    cf403 = int(summary.get("cf403") or summary.get("cf403_propagated") or summary.get("cf_abort") or 0)
    stopped = bool(summary.get("stopped_early"))
    explicit = summary.get("serial5_passed")
    if explicit is True:
        return True
    return (
        ok >= planned
        and no_image_gen == 0
        and cf403 == 0
        and not stopped
        and int(summary.get("attempted") or ok) >= planned
    )


def concurrent4_allowed(serial5_path: Path | str) -> tuple[bool, str]:
    path = Path(serial5_path)
    if not path.is_file():
        return False, f"missing_serial5_evidence:{path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    if serial5_passed(data):
        return True, "ok"
    return False, "serial5_not_passed"


def should_stop_serial5(rows: list[dict[str, Any]], *, policy: str = "strict") -> tuple[bool, str]:
    if not rows:
        return False, ""
    last = rows[-1]
    if not last.get("ok"):
        err = str(last.get("error") or "").lower()
        failure_class = str(last.get("failure_class") or "").lower()
        cf_layers = last.get("cf_layers") or last.get("cf_observability") or {}
        propagated = int(cf_layers.get("propagated_cf") or 0) > 0
        cf_class = str(last.get("cf_classification") or "").lower()
        if propagated or cf_class == "cf403" or "cf_abort" in err or "cloudflare" in err:
            if policy == "strict":
                # consecutive CF signal check
                if len(rows) >= 2:
                    prev = rows[-2]
                    prev_cf = str(prev.get("cf_classification") or "").lower()
                    prev_layers = prev.get("cf_layers") or prev.get("cf_observability") or {}
                    prev_prop = int(prev_layers.get("propagated_cf") or 0) > 0
                    if prev_prop or prev_cf == "cf403":
                        return True, "two_consecutive_rounds_with_cf_signal"
                return True, "cf_signal"
        if "no_image_gen" in err or failure_class.startswith("no_image_gen") or failure_class == "tool_args_as_text":
            return True, f"round{len(rows)}_no_image_gen"
        if failure_class == "late_image_gen_after_gate":
            return True, f"round{len(rows)}_late_image_gen_after_gate"
    return False, ""


def summarize_failure_classes(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "tool_args_as_text": 0,
        "late_image_gen_after_gate": 0,
        "no_image_gen_quiet_stream": 0,
        "no_image_gen_within_gate": 0,
    }
    for row in rows:
        fc = str(row.get("failure_class") or "").strip()
        if fc in counts:
            counts[fc] += 1
        elif not row.get("ok") and "no_image_gen_within" in str(row.get("error") or "").lower():
            counts["no_image_gen_within_gate"] += 1
    return counts


def summarize_cf_layers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    home_soft = 0
    req_cf = 0
    start_cf = 0
    tasks_cf = 0
    for row in rows:
        layers = row.get("cf_layers") or row.get("cf_observability") or {}
        if layers.get("home_403_soft_fail"):
            home_soft += 1
        req_cf += int(layers.get("requirements_cf403") or 0)
        start_cf += int(layers.get("start_cf403") or 0)
        tasks_cf += int(layers.get("tasks_cf403") or 0)
    return {
        "home_403_soft_fail": home_soft,
        "requirements_cf403": req_cf,
        "start_cf403": start_cf,
        "tasks_cf403": tasks_cf,
        "propagated_cf": int(req_cf > 0 or start_cf > 0 or tasks_cf > 0),
    }
