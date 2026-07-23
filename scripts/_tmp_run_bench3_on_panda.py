#!/usr/bin/env python3
"""Upload bench+secret to panda data mount and run inside chatgpt2api-local container."""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH = ROOT / "scripts" / "_tmp_spa_image_bench3.py"
SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
REMOTE_HOST_DIR = "/root/gptimage/data/runlogs/spa_repro/bench3"
REMOTE_APP_DIR = "/app/data/runlogs/spa_repro/bench3"
CONTAINER = "chatgpt2api-local"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["panda_direct", "panda_webshare"])
    args = ap.parse_args()
    if not SECRET.exists():
        print(json.dumps({"ok": False, "error": "missing_secret"}))
        return 2

    b64_bench = base64.b64encode(BENCH.read_bytes()).decode("ascii")
    b64_secret = base64.b64encode(SECRET.read_bytes()).decode("ascii")
    install = (
        "import base64,json,pathlib\n"
        f"d=pathlib.Path({REMOTE_HOST_DIR!r})\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        f"(d/'bench.py').write_bytes(base64.b64decode({b64_bench!r}))\n"
        f"(d/'secret.json').write_bytes(base64.b64decode({b64_secret!r}))\n"
        "print(json.dumps({'ok':True,'dir':str(d),'bench':(d/'bench.py').stat().st_size}))\n"
    )
    up = subprocess.run(["ssh", "panda", "python3", "-"], input=install.encode("utf-8"), capture_output=True)
    sys.stdout.write(up.stdout.decode("utf-8", errors="replace"))
    if up.returncode != 0:
        sys.stderr.write(up.stderr.decode("utf-8", errors="replace")[:1000])
        return up.returncode

    # container cwd /app; ROOT inside bench resolves parents[1] from script path under /app/data/...
    # Fix: bench uses Path(__file__).parents[1] which would be /app/data/runlogs — wrong.
    # So run with explicit PYTHONPATH=/app and patch by copying to /tmp inside container.
    run_cmd = (
        f"docker exec -w /app {CONTAINER} bash -lc "
        f"\"mkdir -p /tmp/spa_bench3 && "
        f"cp {REMOTE_APP_DIR}/bench.py /tmp/spa_bench3/bench.py && "
        f"cp {REMOTE_APP_DIR}/secret.json /tmp/spa_bench3/secret.json && "
        f"PYTHONPATH=/app python /tmp/spa_bench3/bench.py "
        f"--mode {args.mode} --secret /tmp/spa_bench3/secret.json "
        f"2>&1 | tee {REMOTE_APP_DIR}/{args.mode}.log; "
        f"echo EXIT:\\${{PIPESTATUS[0]}}; "
        f"ls -la {REMOTE_APP_DIR} | tail -20\""
    )
    # Wait - ROOT in bench is parents[1] of /tmp/spa_bench3/bench.py => /tmp — OUT_DIR broken.
    # Better: place bench at /app/scripts is RO. Place under /app/data and fix bench ROOT via env.

    print(json.dumps({"phase": "run", "mode": args.mode, "via": CONTAINER}, ensure_ascii=False), flush=True)
    # Use env GPTIMAGE_ROOT=/app so we should update bench — quicker patch: symlink trick
    run_cmd = (
        f"docker exec -w /app -e GPTIMAGE_ROOT=/app {CONTAINER} bash -lc "
        f"'cp {REMOTE_APP_DIR}/bench.py /tmp/bench3.py && "
        f"cp {REMOTE_APP_DIR}/secret.json /tmp/secret.json && "
        f"/app/.venv/bin/python /tmp/bench3.py --mode {args.mode} --secret /tmp/secret.json "
        f"2>&1 | tee {REMOTE_APP_DIR}/{args.mode}.log; "
        f"echo EXIT:${{PIPESTATUS[0]}}; ls -la {REMOTE_APP_DIR} | tail -25'"
    )
    run = subprocess.run(["ssh", "panda", run_cmd])
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
