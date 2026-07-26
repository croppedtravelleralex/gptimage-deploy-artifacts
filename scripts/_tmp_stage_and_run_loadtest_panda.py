#!/usr/bin/env python3
"""Stage SPA bench/acceptance scripts into Panda RW mount and run inside container."""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REMOTE_HOST_DIR = "/root/gptimage/data/runlogs/spa_repro/staged"
REMOTE_SERVICE_ROOT = "/root/gptimage"
REMOTE_APP_DIR = "/app/data/runlogs/spa_repro/staged"
CONTAINER = "chatgpt2api-local"

STAGE_FILES = (
    "scripts/_tmp_spa_image_bench3.py",
    "scripts/spa_bench_sse.py",
    "scripts/spa_acceptance_gates.py",
    "scripts/spa_image_load_test.py",
    "scripts/spa_image_panda_acceptance.py",
    "scripts/_tmp_pipeline_conc10_acceptance.py",
    "scripts/_tmp_swap_and_probe_proxy.py",
    "scripts/_tmp_account_brief.py",
)

_PIPELINE_FILES = tuple(
    f"services/image_pipeline/{path.name}"
    for path in sorted((ROOT / "services" / "image_pipeline").glob("*.py"))
)

SERVICE_FILES = (
    "services/openai_backend_api.py",
    "services/image_task_service.py",
    "services/protocol/conversation.py",
    "services/protocol/openai_v1_image_generations.py",
    "services/protocol/openai_v1_image_edit.py",
    "services/account_service.py",
    "services/config.py",
    "api/image_tasks.py",
    "api/ops.py",
    *_PIPELINE_FILES,
)


def _stage_files() -> int:
    chunks: list[str] = [
        "import base64,json,pathlib\n",
        f"d=pathlib.Path({REMOTE_HOST_DIR!r})\n",
        "d.mkdir(parents=True, exist_ok=True)\n",
        "sizes={}\n",
    ]
    for rel in STAGE_FILES:
        src = ROOT / rel
        name = pathlib.Path(rel).name
        payload = base64.b64encode(src.read_bytes()).decode("ascii")
        chunks.append(f"(d/{name!r}).write_bytes(base64.b64decode({payload!r}))\n")
        chunks.append(f"sizes[{name!r}]=(d/{name!r}).stat().st_size\n")
    chunks.append(f"app_root=pathlib.Path({REMOTE_SERVICE_ROOT!r})\n")
    for rel in SERVICE_FILES:
        src = ROOT / rel
        payload = base64.b64encode(src.read_bytes()).decode("ascii")
        rel_path = rel.replace("\\", "/")
        chunks.append(f"t=app_root / {rel_path!r}\n")
        chunks.append("t.parent.mkdir(parents=True, exist_ok=True)\n")
        chunks.append(f"t.write_bytes(base64.b64decode({payload!r}))\n")
        chunks.append(f"sizes[{rel_path!r}]=t.stat().st_size\n")
    chunks.append("print(json.dumps({'ok':True,'sizes':sizes}))\n")
    install = "".join(chunks)
    up = subprocess.run(["ssh", "panda", "python3", "-"], input=install.encode("utf-8"), capture_output=True)
    sys.stdout.write(up.stdout.decode("utf-8", errors="replace"))
    if up.returncode != 0:
        sys.stderr.write(up.stderr.decode("utf-8", errors="replace")[:2000])
    return up.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        default="load",
        choices=["load", "cf_scan5", "serial5", "concurrent4", "full", "pipeline_conc10"],
    )
    ap.add_argument("--conc10-count", type=int, default=10)
    ap.add_argument("--email", default="")
    ap.add_argument("--account-hash", default="")
    ap.add_argument("--account-email", default="")
    ap.add_argument("--mode", default="serial", choices=["serial", "concurrent"])
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--only-round", type=int, default=0)
    ap.add_argument("--round-gap-secs", type=float, default=20.0)
    ap.add_argument("--emails", default="")
    ap.add_argument("--per-account", type=int, default=1)
    ap.add_argument("--cf-scan-pool", default="")
    ap.add_argument("--cf-scan-workers", type=int, default=5)
    ap.add_argument("--protocol", default="spa_tool", choices=["picture_v2", "spa_tool"])
    ap.add_argument("--image-gen-deadline", type=float, default=65.0)
    ap.add_argument("--sse-diagnostic-read-secs", type=float, default=90.0)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--require-serial5-evidence", default="")
    ap.add_argument("--stage-only", action="store_true")
    args = ap.parse_args()

    rc = _stage_files()
    if rc != 0:
        return rc
    if args.stage_only:
        return 0

    out_dir = args.out_dir or f"{REMOTE_APP_DIR}/out"
    if args.phase == "pipeline_conc10":
        inner = (
            f"/app/.venv/bin/python {REMOTE_APP_DIR}/_tmp_pipeline_conc10_acceptance.py "
            f"--base http://127.0.0.1:80 --count {args.conc10_count} --out-dir {out_dir}"
        )
    elif args.phase == "load":
        sel = f"--account-hash {args.account_hash}" if args.account_hash else f"--email {args.email}"
        if not args.account_hash and not args.email:
            raise SystemExit("--account-hash or --email required for load phase")
        count = f"--rounds {args.rounds}" if args.mode == "serial" else f"--per-account {args.per_account}"
        inner = (
            f"/app/.venv/bin/python {REMOTE_APP_DIR}/spa_image_load_test.py "
            f"--mode {args.mode} {sel} {count} --protocol {args.protocol} "
            f"--image-gen-deadline {args.image_gen_deadline} "
            f"--sse-diagnostic-read-secs {args.sse_diagnostic_read_secs} "
            f"--round-gap-secs {args.round_gap_secs} --out-dir {out_dir}"
        )
    else:
        inner = (
            f"/app/.venv/bin/python {REMOTE_APP_DIR}/spa_image_panda_acceptance.py "
            f"--phase {args.phase} --protocol {args.protocol} "
            f"--image-gen-deadline {args.image_gen_deadline} "
            f"--sse-diagnostic-read-secs {args.sse_diagnostic_read_secs} "
            f"--round-gap-secs {args.round_gap_secs} --rounds {args.rounds} "
            f"--out-dir {out_dir}"
        )
        if args.account_email:
            inner += f" --account-email {args.account_email}"
        elif args.account_hash:
            inner += f" --account-hash {args.account_hash}"
        if args.only_round:
            inner += f" --only-round {args.only_round}"
        if args.cf_scan_pool:
            inner += f" --cf-scan-pool {args.cf_scan_pool} --cf-scan-workers {args.cf_scan_workers}"
        if args.require_serial5_evidence:
            inner += f" --require-serial5-evidence {args.require_serial5_evidence}"
        if args.emails:
            inner += f" --concurrent-emails {args.emails}"

    run_cmd = (
        f"docker exec -w /app -e GPTIMAGE_ROOT=/app "
        f"-e SPA_BENCH_PATH={REMOTE_APP_DIR}/_tmp_spa_image_bench3.py {CONTAINER} bash -lc "
        f"'{inner} 2>&1; echo EXIT:$?'"
    )
    run = subprocess.run(["ssh", "panda", run_cmd])
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
