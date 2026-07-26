#!/usr/bin/env python3
"""Deploy one sentinel ablation batch to Panda (~2-8 min each). Merge across batches.

Usage:
  python scripts/_tmp_deploy_sentinel_ablation.py batch1
  python scripts/_tmp_deploy_sentinel_ablation.py batch2 --merge
  python scripts/_tmp_deploy_sentinel_ablation.py batch4-60 --merge
  python scripts/_tmp_deploy_sentinel_ablation.py --list
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REMOTE = "panda"
GPTIMAGE = "/root/gptimage"
EMAIL = "qaflowakjewai6ps@proton.me"
OUT_TAG = "sentinel-ticket-ablation-20260723"
HELPER = "gptimage-gateway-rs-helper"

BATCHES = [
    "batch1",       # baseline + reuse + cross_session (~5 min)
    "batch2",       # cross_ip + cross_both (~5 min)
    "batch3",       # delay 30s (~2 min)
    "batch4-60",    # delay 60s (~2 min)
    "batch4-120",   # delay 120s (~3 min)
    "batch4-300",   # delay 300s (~6 min)
    "batch5",       # concurrent x2 (~5 min)
]


def run(cmd: list[str], *, timeout: float = 900) -> str:
    p = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if p.returncode != 0:
        raise RuntimeError(f"rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return (p.stdout or "").strip()


def remote(cmd: str, *, timeout: float = 900) -> str:
    return run(["ssh", "-o", "ConnectTimeout=25", REMOTE, cmd], timeout=timeout)


def scp(local: Path, dest: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{dest}"], timeout=180)


def ensure_secret(root: Path) -> None:
    secret_path = f"{GPTIMAGE}/data/runlogs/spa_repro/qaflow_secret.json"
    export_py = root / "scripts" / "prototype" / "openai_ticket_billing" / "v20260723" / "_tmp_export_spa_secret.py"
    scp(export_py, f"{GPTIMAGE}/scripts/_tmp_export_spa_secret_ablation.py")
    remote(
        f"python3 {GPTIMAGE}/scripts/_tmp_export_spa_secret_ablation.py "
        f"{EMAIL} {secret_path}"
    )


def run_batch(batch: str, *, merge: bool) -> str:
    out_in_container = f"/app/data/runlogs/spa_repro/{OUT_TAG}"
    secret_in_container = "/app/data/runlogs/spa_repro/qaflow_secret.json"
    merge_flag = "--merge" if merge else ""
    cmd = (
        f"docker exec -e GPTIMAGE_ROOT=/app -w /app {HELPER} "
        f"/app/.venv/bin/python3 scripts/_tmp_sentinel_ticket_ablation.py "
        f"--secret {secret_in_container} "
        f"--out-dir {out_in_container} "
        f"--accounts-db /app/data/accounts.db "
        f"--batch {batch} {merge_flag}"
    )
    return remote(cmd, timeout=900)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch", nargs="?", default="", help="batch1|batch2|...")
    ap.add_argument("--merge", action="store_true", help="merge into existing report")
    ap.add_argument("--list", action="store_true", help="list batches")
    ap.add_argument("--fetch", action="store_true", help="fetch report to docs/captures")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]

    if args.list:
        print("Batches (run separately, use --merge from batch2 onward):")
        for b in BATCHES:
            print(f"  {b}")
        return 0

    batch = str(args.batch or "").strip()
    if not batch:
        ap.print_help()
        return 2
    if batch not in BATCHES:
        raise SystemExit(f"unknown batch {batch!r}; --list for options")

    print(f"[1/3] upload + secret ({batch})")
    scp(root / "scripts" / "_tmp_sentinel_ticket_ablation.py", f"{GPTIMAGE}/scripts/_tmp_sentinel_ticket_ablation.py")
    ensure_secret(root)

    print(f"[2/3] run {batch} (probe mode, target <10 min)")
    out = run_batch(batch, merge=bool(args.merge))
    print(out)

    if args.fetch or batch == BATCHES[-1]:
        print("[3/3] fetch report")
        host_out = f"{GPTIMAGE}/data/runlogs/spa_repro/{OUT_TAG}/ablation_report.json"
        summary = remote(f"cat {host_out}")
        local = root / "docs" / "captures" / "spa" / f"P-{OUT_TAG}.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(summary, encoding="utf-8")
        print(f"saved {local}")
    else:
        print("[3/3] skip fetch (use --fetch on last batch)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise
