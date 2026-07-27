#!/usr/bin/env python3
"""Run N concurrent image gens and capture queue/slot topology before & after."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "captures" / "spa"
REMOTE = "panda"


def remote(cmd: str, timeout: float = 60) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return (proc.stdout or "").strip()


def health_snapshot() -> dict:
    raw = remote(
        'curl -fsS "http://127.0.0.1:8012/health?format=json"',
        timeout=30,
    )
    data = json.loads(raw)
    return {
        "slot_topology": data.get("slot_topology") or {},
        "workload": data.get("workload") or {},
        "bandwidth": data.get("bandwidth") or {},
        "accounts": {
            k: data.get("accounts", {}).get(k)
            for k in ("image_schedulable", "dispatchable_candidate_count", "image_inflight_count")
        },
        "pipeline_watchdog": {
            k: (data.get("pipeline_watchdog") or {}).get(k)
            for k in ("ss_active", "ss_queued", "ss_limit", "pipeline_in_flight")
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=15)
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    before = health_snapshot()
    print(json.dumps({"phase": "before", **before}, ensure_ascii=False, indent=2), flush=True)

    runner = ROOT / "scripts" / "_tmp_run_conc10_phases.py"
    proc = subprocess.run(
        [sys.executable, str(runner), "--count", str(args.count)],
        cwd=str(ROOT),
        timeout=900,
    )
    after = health_snapshot()

    report_path = OUT_DIR / f"PROD-overflow-conc{args.count}-{stamp}.json"
    report = {
        "stamp": stamp,
        "count": args.count,
        "exit_code": proc.returncode,
        "health_before": before,
        "health_after": after,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "exit_code": proc.returncode}, ensure_ascii=False))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
