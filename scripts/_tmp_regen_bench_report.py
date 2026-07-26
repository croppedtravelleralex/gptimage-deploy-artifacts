#!/usr/bin/env python3
"""Regenerate compare-summary + R report from local evidence (no remote pull)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from scripts.image_path_benchmark_suite import compare_paths  # noqa: E402

DATE = "20260724"
LOCAL = ROOT / "data" / "runlogs" / "image-path-benchmark" / DATE
REPORT = ROOT / "docs" / "captures" / "spa" / f"R-image-path-benchmark-{DATE}.md"


def fmt_ms(stats: dict | None) -> str:
    if not isinstance(stats, dict) or stats.get("p50") is None:
        return "-"
    p50 = float(stats["p50"]) / 1000.0
    p90 = float(stats.get("p90") or stats["p50"]) / 1000.0
    return f"{p50:.1f}s / {p90:.1f}s"


def fmt_num(stats: dict | None, scale: float = 1.0, unit: str = "") -> str:
    if not isinstance(stats, dict) or stats.get("p50") is None:
        return "-"
    p50 = float(stats["p50"]) * scale
    p90 = float(stats.get("p90") or stats["p50"]) * scale
    if unit == "MB":
        return f"{p50:.2f}/{p90:.2f} MB"
    if unit == "t/s":
        return f"{p50:.1f}/{p90:.1f}"
    return f"{p50:.1f}/{p90:.1f}{unit}"


def main() -> int:
    summary = compare_paths(
        LOCAL / "pure_http",
        LOCAL / "browser",
        LOCAL / "ticket_pool",
        out_path=LOCAL / "compare-summary.json",
    )
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
            f"| {name} | {g.get('runs') or 0} | {rate_s} | {fmt_ms(m.get('wall_clock_ms'))} | "
            f"{fmt_ms(m.get('sse_stream_ms'))} | {fmt_ms(m.get('download_ms'))} | "
            f"{fmt_num(m.get('tokens_per_sec'), unit='t/s')} | "
            f"{fmt_num(m.get('upload_bytes'), scale=1 / (1024 * 1024), unit='MB')} | "
            f"{fmt_num(m.get('download_bytes'), scale=1 / (1024 * 1024), unit='MB')} |"
        )

    success_walls = []
    for path in sorted((LOCAL / "ticket_pool").glob("result_*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if not row.get("ok"):
            continue
        phase = row.get("phase_timings_ms") or {}
        if isinstance(phase, dict) and phase.get("wall_clock_ms"):
            success_walls.append(int(phase["wall_clock_ms"]))

    ph = paths.get("pure_http") or {}
    tp = paths.get("ticket_pool") or {}
    ph_wall = ((ph.get("metrics") or {}).get("wall_clock_ms") or {}).get("p50")
    tp_wall = ((tp.get("metrics") or {}).get("wall_clock_ms") or {}).get("p50")
    conclusion = [
        "- ticket_pool 前 2 轮 240s 超时（疑似 pure_http 连跑后号忙/上游排队），后 3 轮成功。",
        "- browser 路径本轮未实测。",
        "- 生产推荐：继续用票路径 `/v1/images`；pure_http 作协议对照。",
        f"- 证据：`data/runlogs/image-path-benchmark/{DATE}/`",
    ]
    if success_walls:
        sw = sorted(success_walls)
        conclusion.insert(0, f"- ticket_pool 成功子集 wall≈{sw}（P50={sw[len(sw)//2]}ms，n={len(sw)}）")
    if ph_wall is not None and tp_wall is not None:
        conclusion.insert(
            0,
            f"- 全量 P50 wall：pure_http={int(ph_wall)}ms，ticket_pool={int(tp_wall)}ms（含 2 次超时）",
        )
    if ph_wall is not None and success_walls:
        fair = sorted(success_walls)[len(success_walls) // 2]
        faster = "ticket_pool(成功子集)" if fair < ph_wall else "pure_http"
        conclusion.insert(0, f"- 公平对比（剔除超时）：**{faster}**（pure_http P50={int(ph_wall)}ms vs ticket 成功 P50={fair}ms）")

    lines.extend(["", "## 结论", *conclusion, ""])
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "report": str(REPORT),
        "pure_http_runs": (paths.get("pure_http") or {}).get("runs"),
        "ticket_pool_runs": (paths.get("ticket_pool") or {}).get("runs"),
        "ticket_pool_success_rate": (paths.get("ticket_pool") or {}).get("success_rate"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
