#!/usr/bin/env python3
"""Assemble gptimage-deploy-artifacts overlay for schedule-core optimization."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".artifact-schedule-deploy"
RELEASE = "cf-eligibility-20260725"

PY_FILES = (
    "api/ai.py",
    "services/image_pipeline/schedule_trace.py",
    "services/image_pipeline/schedule_trace_model.py",
    "services/image_pipeline/schedule_core.py",
    "services/image_pipeline/slot_ledger.py",
    "services/image_pipeline/pipeline_watchdog.py",
    "services/image_pipeline/pre_ticket_pool.py",
    "services/image_pipeline/account_lease_pool.py",
    "services/image_pipeline/account_provider.py",
    "services/image_pipeline/orchestrator.py",
    "services/image_task_service.py",
    "services/image_sync_adapter.py",
    "services/account_service.py",
    "services/protocol/conversation.py",
    "services/openai_backend_api.py",
    "services/config.py",
    "services/storage/database_storage.py",
    "services/proxy_cf_eligibility.py",
    "services/proxy_cf_failover.py",
    "services/proxy_cf_probe.py",
    "services/proxy_quarantine.py",
    "api/system.py",
    "utils/process_memory.py",
)

SCRIPT_FILES = (
    "scripts/_tmp_stamp_accounts_cf_ok.py",
)

NATIVE_LIBS = (
    "libimage_schedule_trace.so",
    "libimage_schedule_core.so",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_linux_native() -> None:
    missing = [name for name in NATIVE_LIBS if not (ROOT / "native" / name).is_file()]
    if not missing:
        return
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_schedule_trace_linux.py"), "--target", "linux"],
        check=True,
    )
    still_missing = [name for name in NATIVE_LIBS if not (ROOT / "native" / name).is_file()]
    if still_missing:
        raise SystemExit(f"missing native libs after build: {still_missing}")


def main() -> int:
    ensure_linux_native()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    manifest: dict[str, str] = {}
    for rel in PY_FILES:
        src = ROOT / rel
        if not src.is_file():
            raise SystemExit(f"missing: {src}")
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        dst.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        manifest[rel.replace("\\", "/")] = sha256_file(dst)

    for name in NATIVE_LIBS:
        src = ROOT / "native" / name
        if not src.is_file():
            raise SystemExit(f"missing: {src}")
        rel = f"native/{name}"
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest[rel] = sha256_file(dst)

    for rel in SCRIPT_FILES:
        src = ROOT / rel
        if not src.is_file():
            raise SystemExit(f"missing: {src}")
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        dst.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        manifest[rel.replace("\\", "/")] = sha256_file(dst)

    deploy_sh = ROOT / "scripts" / "deploy_panda_schedule_core.sh"
    readme = ROOT / "scripts" / "artifact_README_schedule_core.md"
    for src, out_name in (
        (deploy_sh, "deploy_panda_schedule_core.sh"),
        (readme, "README.md"),
    ):
        if not src.is_file():
            raise SystemExit(f"missing: {src}")
        dst = OUT / out_name
        dst.write_text(src.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        manifest[out_name] = sha256_file(dst)

    (OUT / "deployment-manifest.json").write_text(
        json.dumps({"release": RELEASE, "files": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"artifact_dir": str(OUT), "release": RELEASE, "files": len(manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
