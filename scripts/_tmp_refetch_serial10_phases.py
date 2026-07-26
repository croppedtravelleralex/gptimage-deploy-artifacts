#!/usr/bin/env python3
"""Re-fetch phase rows for an existing serial10 JSON (fix call-log matching)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts._tmp_run_serial10_phases import (  # noqa: E402
    PHASE_KEYS,
    PROMPT_TAG,
    _fetch_phase_rows,
    _md_report,
    _summarize_phases,
)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/captures/spa/PROD-serial10-20260724T165739Z.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    tag = str(payload.get("prompt_tag") or "")
    rows = _fetch_phase_rows(tag)
    payload["phase_rows"] = rows
    payload["phase_summary"] = _summarize_phases(rows)
    trace_engines = sorted({str(r.get("trace_engine") or "") for r in rows if r.get("trace_engine")})
    trace_events = [int(r.get("trace_events") or 0) for r in rows if int(r.get("trace_events") or 0) > 0]
    payload["trace_engine"] = trace_engines[0] if len(trace_engines) == 1 else trace_engines
    payload["trace_event_count_mean"] = round(sum(trace_events) / len(trace_events), 1) if trace_events else None
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = path.with_suffix(".md")
    md_path.write_text(_md_report(payload), encoding="utf-8")
    print(json.dumps({"phase_n": len(rows), "trace_engine": payload.get("trace_engine"), "json": str(path)}, indent=2))
    print(_md_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
