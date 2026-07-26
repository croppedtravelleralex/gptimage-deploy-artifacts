#!/usr/bin/env python3
"""Production-path validation: verify-quota-all → serial×5 → concurrent×3 (stop on error)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REMOTE = "panda"
GPTIMAGE = "/root/gptimage"
HELPER = "gptimage-gateway-rs-helper"
OUT_TAG = "sentinel-ticket-validation-20260723-production"
OUT_CTR = f"/app/data/runlogs/spa_repro/{OUT_TAG}"


def run(cmd: list[str], *, timeout: float = 7200) -> str:
    p = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if p.returncode != 0:
        raise RuntimeError(f"rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return (p.stdout or "").strip()


def scp(local: Path, dest: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{dest}"], timeout=180)


def upload(root: Path) -> None:
    for rel in (
        "services/openai_backend_api.py",
        "services/config.py",
        "services/proxy_cf_failover.py",
        "services/protocol/conversation.py",
    ):
        scp(root / rel, f"{GPTIMAGE}/{rel}")
    for name in (
        "sentinel_ticket_validation_suite.py",
        "_tmp_run_sentinel_validation.py",
        "_tmp_summarize_validation.py",
    ):
        scp(root / "scripts" / name, f"{GPTIMAGE}/scripts/{name}")


def docker_py(args: str, *, timeout: float = 7200) -> tuple[int, str]:
    cmd = (
        f"docker exec -e GPTIMAGE_ROOT=/app -w /app {HELPER} "
        f"/app/.venv/bin/python3 scripts/sentinel_ticket_validation_suite.py {args}"
    )
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=25", REMOTE, cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "phase",
        choices=[
            "verify-quota-all",
            "cross-serial",
            "cross-concurrent",
            "cross-concurrent-own-proxy",
            "production-run",
            "resume-production-run",
            "continue-load-test",
            "retry-bad-verify",
            "summarize",
        ],
    )
    ap.add_argument("--from-round", type=int, default=1)
    ap.add_argument("--to-round", type=int, default=4)
    ap.add_argument(
        "--retry-emails",
        default="qaflowakjewai6ps@proton.me,qaflowxho1z6hynk@proton.me",
        help="emails for retry-bad-verify",
    )
    ap.add_argument(
        "--skip-emails",
        default="qaflowakjewai6ps@proton.me",
        help="comma-separated emails to skip during verify-quota-all resume",
    )
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    print("[upload]")
    upload(root)
    common = f"--accounts-db /app/data/accounts.db --out-dir {OUT_CTR} --stop-on-error"

    if args.phase == "verify-quota-all":
        print("[verify-quota-all] every pooled account, production quota sync")
        rc, out = docker_py(f"{common} verify-quota-all --account-gap 8", timeout=14400)
        print(out)
        return rc

    if args.phase == "resume-production-run":
        skip = str(args.skip_emails or "").strip()
        skip_arg = f" --skip-emails {skip}" if skip else ""
        # finish remaining verify (ud630wbo2a), then load tests
        print("[verify-quota-resume] finish qaflowud630wbo2a")
        rc, out = docker_py(
            f"{common} verify-quota-all --account-gap 8 --resume "
            f"--only-emails qaflowud630wbo2a@proton.me{skip_arg}",
            timeout=3600,
        )
        print(out)
        if rc != 0:
            subprocess.call([sys.executable, __file__, "summarize"])
            return rc
        for step, extra in [
            ("cross-serial", ["--from-round", str(args.from_round)]),
            ("cross-concurrent", ["--from-round", str(args.from_round)]),
            ("summarize", []),
        ]:
            rc = subprocess.call([sys.executable, __file__, step, *extra])
            if rc != 0:
                subprocess.call([sys.executable, __file__, "summarize"])
                return rc
        return 0

    if args.phase == "cross-serial":
        for r in range(args.from_round, 6):
            print(f"[cross-serial] round {r}/5")
            rc, out = docker_py(f"{common} cross-serial --round {r}", timeout=2400)
            print(out)
            if rc != 0:
                print(f"STOP serial round {r}")
                return rc
        return 0

    if args.phase == "cross-concurrent" or args.phase == "cross-concurrent-own-proxy":
        end = max(int(args.from_round), int(args.to_round))
        own = args.phase == "cross-concurrent-own-proxy"
        extra = " --own-proxy-only --unique-egress" if own else ""
        for r in range(args.from_round, end + 1):
            tag = "own-proxy" if own else "mixed-proxy"
            print(f"[cross-concurrent/{tag}] round {r}/{end} workers=10")
            rc, out = docker_py(
                f"{common} cross-concurrent --round {r} --workers 10{extra}",
                timeout=3600,
            )
            print(out)
            if rc != 0:
                print(f"STOP concurrent round {r}")
                return rc
        return 0

    if args.phase == "retry-bad-verify":
        emails = str(getattr(args, "retry_emails", None) or "qaflowakjewai6ps@proton.me,qaflowxho1z6hynk@proton.me")
        print(f"[retry-bad-verify] alt proxy: {emails}")
        rc, out = docker_py(
            f"{common} retry-verify-alt-proxy --emails {emails} --account-gap 8",
            timeout=3600,
        )
        print(out)
        return rc

    if args.phase == "production-run":
        steps = [
            ("verify-quota-all", []),
            ("cross-serial", ["--from-round", str(args.from_round)]),
            ("cross-concurrent", ["--from-round", str(args.from_round)]),
            ("summarize", []),
        ]
        for step, extra in steps:
            rc = subprocess.call([sys.executable, __file__, step, *extra])
            if rc != 0:
                subprocess.call([sys.executable, __file__, "summarize"])
                return rc
        return 0

    if args.phase == "continue-load-test":
        for step, extra in [
            ("cross-serial", ["--from-round", str(args.from_round)]),
            ("cross-concurrent", ["--from-round", str(args.from_round)]),
            ("summarize", []),
        ]:
            rc = subprocess.call([sys.executable, __file__, step, *extra])
            if rc != 0:
                return rc
        return 0

    if args.phase == "summarize":
        run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=25",
                REMOTE,
                f"docker exec -e GPTIMAGE_ROOT=/app -w /app {HELPER} "
                f"/app/.venv/bin/python3 scripts/_tmp_summarize_validation.py "
                f"--out-dir {OUT_CTR} --write {OUT_CTR}/production_summary.json",
            ],
            timeout=120,
        )
        summary = run(
            ["ssh", "-o", "ConnectTimeout=25", REMOTE, f"cat {GPTIMAGE}/data/runlogs/spa_repro/{OUT_TAG}/production_summary.json"],
            timeout=60,
        )
        print(summary)
        local = root / "data" / "runlogs" / "spa_repro" / OUT_TAG
        local.mkdir(parents=True, exist_ok=True)
        (local / "production_summary.json").write_text(summary, encoding="utf-8")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
