#!/usr/bin/env python3
"""BENCH-001~003: Panda pure_http + ticket_pool ×5 → compare → R report."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATE = "20260724"
REMOTE = "panda"
REMOTE_DIR = "/root/gptimage"
PARENT = f"{REMOTE_DIR}/data/runlogs/image-path-benchmark/{DATE}"
LOCAL_PARENT = ROOT / "data" / "runlogs" / "image-path-benchmark" / DATE
REPORT = ROOT / "docs" / "captures" / "spa" / f"R-image-path-benchmark-{DATE}.md"


def run(cmd: list[str], *, timeout: float = 3600) -> str:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\ncmd={' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 3600) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def scp_to(local: Path, remote_path: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=120)


def _fmt_ms(stats: dict | None) -> str:
    if not isinstance(stats, dict) or stats.get("p50") is None:
        return "-"
    p50 = float(stats["p50"]) / 1000.0
    p90 = float(stats.get("p90") or stats["p50"]) / 1000.0
    return f"{p50:.1f}s / {p90:.1f}s"


def _fmt_num(stats: dict | None, scale: float = 1.0, unit: str = "") -> str:
    if not isinstance(stats, dict) or stats.get("p50") is None:
        return "-"
    p50 = float(stats["p50"]) * scale
    p90 = float(stats.get("p90") or stats["p50"]) * scale
    if unit == "MB":
        return f"{p50:.2f}/{p90:.2f} MB"
    if unit == "t/s":
        return f"{p50:.1f}/{p90:.1f}"
    return f"{p50:.1f}/{p90:.1f}{unit}"


def write_report(summary: dict) -> None:
    paths = summary.get("paths") or {}
    lines = [
        f"# 三路径生图性能对比 — {DATE}",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        "",
        "## 环境",
        "- 目标：Panda `127.0.0.1:8012`",
        "- 账号：`qaflowakjewai6ps@proton.me`",
        "- Prompt：`MEDIUM_PROMPT`（东京雨夜街景）",
        "- 并发：串行 N=5；gap=30s",
        "- pure_http：`panda_webshare` + picture_v2；deadline=90s",
        "- ticket_pool：生产 `/v1/images/generations`（用票路径）",
        "- browser：本轮未跑（后置 BENCH-004）",
        "",
        "## 汇总表",
        "",
        "| 路径 | N | 成功率 | P50/P90 wall | P50/P90 SSE | P50/P90 下载 | P50/P90 tokens/s | P50/P90 上行 | P50/P90 下行 |",
        "|---|---:|---:|---|---|---|---|---|---|",
    ]
    for name in ("pure_http", "ticket_pool", "browser"):
        g = paths.get(name) or {}
        m = g.get("metrics") or {}
        rate = g.get("success_rate")
        rate_s = "-" if rate is None else f"{rate * 100:.0f}%"
        lines.append(
            "| {name} | {n} | {rate} | {wall} | {sse} | {dl} | {tps} | {up} | {down} |".format(
                name=name,
                n=g.get("runs") or 0,
                rate=rate_s,
                wall=_fmt_ms(m.get("wall_clock_ms")),
                sse=_fmt_ms(m.get("sse_stream_ms")),
                dl=_fmt_ms(m.get("download_ms")),
                tps=_fmt_num(m.get("tokens_per_sec"), unit="t/s"),
                up=_fmt_num(m.get("upload_bytes"), scale=1 / (1024 * 1024), unit="MB"),
                down=_fmt_num(m.get("download_bytes"), scale=1 / (1024 * 1024), unit="MB"),
            )
        )

    ph = paths.get("pure_http") or {}
    tp = paths.get("ticket_pool") or {}
    ph_wall = ((ph.get("metrics") or {}).get("wall_clock_ms") or {}).get("p50")
    tp_wall = ((tp.get("metrics") or {}).get("wall_clock_ms") or {}).get("p50")
    conclusion = [
        "- browser 路径本轮未实测（空目录）。",
        "- 生产推荐：继续用票路径 `/v1/images`；pure_http 作对照与协议回归。",
        f"- 证据目录：`data/runlogs/image-path-benchmark/{DATE}/`",
        f"- compare：`data/runlogs/image-path-benchmark/{DATE}/compare-summary.json`",
    ]
    if ph_wall and tp_wall:
        faster = "ticket_pool" if tp_wall < ph_wall else "pure_http"
        conclusion.insert(
            0,
            f"- 墙钟 P50 更快路径：**{faster}**（pure_http={int(ph_wall)}ms，ticket_pool={int(tp_wall)}ms）",
        )

    lines.extend(["", "## 结论", *conclusion, ""])
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    print("[0] sync suite scripts")
    for rel in ("scripts/image_path_benchmark_suite.py", "scripts/_tmp_spa_image_bench3.py"):
        scp_to(ROOT / rel, f"{REMOTE_DIR}/{rel}")
    remote(f"mkdir -p {PARENT}/pure_http {PARENT}/ticket_pool {PARENT}/browser")

    print("[1] BENCH-001 pure_http ×5 (~6–12 min)")
    ph_cmd = (
        "docker exec -w /app -e GPTIMAGE_ROOT=/app chatgpt2api-local "
        "/app/.venv/bin/python scripts/image_path_benchmark_suite.py pure_http "
        f"--mode panda_webshare --runs 5 --gap-secs 30 "
        f"--image-gen-deadline 90 --sse-diagnostic-read-secs 90 --date {DATE} "
        "--secret data/runlogs/spa_repro/qaflow_secret.json"
    )
    print(ph_cmd)
    out1 = remote(ph_cmd, timeout=2400)
    print(out1[-2500:] if len(out1) > 2500 else out1)

    print("[2] BENCH-002 ticket_pool /v1/images ×5 (~6–12 min)")
    tp_cmd = (
        f"cd {REMOTE_DIR} && python3 scripts/image_path_benchmark_suite.py ticket_pool "
        f"--runs 5 --gap-secs 30 --date {DATE} --base-url http://127.0.0.1:8012"
    )
    print(tp_cmd)
    out2 = remote(tp_cmd, timeout=2400)
    print(out2[-2500:] if len(out2) > 2500 else out2)

    print("[3] pull evidence")
    LOCAL_PARENT.mkdir(parents=True, exist_ok=True)
    for name in ("pure_http", "ticket_pool", "browser"):
        dest = LOCAL_PARENT / name
        dest.mkdir(parents=True, exist_ok=True)
        for f in dest.glob("*.json"):
            f.unlink()
        try:
            run(["scp", f"{REMOTE}:{PARENT}/{name}/*.json", str(dest)], timeout=300)
        except Exception as exc:
            print(f"warn pull {name}: {exc}")

    print("[4] compare + report")
    run(
        [
            "python",
            str(ROOT / "scripts" / "image_path_benchmark_suite.py"),
            "compare",
            "--parent-dir",
            str(LOCAL_PARENT),
        ],
        timeout=120,
    )
    summary_path = LOCAL_PARENT / "compare-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    scp_to(summary_path, f"{PARENT}/compare-summary.json")
    write_report(summary)
    print(
        json.dumps(
            {
                "report": str(REPORT),
                "summary": str(summary_path),
                "paths": {k: (v or {}).get("runs") for k, v in (summary.get("paths") or {}).items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    t0 = time.time()
    try:
        code = main()
    except Exception as exc:
        print(f"BENCH_FAILED: {exc}")
        raise SystemExit(1) from exc
    print(f"elapsed_secs={round(time.time() - t0, 1)}")
    raise SystemExit(code)
