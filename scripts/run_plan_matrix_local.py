#!/usr/bin/env python3
"""Run local-executable plan.md P8 matrix slices and write matrix-results.json."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DEFAULT = ROOT / "data" / "runlogs" / "account-identity-remediation-matrix-local"


SUITES = [
    {
        "id": "identity_gate",
        "section": "11.2/11.3",
        "cmd": [
            sys.executable, "-m", "pytest", "-q",
            "test/test_account_identity_persistence.py",
            "test/test_account_fingerprint_and_proxy_pick.py",
            "test/test_repair_panda_account_identity.py",
        ],
    },
    {
        "id": "poll_budget",
        "section": "11.4/11.7",
        "cmd": [
            sys.executable, "-m", "pytest", "-q",
            "test/test_image_poll_budget.py",
            "test/test_multi_image_results.py",
            "test/test_image_pre_conversation_timeout.py",
            "test/test_request_shape.py",
        ],
    },
    {
        "id": "proxy_runtime",
        "section": "11.2",
        "cmd": [
            sys.executable, "-m", "pytest", "-q",
            "test/test_account_service_proxy_runtime.py",
            "test/test_openai_backend_api_proxy_runtime.py",
            "test/test_proxy_service.py",
        ],
    },
]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT
    out.mkdir(parents=True, exist_ok=True)
    results = []
    overall_ok = True
    for suite in SUITES:
        started = time.time()
        proc = subprocess.run(suite["cmd"], cwd=ROOT, capture_output=True, text=True)
        elapsed = round(time.time() - started, 2)
        ok = proc.returncode == 0
        overall_ok = overall_ok and ok
        results.append({
            "id": suite["id"],
            "section": suite["section"],
            "ok": ok,
            "exit_code": proc.returncode,
            "elapsed_secs": elapsed,
            "cmd": suite["cmd"],
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-1000:],
        })
        print(f"{suite['id']}: {'PASS' if ok else 'FAIL'} ({elapsed}s)")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_mock_fixture",
        "note": "Production critical combos still require P6/P7 canary evidence.",
        "overall_ok": overall_ok,
        "suites": results,
    }
    path = out / "matrix-results.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", path)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
