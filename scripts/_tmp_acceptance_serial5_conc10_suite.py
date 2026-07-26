#!/usr/bin/env python3
"""Serial5 (round-by-round, stop on fail) then 3x pipeline conc10; aggregate full metrics."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STAGE_RUNNER = ROOT / "scripts" / "_tmp_stage_and_run_loadtest_panda.py"
REMOTE_HOST_ROOT = "/root/gptimage/data/runlogs/spa_repro"
CONTAINER = "chatgpt2api-local"
REMOTE_APP_DIR = "/app/data/runlogs/spa_repro/staged"
DEFAULT_EMAIL = "qaflowakjewai6ps@proton.me"


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None, "p99": None, "sum": None}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(statistics.mean(values), 3),
        "p50": _pct(values, 50),
        "p95": _pct(values, 95),
        "p99": _pct(values, 99),
        "sum": round(sum(values), 3),
    }


def _ssh(cmd: str, *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "panda", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _docker_run(inner: str, *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    quoted = inner.replace("'", "'\"'\"'")
    cmd = (
        f"docker exec -w /app -e GPTIMAGE_ROOT=/app "
        f"-e SPA_BENCH_PATH={REMOTE_APP_DIR}/_tmp_spa_image_bench3.py {CONTAINER} "
        f"bash -lc '{quoted}; echo EXIT:$?'"
    )
    return _ssh(cmd, timeout=timeout)


def _stage() -> int:
    proc = subprocess.run([sys.executable, str(STAGE_RUNNER), "--stage-only"], cwd=str(ROOT))
    return proc.returncode


def _run_serial_round(
    *,
    out_dir: str,
    email: str,
    round_no: int,
    rounds: int,
    round_gap: float,
    image_deadline: float,
    sse_read: float,
    protocol: str,
) -> tuple[int, str]:
    inner = (
        f"/app/.venv/bin/python {REMOTE_APP_DIR}/spa_image_panda_acceptance.py "
        f"--phase serial5 --account-email {email} --only-round {round_no} --rounds {rounds} "
        f"--round-gap-secs {round_gap} --image-gen-deadline {image_deadline} "
        f"--sse-diagnostic-read-secs {sse_read} --protocol {protocol} --out-dir {out_dir}"
    )
    proc = _docker_run(inner, timeout=900)
    out = (proc.stdout or "") + (proc.stderr or "")
    exit_code = 1
    for line in reversed(out.strip().splitlines()):
        if line.startswith("EXIT:"):
            try:
                exit_code = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            break
    return exit_code, out


def _run_conc10(*, out_dir: str, count: int) -> tuple[int, str]:
    inner = (
        f"/app/.venv/bin/python {REMOTE_APP_DIR}/_tmp_pipeline_conc10_acceptance.py "
        f"--base http://127.0.0.1:80 --count {count} --out-dir {out_dir}"
    )
    proc = _docker_run(inner, timeout=1200)
    out = (proc.stdout or "") + (proc.stderr or "")
    exit_code = 1
    for line in reversed(out.strip().splitlines()):
        if line.startswith("EXIT:"):
            try:
                exit_code = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            break
    return exit_code, out


def _remote_read(path: str) -> Any:
    proc = _ssh(f"cat {path}")
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout


def _collect_serial5(host_out: str, rounds: int) -> dict[str, Any]:
    serial_path = f"{host_out}/serial5.json"
    serial = _remote_read(serial_path) or {}
    round_files: list[dict[str, Any]] = []
    for i in range(1, rounds + 1):
        row = _remote_read(f"{host_out}/round-{i}-canary.json")
        if isinstance(row, dict):
            round_files.append(row)

    total_ms = [float((r.get("timings_ms") or {}).get("total_ms") or 0) for r in round_files if r.get("ok")]
    req_bytes = [float((r.get("traffic") or {}).get("req_bytes") or 0) for r in round_files]
    resp_bytes = [float((r.get("traffic") or {}).get("resp_bytes") or 0) for r in round_files]
    total_bytes = [float((r.get("traffic") or {}).get("total_bytes") or 0) for r in round_files]
    sse_bytes = [float((r.get("traffic") or {}).get("sse_bytes") or (r.get("sse") or {}).get("bytes") or 0) for r in round_files]

    phase_keys: set[str] = set()
    for r in round_files:
        phase_keys.update((r.get("timings_ms") or {}).keys())
    phase_stats = {k: _stats([float((r.get("timings_ms") or {}).get(k) or 0) for r in round_files]) for k in sorted(phase_keys)}

    per_round = []
    for i, r in enumerate(round_files, start=1):
        per_round.append(
            {
                "round": i,
                "ok": bool(r.get("ok")),
                "failure_class": r.get("failure_class"),
                "error": (r.get("error") or "")[:300],
                "total_ms": (r.get("timings_ms") or {}).get("total_ms"),
                "timings_ms": r.get("timings_ms"),
                "traffic": r.get("traffic"),
                "cf_layers": r.get("cf_layers") or r.get("cf_observability"),
                "image_bytes": ((r.get("image") or {}).get("bytes") if isinstance(r.get("image"), dict) else None),
            }
        )

    return {
        "evidence_path": serial_path,
        "summary": serial.get("summary") or {},
        "identity": serial.get("identity") or {},
        "constraints": serial.get("constraints") or {},
        "per_round": per_round,
        "timing_total_ms": _stats(total_ms),
        "timing_phases_ms": phase_stats,
        "traffic_req_bytes": _stats(req_bytes),
        "traffic_resp_bytes": _stats(resp_bytes),
        "traffic_total_bytes": _stats(total_bytes),
        "traffic_sse_bytes": _stats(sse_bytes),
    }


def _collect_conc10(host_out: str) -> dict[str, Any]:
    proc = _ssh(f"ls -1 {host_out}/*.json 2>/dev/null | tail -1")
    report_path = (proc.stdout or "").strip()
    report = _remote_read(report_path) if report_path else None
    if not isinstance(report, dict):
        return {"error": "missing_report", "path": report_path}

    final_items = report.get("final_items") or []
    wall_clocks: list[float] = []
    ss_queue: list[float] = []
    ps_queue: list[float] = []
    submit_ms: list[float] = []
    phase_buckets: dict[str, list[float]] = {}

    for item in final_items:
        if item.get("wall_clock_ms") is not None:
            wall_clocks.append(float(item["wall_clock_ms"]))
        timings = item.get("phase_timings_ms") if isinstance(item.get("phase_timings_ms"), dict) else {}
        if isinstance(timings, dict):
            for k, v in timings.items():
                if v is None:
                    continue
                phase_buckets.setdefault(str(k), []).append(float(v))
            if timings.get("ss_queue_ms") is not None:
                ss_queue.append(float(timings["ss_queue_ms"]))
            if timings.get("ps_queue_ms") is not None:
                ps_queue.append(float(timings["ps_queue_ms"]))

    for s in report.get("submits") or []:
        if s.get("submit_ms") is not None:
            submit_ms.append(float(s["submit_ms"]))

    poll_times = [float(p.get("t_secs") or 0) for p in (report.get("poll_log") or [])]

    return {
        "report_path": report_path,
        "run_id": report.get("run_id"),
        "summary": {
            "count": report.get("count"),
            "submitted": report.get("submitted"),
            "submit_ok": report.get("submit_ok"),
            "wall_clock_ms": report.get("wall_clock_ms"),
            "final_status_counts": report.get("final_status_counts"),
        },
        "wall_clock_ms": _stats([float(report.get("wall_clock_ms") or 0)]),
        "task_wall_clock_ms": _stats(wall_clocks),
        "ss_queue_ms": _stats(ss_queue),
        "ps_queue_ms": _stats(ps_queue),
        "submit_ms": _stats(submit_ms),
        "phase_timings_ms": {k: _stats(v) for k, v in sorted(phase_buckets.items())},
        "poll_duration_secs": _stats(poll_times),
        "pipeline_snapshot_before": report.get("pipeline_snapshot_before"),
        "pipeline_snapshot_after": report.get("pipeline_snapshot_after"),
        "final_items": final_items,
        "submits": report.get("submits"),
        "poll_log": report.get("poll_log"),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Acceptance suite — {report.get('suite_id')}",
        "",
        f"- started: {report.get('started_at')}",
        f"- finished: {report.get('finished_at')}",
        f"- total_wall_secs: {report.get('total_wall_secs')}",
        f"- account: {report.get('account_email')}",
        f"- outcome: **{report.get('outcome')}**",
        "",
        "## Serial5",
        "",
    ]
    s5 = report.get("serial5") or {}
    lines.append(f"- passed: {((s5.get('summary') or {}).get('serial5_passed'))}")
    lines.append(f"- rounds attempted: {len(s5.get('per_round') or [])}")
    tt = s5.get("timing_total_ms") or {}
    lines.append(f"- total_ms: p50={tt.get('p50')} p95={tt.get('p95')} p99={tt.get('p99')} max={tt.get('max')}")
    tr = s5.get("traffic_total_bytes") or {}
    lines.append(f"- traffic total_bytes: sum={tr.get('sum')} p50={tr.get('p50')} max={tr.get('max')}")
    lines.append("")
    lines.append("| round | ok | total_ms | req_B | resp_B | total_B | failure |")
    lines.append("|------:|----|---------:|------:|-------:|--------:|---------|")
    for r in s5.get("per_round") or []:
        t = r.get("traffic") or {}
        lines.append(
            f"| {r.get('round')} | {r.get('ok')} | {r.get('total_ms')} | {t.get('req_bytes')} | {t.get('resp_bytes')} | {t.get('total_bytes')} | {r.get('failure_class') or r.get('error','')[:40]} |"
        )
    lines.append("")
    conc = report.get("conc10_rounds") or []
    if conc:
        lines.append("## Pipeline conc10")
        lines.append("")
        for i, c in enumerate(conc, start=1):
            sm = c.get("summary") or {}
            tw = c.get("task_wall_clock_ms") or {}
            sq = c.get("ss_queue_ms") or {}
            lines.append(f"### Round {i} — {c.get('run_id')}")
            lines.append(f"- wall_clock_ms: {sm.get('wall_clock_ms')}")
            lines.append(f"- final_status: {sm.get('final_status_counts')}")
            lines.append(f"- task_wall p50/p95/p99: {tw.get('p50')}/{tw.get('p95')}/{tw.get('p99')}")
            lines.append(f"- ss_queue p50/p95/p99: {sq.get('p50')}/{sq.get('p95')}/{sq.get('p99')}")
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--serial-rounds", type=int, default=5)
    ap.add_argument("--conc10-rounds", type=int, default=3)
    ap.add_argument("--conc10-count", type=int, default=10)
    ap.add_argument("--round-gap-secs", type=float, default=20.0)
    ap.add_argument("--image-gen-deadline", type=float, default=65.0)
    ap.add_argument("--sse-diagnostic-read-secs", type=float, default=0.0, help="0=auto max(deadline+30, 120)")
    ap.add_argument("--protocol", default="picture_v2", choices=("picture_v2", "spa_tool"))
    ap.add_argument("--suite-id", default="")
    ap.add_argument("--local-out", default="")
    args = ap.parse_args()
    sse_read = float(args.sse_diagnostic_read_secs or 0.0)
    if sse_read <= 0:
        sse_read = max(float(args.image_gen_deadline) + 30.0, 120.0)

    suite_id = args.suite_id or datetime.now(timezone.utc).strftime("acceptance-%Y%m%dT%H%M%SZ")
    host_out = f"{REMOTE_HOST_ROOT}/{suite_id}"
    container_out = f"/app/data/runlogs/spa_repro/{suite_id}"
    local_out = Path(args.local_out) if args.local_out else ROOT / "docs" / "captures" / "spa" / f"{suite_id}.json"

    wall_start = time.time()
    report: dict[str, Any] = {
        "suite_id": suite_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "account_email": args.email,
        "protocol": args.protocol,
        "image_gen_deadline_secs": args.image_gen_deadline,
        "sse_diagnostic_read_secs": sse_read,
        "host_out": host_out,
        "container_out": container_out,
        "serial5": None,
        "conc10_rounds": [],
        "execution_log": [],
    }

    print(f"[stage] {STAGE_RUNNER}", flush=True)
    if _stage() != 0:
        report["outcome"] = "stage_failed"
        return 1
    _ssh(f"mkdir -p {host_out}")

    serial_ok = True
    for r in range(1, args.serial_rounds + 1):
        print(f"[serial5] round {r}/{args.serial_rounds}", flush=True)
        t0 = time.time()
        code, log = _run_serial_round(
            out_dir=container_out,
            email=args.email,
            round_no=r,
            rounds=args.serial_rounds,
            round_gap=args.round_gap_secs,
            image_deadline=args.image_gen_deadline,
            sse_read=sse_read,
            protocol=args.protocol,
        )
        entry = {
            "phase": "serial5",
            "round": r,
            "exit_code": code,
            "wall_secs": round(time.time() - t0, 2),
            "log_tail": log[-4000:],
        }
        report["execution_log"].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if code != 0:
            serial_ok = False
            report["outcome"] = f"serial5_failed_round_{r}"
            break
        if r < args.serial_rounds and args.round_gap_secs > 0:
            time.sleep(args.round_gap_secs)

    report["serial5"] = _collect_serial5(host_out, args.serial_rounds)
    if serial_ok and not (report["serial5"].get("summary") or {}).get("serial5_passed"):
        serial_ok = False
        report["outcome"] = "serial5_summary_not_passed"

    if serial_ok:
        report["outcome"] = "serial5_passed"
        for c in range(1, args.conc10_rounds + 1):
            conc_dir_host = f"{host_out}/conc10-r{c}"
            conc_dir_container = f"{container_out}/conc10-r{c}"
            print(f"[conc10] round {c}/{args.conc10_rounds}", flush=True)
            _ssh(f"mkdir -p {conc_dir_host}")
            t0 = time.time()
            code, log = _run_conc10(out_dir=conc_dir_container, count=args.conc10_count)
            entry = {
                "phase": "pipeline_conc10",
                "round": c,
                "exit_code": code,
                "wall_secs": round(time.time() - t0, 2),
                "log_tail": log[-4000:],
            }
            report["execution_log"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            collected = _collect_conc10(conc_dir_host)
            collected["round"] = c
            collected["exit_code"] = code
            report["conc10_rounds"].append(collected)
            if code != 0:
                report["outcome"] = f"conc10_failed_round_{c}"
                break
            if c < args.conc10_rounds:
                time.sleep(10)
        else:
            if report["outcome"] == "serial5_passed":
                report["outcome"] = "all_passed"

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["total_wall_secs"] = round(time.time() - wall_start, 2)

    local_out.parent.mkdir(parents=True, exist_ok=True)
    local_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = local_out.with_suffix(".md")
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    remote_report = f"{host_out}/suite-report.json"
    proc = subprocess.run(
        ["ssh", "panda", "python3", "-"],
        input=json.dumps(report, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode == 0:
        install = (
            f"import pathlib,sys; p=pathlib.Path({remote_report!r}); "
            f"p.write_text(sys.stdin.read(), encoding='utf-8'); print(p)"
        )
        subprocess.run(["ssh", "panda", "python3", "-c", install], input=proc.stdout)

    print(json.dumps({"outcome": report["outcome"], "report": str(local_out), "md": str(md_path)}, ensure_ascii=False))
    return 0 if report["outcome"] == "all_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
