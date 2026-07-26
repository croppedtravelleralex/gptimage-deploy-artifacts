#!/usr/bin/env python3
"""Micro-benchmark schedule_trace emit/finish overhead (rust vs python fallback)."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.image_pipeline import schedule_trace

EVENTS = [
    "task_queued",
    "task_worker_start",
    "pipeline_admit",
    "account_wait_start",
    "account_acquired",
    "ready_buffer_wait_start",
    "ready_buffer_wait_end",
    "ss_queue_enter",
    "ss_slot_acquired",
    "sse_stream_end",
    "ss_slot_released",
    "poll_resolve_end",
    "download_start",
    "download_end",
    "pipeline_finish",
    "task_terminal",
]
ITERS = int(os.environ.get("BENCH_ITERS", "5000"))
RUNS = 7


def _bench_emit_finish(*, force_python: bool) -> dict:
    if force_python:
        os.environ["SCHEDULE_TRACE_FORCE_PYTHON"] = "1"
    else:
        os.environ.pop("SCHEDULE_TRACE_FORCE_PYTHON", None)
    # reload rust binding pick
    schedule_trace._RUST._lib = None  # type: ignore[attr-defined]
    schedule_trace._RUST._load()  # type: ignore[attr-defined]

    wall_samples: list[float] = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        for i in range(ITERS):
            run = schedule_trace.begin(f"bench-{i}", "bench@local.test")
            token = schedule_trace.bind(run)
            try:
                for kind in EVENTS:
                    aux = 0
                    if kind == "ss_queue_enter":
                        aux = schedule_trace.pack_pool_aux(active=3, queued=1)
                    if kind == "ss_slot_acquired":
                        aux = schedule_trace.pack_pool_aux(active=4, queued=0, slot=2)
                    run.emit(kind, aux)
                run.emit("task_terminal")
                payload = run.finish()
                assert payload.get("events") or payload.get("event_count")
            finally:
                schedule_trace.unbind(token)
                schedule_trace.pop(f"bench-{i}")
        wall_samples.append((time.perf_counter() - t0) * 1000.0)

    engine = schedule_trace.engine_info()
    per_task_us = (statistics.mean(wall_samples) / ITERS) * 1000.0
    per_event_us = per_task_us / max(1, len(EVENTS) + 1)
    return {
        "engine": engine.get("engine"),
        "lib_path": engine.get("lib_path"),
        "iters": ITERS,
        "runs": RUNS,
        "events_per_task": len(EVENTS) + 1,
        "wall_ms_mean": round(statistics.mean(wall_samples), 3),
        "wall_ms_p50": round(statistics.median(wall_samples), 3),
        "per_task_us_mean": round(per_task_us, 3),
        "per_event_us_mean": round(per_event_us, 3),
    }


def main() -> int:
    # python fallback: temporarily hide native lib
    orig = schedule_trace._RustLib._lib_path  # type: ignore[attr-defined]

    def _no_lib(_self: object) -> Path | None:
        return None

    schedule_trace._RustLib._lib_path = _no_lib  # type: ignore[assignment]
    py_stats = _bench_emit_finish(force_python=True)
    schedule_trace._RustLib._lib_path = orig  # type: ignore[assignment]
    schedule_trace._RUST._lib = None  # type: ignore[attr-defined]
    schedule_trace._RUST._load()  # type: ignore[attr-defined]

    rust_stats = _bench_emit_finish(force_python=False)
    ratio = 0.0
    if py_stats["per_task_us_mean"] > 0:
        ratio = round(rust_stats["per_task_us_mean"] / py_stats["per_task_us_mean"], 4)

    out = {
        "bench": "schedule_trace_emit_finish",
        "python_fallback": py_stats,
        "rust_engine": rust_stats,
        "rust_vs_python_task_us_ratio": ratio,
        "note": "per_task includes begin+16 emits+finish+pop; hot path is emit only",
    }
    out_dir = ROOT / "docs" / "captures" / "spa"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "schedule-trace-overhead-bench.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
