#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path("/root/gptimage")
BASE = "http://127.0.0.1:8012"
PUBLIC = "https://gptimage.relai.asia"
STRICT_LOG_RE = re.compile(
    r'Traceback|dictionary changed|image service busy|HTTP/1\.1" 5[0-9][0-9] '
    r"|\b524\b|\b502\b|connection reset|timeout_pending|Unhandled|Exception",
    re.I,
)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cmd(cmd: list[str], timeout: float = 30) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "output": proc.stdout[-20000:],
        }
    except Exception as exc:
        return {"ok": False, "elapsed_ms": round((time.time() - started) * 1000, 2), "error": repr(exc)}


def auth_headers() -> dict[str, str]:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    key = str(cfg.get("auth-key") or cfg.get("auth_key") or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


AUTH_HEADERS = auth_headers()


def http_json(path: str, *, auth: bool = False, timeout: float = 20) -> dict[str, Any]:
    headers = AUTH_HEADERS if auth else {}
    started = time.time()
    req = urllib.request.Request(BASE + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = raw[:1000].decode("utf-8", "replace")
            return {
                "ok": True,
                "status": resp.status,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "bytes": len(raw),
                "body": body,
            }
    except Exception as exc:
        return {"ok": False, "elapsed_ms": round((time.time() - started) * 1000, 2), "error": repr(exc)}


def public_curl(path: str, timeout: int = 20) -> str:
    out = run_cmd(
        [
            "curl",
            "-sS",
            "-m",
            str(timeout),
            "-o",
            "/tmp/r56_public_body",
            "-w",
            "HTTP=%{http_code} time=%{time_total} bytes=%{size_download}",
            PUBLIC + path,
        ],
        timeout=timeout + 5,
    )
    return str(out.get("output") or out)


def default_iface() -> str:
    out = run_cmd(
        [
            "bash",
            "-lc",
            "ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i==\"dev\") print $(i+1)}' | head -1",
        ],
        timeout=5,
    )
    text = str(out.get("output") or "").strip()
    if text:
        return text.splitlines()[0]
    candidates = [p.name for p in Path("/sys/class/net").iterdir() if p.name != "lo"]
    return candidates[0] if candidates else "eth0"


IFACE = default_iface()


def net_bytes() -> tuple[int, int]:
    base = Path("/sys/class/net") / IFACE / "statistics"
    return int((base / "rx_bytes").read_text()), int((base / "tx_bytes").read_text())


def docker_stats() -> dict[str, Any]:
    out = run_cmd(["docker", "stats", "chatgpt2api-local", "--no-stream", "--format", "{{json .}}"], timeout=10)
    sample: dict[str, Any] = {"ok": out.get("ok"), "raw": str(out.get("output") or "").strip()}
    try:
        data = json.loads(sample["raw"])
        sample["cpu_pct"] = float(str(data.get("CPUPerc", "0")).replace("%", "").strip() or 0)
        mem = str(data.get("MemUsage", ""))
        match = re.search(r"([0-9.]+)([KMG]i?B?)\s*/\s*([0-9.]+)([KMG]i?B?)", mem)

        def to_mib(value: float, unit: str) -> float:
            unit = unit.lower()
            if unit.startswith("k"):
                return value / 1024
            if unit.startswith("g"):
                return value * 1024
            return value

        if match:
            sample["mem_mib"] = to_mib(float(match.group(1)), match.group(2))
            sample["mem_limit_mib"] = to_mib(float(match.group(3)), match.group(4))
        pids = str(data.get("PIDs") or "").strip()
        sample["pids"] = int(pids) if pids.isdigit() else None
        sample["block_io"] = data.get("BlockIO")
        sample["net_io"] = data.get("NetIO")
    except Exception as exc:
        sample["parse_error"] = repr(exc)
    return sample


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def summary(values: list[float]) -> dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0}
    return {
        "count": len(vals),
        "min": round(min(vals), 3),
        "p50": round(pct(vals, 50) or 0, 3),
        "p95": round(pct(vals, 95) or 0, 3),
        "p99": round(pct(vals, 99) or 0, 3),
        "max": round(max(vals), 3),
        "avg": round(sum(vals) / len(vals), 3),
    }


def db_counts() -> dict[str, Any]:
    db = ROOT / "data" / "image_tasks.db"
    result = {"exists": db.exists(), "size": db.stat().st_size if db.exists() else 0}
    if db.exists():
        con = sqlite3.connect(db)
        try:
            result["counts"] = [tuple(row) for row in con.execute("select status,count(*) from image_tasks group by status")]
        finally:
            con.close()
    return result


def strict_logs(since: str) -> dict[str, Any]:
    out = run_cmd(["docker", "logs", "--since", since, "chatgpt2api-local"], timeout=30)
    lines = [line for line in str(out.get("output") or "").splitlines() if STRICT_LOG_RE.search(line)]
    return {"count": len(lines), "tail": lines[-80:]}


def task_rows(task_ids: list[str]) -> list[dict[str, Any]]:
    db = ROOT / "data" / "image_tasks.db"
    if not db.exists() or not task_ids:
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        query = "select task_id,status,updated_ts,data from image_tasks where task_id in (%s) order by task_id" % ",".join(
            "?" * len(task_ids)
        )
        rows = con.execute(query, task_ids).fetchall()
    finally:
        con.close()
    parsed = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except Exception:
            data = {}
        parsed.append(
            {
                "task_id": row["task_id"],
                "status": row["status"],
                "mode": data.get("mode"),
                "progress": data.get("progress"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "duration_ms": data.get("duration_ms"),
                "error": data.get("error"),
                "conversation_id": data.get("conversation_id"),
                "resume_attempts": data.get("resume_attempts"),
                "model": data.get("model"),
            }
        )
    return parsed


def collect_snapshot(report_dir: Path, name: str) -> dict[str, Any]:
    snap = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "docker_compose_ps": run_cmd(["docker", "compose", "-f", "docker-compose.panda.yml", "ps"]),
        "docker_inspect_limits": run_cmd(
            ["docker", "inspect", "chatgpt2api-local", "--format", "NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}"]
        ),
        "docker_stats": docker_stats(),
        "health": http_json("/health?format=json"),
        "settings": http_json("/api/settings", auth=True),
        "maintenance": http_json("/api/accounts/maintenance-loop/status", auth=True),
        "image_tasks": http_json("/api/image-tasks/status", auth=True),
        "image_task_db": db_counts(),
        "strict_logs_15m": strict_logs("15m"),
        "df": run_cmd(["df", "-h", "/", "/root/gptimage"]),
        "free": run_cmd(["free", "-m"]),
        "uptime": run_cmd(["uptime"]),
        "public_health": public_curl("/health?format=json"),
        "public_image_manager": public_curl("/image-manager/"),
    }
    write_json(report_dir / f"{name}.json", snap)
    return snap


def sample_baseline(seconds: float, interval: float) -> list[dict[str, Any]]:
    samples = []
    last_rx, last_tx = net_bytes()
    last_t = time.time()
    end = last_t + seconds
    while time.time() < end:
        time.sleep(min(interval, max(0.1, end - time.time())))
        now = time.time()
        rx, tx = net_bytes()
        delta = max(0.001, now - last_t)
        rx_mbps = (rx - last_rx) * 8 / delta / 1_000_000
        tx_mbps = (tx - last_tx) * 8 / delta / 1_000_000
        samples.append({"ts": now, "rx_mbps": rx_mbps, "tx_mbps": tx_mbps, "total_mbps": rx_mbps + tx_mbps})
        last_rx, last_tx, last_t = rx, tx, now
    return samples


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: r56_panda_monitor.py REPORT_DIR RUN_ID", file=sys.stderr)
        return 2
    report_dir = Path(sys.argv[1])
    run_id = sys.argv[2]
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / "monitor-meta.json", {"run_id": run_id, "iface": IFACE, "started_at": time.time()})
    collect_snapshot(report_dir, "pre-snapshot")
    baseline = sample_baseline(60, 5)
    write_json(
        report_dir / "bandwidth-baseline.json",
        {
            "iface": IFACE,
            "samples": baseline,
            "summary": {
                "rx_mbps": summary([row["rx_mbps"] for row in baseline]),
                "tx_mbps": summary([row["tx_mbps"] for row in baseline]),
                "total_mbps": summary([row["total_mbps"] for row in baseline]),
            },
        },
    )
    (report_dir / "monitor.ready").write_text(str(time.time()), encoding="utf-8")

    samples = []
    status_history = []
    last_status = 0.0
    last_rx, last_tx = net_bytes()
    last_t = time.time()
    started = last_t
    stop_file = report_dir / "stop-monitor"
    while not stop_file.exists() and time.time() - started < 3600:
        time.sleep(5)
        now = time.time()
        rx, tx = net_bytes()
        delta = max(0.001, now - last_t)
        rx_mbps = (rx - last_rx) * 8 / delta / 1_000_000
        tx_mbps = (tx - last_tx) * 8 / delta / 1_000_000
        sample = {
            "ts": now,
            "elapsed_seconds": now - started,
            "rx_mbps": rx_mbps,
            "tx_mbps": tx_mbps,
            "total_mbps": rx_mbps + tx_mbps,
            "docker": docker_stats(),
            "health_latency_ms": http_json("/health?format=json", timeout=10).get("elapsed_ms"),
        }
        samples.append(sample)
        if now - last_status >= 15:
            ids_path = report_dir / "task-ids.json"
            if ids_path.exists():
                try:
                    ids = json.loads(ids_path.read_text(encoding="utf-8")).get("task_ids", [])
                except Exception:
                    ids = []
                if ids:
                    query = "/api/image-tasks/status?ids=" + urllib.parse.quote(",".join(ids))
                    resp = http_json(query, auth=True, timeout=30)
                    counts: dict[str, int] = {}
                    if isinstance(resp.get("body"), dict) and isinstance(resp["body"].get("items"), list):
                        for item in resp["body"]["items"]:
                            status = str(item.get("status") or "missing")
                            counts[status] = counts.get(status, 0) + 1
                    status_history.append({"ts": now, "elapsed_seconds": now - started, "counts": counts, "latency_ms": resp.get("elapsed_ms")})
            last_status = now
        last_rx, last_tx, last_t = rx, tx, now

    write_json(report_dir / "monitor-samples.json", {"samples": samples})
    write_json(report_dir / "monitor-status-history.json", {"history": status_history})
    post = collect_snapshot(report_dir, "post-snapshot")
    strict = strict_logs("60m")
    write_json(report_dir / "strict-logs-60m.json", strict)

    ids = []
    ids_path = report_dir / "task-ids.json"
    if ids_path.exists():
        try:
            ids = json.loads(ids_path.read_text(encoding="utf-8")).get("task_ids", [])
        except Exception:
            ids = []
    rows = task_rows(ids)
    write_json(report_dir / "task-db-rows.json", {"rows": rows})

    cpu = [float((row.get("docker") or {}).get("cpu_pct") or 0) for row in samples]
    mem = [float((row.get("docker") or {}).get("mem_mib") or 0) for row in samples if (row.get("docker") or {}).get("mem_mib") is not None]
    bw_rx = [float(row["rx_mbps"]) for row in samples]
    bw_tx = [float(row["tx_mbps"]) for row in samples]
    bw_total = [float(row["total_mbps"]) for row in samples]
    health = [float(row.get("health_latency_ms") or 0) for row in samples if row.get("health_latency_ms") is not None]
    success = [row for row in rows if row.get("status") == "success"]
    errors = [row for row in rows if row.get("status") == "error"]
    timeout_pending = [row for row in rows if row.get("status") == "timeout_pending"]
    durations = [float(row.get("duration_ms") or 0) / 1000 for row in rows if row.get("duration_ms")]
    monitor_summary = {
        "run_id": run_id,
        "report_dir": str(report_dir),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "resources": {
            "cpu_pct": summary(cpu),
            "memory_mib": summary(mem),
            "bandwidth_rx_mbps": summary(bw_rx),
            "bandwidth_tx_mbps": summary(bw_tx),
            "bandwidth_total_mbps": summary(bw_total),
            "health_latency_ms": summary(health),
        },
        "task_db": {
            "total": len(rows),
            "success": len(success),
            "error": len(errors),
            "timeout_pending": len(timeout_pending),
            "duration_seconds": summary(durations),
            "errors": [{"task_id": row.get("task_id"), "error": row.get("error")} for row in errors],
        },
        "strict_bad_count_60m": strict.get("count"),
        "post_accounts": ((post.get("health") or {}).get("body") or {}).get("accounts")
        if isinstance((post.get("health") or {}).get("body"), dict)
        else None,
    }
    write_json(report_dir / "monitor-summary.json", monitor_summary)
    (report_dir / "monitor.done").write_text(str(time.time()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
