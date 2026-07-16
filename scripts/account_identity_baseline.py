#!/usr/bin/env python3
"""P0 基线：记录本轮允许修改的文件 hash、git 状态与证据目录。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    "services/account_service.py",
    "services/account_identity.py",
    "services/account_fingerprint.py",
    "services/account_refresh_all_service.py",
    "services/openai_backend_api.py",
    "services/outlook_auto_recovery_loop_service.py",
    "services/account_workload_policy.py",
    "web/src/app/accounts/page.tsx",
    "web/src/lib/api.ts",
    "scripts/repair_panda_account_identity.py",
    "scripts/build_static_frontend.ps1",
    "plan.md",
]


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = ROOT / "data" / "runlogs" / f"account-identity-remediation-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for rel in TARGETS:
        path = ROOT / rel
        files[rel] = {
            "exists": path.exists(),
            "sha12": sha12(path) if path.exists() else "",
            "bytes": path.stat().st_size if path.exists() else 0,
        }
    try:
        status = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        status = f"git_status_failed:{exc}"
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
    except Exception:
        commit = ""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "files": files,
        "git_status_short": status.splitlines()[:200],
        "out_dir": str(out),
    }
    (out / "baseline.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    operation = {
        "operator": "local-agent",
        "time": payload["generated_at"],
        "layer": "P0-baseline",
        "git_commit": commit,
        "evidence_dir": str(out),
        "rollback": "n/a (baseline only)",
        "verification": "baseline.json written",
    }
    (out / "operation.json").write_text(json.dumps(operation, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
