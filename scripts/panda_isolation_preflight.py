#!/usr/bin/env python3
"""Canary shared-binding isolation preflight for live window."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HASH = "40de2f332c0d3fd4"


def main() -> int:
    token_hash = str(os.environ.get("CANARY_TOKEN_HASH") or DEFAULT_HASH).strip()
    out = ROOT / "data" / "runlogs" / f"account-identity-remediation-canary-{token_hash}" / "isolation-preflight"
    script = ROOT / "scripts" / "panda_isolate_binding_peers.py"
    return subprocess.call(
        [sys.executable, str(script), "--token-hash", token_hash, "--out", str(out), "--preflight"],
        cwd=str(ROOT),
    )


if __name__ == "__main__":
    raise SystemExit(main())
