#!/usr/bin/env python3
"""Compare canary observe snapshots for drift (P7)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def account(doc: dict) -> dict:
    rows = doc.get("accounts") or []
    return rows[0] if rows else {}


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    labels = ["t0-live", "t0-schedulable", "t0-after-apply", "t1h", "t6h", "t24h", "t72h"]
    snaps = {}
    for label in labels:
        # prefer exact label.json; t0-live is special
        for name in (f"{label}.json",):
            p = out / name
            if p.exists():
                snaps[label] = load(p)
                break
    # also plain t0.json
    if (out / "t0.json").exists():
        snaps.setdefault("t0", load(out / "t0.json"))

    series = []
    for label in ["t0-after-apply", "t0-schedulable", "t1h", "t6h", "t24h", "t72h"]:
        if label not in snaps:
            continue
        a = account(snaps[label])
        h = (snaps[label].get("health") or {})
        series.append(
            {
                "label": label,
                "ts": snaps[label].get("ts"),
                "status": a.get("status"),
                "fp_hash": a.get("fp_hash"),
                "proxy_egress_hash": a.get("proxy_egress_hash"),
                "proxy_binding_hash": a.get("proxy_binding_hash"),
                "traffic_total_bytes": a.get("traffic_total_bytes"),
                "success": a.get("success"),
                "fail": a.get("fail"),
                "invalid_count": a.get("invalid_count"),
                "healthy": (h.get("healthy") if isinstance(h, dict) else None),
                "schedulable": ((h.get("accounts") or {}).get("schedulable") if isinstance(h, dict) else None),
            }
        )

    live = snaps.get("t0-live") or {}
    base_fp = None
    base_egress = None
    if series:
        base_fp = series[0].get("fp_hash")
        base_egress = series[0].get("proxy_egress_hash")
    drift = {
        "fp_drift": [
            s["label"] for s in series if base_fp and s.get("fp_hash") and s.get("fp_hash") != base_fp
        ],
        "egress_drift": [
            s["label"]
            for s in series
            if base_egress and s.get("proxy_egress_hash") and s.get("proxy_egress_hash") != base_egress
        ],
        "terminal_or_disable": [
            s["label"] for s in series if str(s.get("status") or "") in {"禁用", "异常"}
        ],
    }
    verdict = "pass"
    if drift["fp_drift"] or drift["egress_drift"] or drift["terminal_or_disable"]:
        verdict = "hold"
    report = {
        "verdict": verdict,
        "series": series,
        "drift": drift,
        "t0_live": {
            "me_ok": live.get("me_ok"),
            "fp_stable": live.get("fp_stable"),
            "egress_stable": live.get("egress_stable"),
            "egress_matches_stored": live.get("egress_matches_stored"),
            "schedulable": live.get("schedulable"),
        },
        "available_labels": sorted(snaps.keys()),
    }
    path = out / "p7-drift-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# P7 drift report",
        "",
        f"- verdict: **{verdict}**",
        f"- points: {', '.join(s['label'] for s in series)}",
        f"- fp_drift: {drift['fp_drift'] or '0'}",
        f"- egress_drift: {drift['egress_drift'] or '0'}",
        f"- terminal_or_disable: {drift['terminal_or_disable'] or '0'}",
        "",
        "| label | status | fp | egress | traffic | schedulable |",
        "|---|---|---|---|---:|---:|",
    ]
    for s in series:
        md.append(
            f"| {s['label']} | {s.get('status')} | {(s.get('fp_hash') or '')[:12]} | "
            f"{(s.get('proxy_egress_hash') or '')[:12]} | {s.get('traffic_total_bytes')} | {s.get('schedulable')} |"
        )
    (out / "p7-drift-report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"wrote": str(path), "verdict": verdict, "points": len(series)}, ensure_ascii=False))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
