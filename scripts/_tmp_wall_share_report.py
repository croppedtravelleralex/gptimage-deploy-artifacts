#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

PHASES = [
    ("task_queue_ms", "任务排队"),
    ("admit_queue_ms", "准入排队"),
    ("upload_queue_ms", "上传排队"),
    ("ps_queue_ms", "pS排队"),
    ("account_queue_ms", "取号"),
    ("ss_queue_ms", "sS排队"),
    ("download_queue_ms", "下载排队"),
    ("upload_ms", "上传"),
    ("ps_ms", "pS执行"),
    ("ss_ms", "sS总段"),
    ("sse_stream_ms", "开票+SSE"),
    ("poll_resolve_ms", "轮询收图"),
    ("download_ms", "下载"),
]


def report(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("phase_rows") or []
    walls = [float(r.get("wall_clock_ms") or 0) for r in rows if float(r.get("wall_clock_ms") or 0) > 0]
    wall_mean = sum(walls) / len(walls) if walls else 0.0
    shares = []
    for key, label in PHASES:
        mean = sum(float(r.get(key) or 0) for r in rows) / len(rows) if rows else 0.0
        pct = round(100.0 * mean / wall_mean, 2) if wall_mean else 0.0
        shares.append({"key": key, "label": label, "mean_ms": round(mean, 1), "pct": pct})
    return {
        "tag": data.get("prompt_tag"),
        "wall_mean_ms": round(wall_mean, 1),
        "shares": shares,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "captures" / "spa"
    paths = [
        root / "PROD-serial10-20260724T165739Z.json",
        root / "PROD-conc10-20260725T005309Z.json",
    ]
    for path in paths:
        r = report(path)
        print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
