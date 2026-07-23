#!/usr/bin/env python3
"""Pull panda bench3 JSON/PNG results into local bench3 dir."""
from __future__ import annotations

import pathlib
import subprocess

LOCAL = pathlib.Path(__file__).resolve().parents[1] / "data" / "runlogs" / "spa_repro" / "bench3"
REMOTE = "/root/gptimage/data/runlogs/spa_repro/bench3"
files = [
    "result_panda_direct_1784614959.json",
    "result_panda_webshare_1784614903.json",
    "panda_webshare_1784614903_0.png",
    "panda_direct.log",
    "panda_webshare.log",
]
LOCAL.mkdir(parents=True, exist_ok=True)
for name in files:
    out = LOCAL / name
    p = subprocess.run(
        ["ssh", "panda", f"cat {REMOTE}/{name}"],
        capture_output=True,
    )
    if p.returncode != 0:
        print("fail", name, p.stderr[:200])
        continue
    out.write_bytes(p.stdout)
    print("ok", name, out.stat().st_size)
