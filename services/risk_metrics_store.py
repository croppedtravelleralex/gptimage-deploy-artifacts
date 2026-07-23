#!/usr/bin/env python3
"""Persist half-hour risk metrics points and risk-check reports (jsonl)."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.config import DATA_DIR

METRICS_PATH = DATA_DIR / "risk_metrics.jsonl"
REPORTS_PATH = DATA_DIR / "risk_check_reports.jsonl"
_LOCK = threading.Lock()
_MAX_METRICS_LINES = 8000
_MAX_REPORT_LINES = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, row: dict[str, Any], *, max_lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if len(raw) > max_lines:
                path.write_text("\n".join(raw[-max_lines:]) + "\n", encoding="utf-8")
        except OSError:
            pass


def append_metrics_point(point: dict[str, Any]) -> dict[str, Any]:
    row = dict(point)
    row.setdefault("ts", _utc_now())
    row.setdefault("t", time.time())
    _append(METRICS_PATH, row, max_lines=_MAX_METRICS_LINES)
    return row


def append_report(report: dict[str, Any]) -> dict[str, Any]:
    row = dict(report)
    row.setdefault("id", uuid4().hex[:16])
    row.setdefault("finished_at", _utc_now())
    _append(REPORTS_PATH, row, max_lines=_MAX_REPORT_LINES)
    return row


def _read_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-max(1, limit) :]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def list_metrics(*, limit: int = 336) -> list[dict[str, Any]]:
    """Default ~7d of half-hour points."""
    return _read_tail(METRICS_PATH, limit=max(1, min(2000, int(limit))))


def list_reports(*, limit: int = 48) -> list[dict[str, Any]]:
    rows = _read_tail(REPORTS_PATH, limit=max(1, min(200, int(limit))))
    rows.reverse()  # newest first
    return rows
