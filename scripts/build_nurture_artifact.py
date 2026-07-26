#!/usr/bin/env python3
"""Assemble gptimage-deploy-artifacts overlay for nurture/heatmap release."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".artifact-nurture-deploy"

FILES = (
    "services/ip_nurture_schedule.py",
    "services/text_nurture_service.py",
    "services/config.py",
    "services/account_service.py",
    "services/risk_dashboard_service.py",
    "services/proxy_cf_probe.py",
    "services/webshare_cf_scan_service.py",
    "services/proxy_quarantine.py",
    "services/proxy_cf_failover.py",
    "api/ops.py",
    "api/app.py",
    "api/support.py",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    manifest: dict[str, str] = {}
    for rel in FILES:
        src = ROOT / rel
        if not src.is_file():
            raise SystemExit(f"missing: {src}")
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        dst.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        manifest[rel.replace("\\", "/")] = sha256_file(dst)

    web_dist = ROOT / "web_dist"
    if not (web_dist / "index.html").is_file():
        raise SystemExit("web_dist missing; run scripts/build_static_frontend.ps1 first")
    tgz = OUT / "web_dist.tgz"
    with tarfile.open(tgz, "w:gz") as tar:
        tar.add(web_dist, arcname="web_dist")
    manifest["web_dist.tgz"] = sha256_file(tgz)

    deploy_sh = ROOT / "scripts" / "deploy_panda_nurture.sh"
    readme = ROOT / "scripts" / "artifact_README_nurture.md"
    for src, name in ((deploy_sh, "deploy_panda_nurture.sh"), (readme, "README.md")):
        dst = OUT / name
        dst.write_text(src.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        manifest[name] = sha256_file(dst)

    (OUT / "deployment-manifest.json").write_text(
        json.dumps({"release": "nurture-webshare-cf-scan-20260723", "files": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"artifact_dir": str(OUT), "files": len(manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
