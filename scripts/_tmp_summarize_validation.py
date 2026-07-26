#!/usr/bin/env python3
"""Summarize production validation reports into one JSON artifact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(out_dir: Path) -> dict[str, Any]:
    vqa = _load(out_dir / "verify_quota_all_report.json") or {}
    serial = _load(out_dir / "cross_serial_report.json") or {}
    conc = _load(out_dir / "cross_concurrent_report.json") or {}

    quota_rows = []
    for row in vqa.get("accounts") or []:
        qs = (row.get("quota_sync") or {}) if row.get("ok") else {}
        quota_rows.append(
            {
                "email": row.get("email"),
                "ok": row.get("ok"),
                "quota_before": qs.get("quota_before"),
                "quota_after_mark": qs.get("quota_after_mark"),
                "quota_remote": qs.get("quota_remote"),
                "bandwidth_total": ((row.get("bandwidth") or {}).get("total_bytes")),
                "error": row.get("error"),
            }
        )

    serial_rows = []
    serial_bw = 0
    for rnd in serial.get("rounds") or []:
        for case in rnd.get("cases") or []:
            res = case.get("result") or {}
            bw = int((res.get("bandwidth") or {}).get("total_bytes") or 0)
            serial_bw += bw
            serial_rows.append(
                {
                    "round": rnd.get("round"),
                    "email": rnd.get("primary_email"),
                    "mode": case.get("mode"),
                    "prompt_mode": case.get("prompt_mode"),
                    "ok": case.get("ok", True),
                    "egress_ip": ((res.get("egress") or {}).get("ip")),
                    "elapsed_ms": res.get("elapsed_ms"),
                    "bandwidth_total": bw,
                    "image_bytes": ((res.get("bandwidth") or {}).get("image_bytes")),
                    "quota_remote": ((res.get("quota_sync") or {}).get("quota_remote")),
                    "error": case.get("error"),
                }
            )

    conc_rows = []
    conc_bw = 0
    for rnd in conc.get("rounds") or []:
        for item in rnd.get("results") or []:
            res = item.get("result") or {}
            bw = int((res.get("bandwidth") or {}).get("total_bytes") or 0)
            conc_bw += bw
            conc_rows.append(
                {
                    "round": rnd.get("round"),
                    "email": item.get("email"),
                    "label": item.get("label"),
                    "ok": item.get("ok"),
                    "egress_ip": ((res.get("egress") or {}).get("ip")),
                    "elapsed_ms": res.get("elapsed_ms"),
                    "bandwidth_total": bw,
                    "image_bytes": ((res.get("bandwidth") or {}).get("image_bytes")),
                    "quota_remote": ((res.get("quota_sync") or {}).get("quota_remote")),
                    "error": item.get("error"),
                }
            )

    return {
        "out_dir": str(out_dir),
        "verify_quota_all": {
            "total": vqa.get("total"),
            "ok": sum(1 for r in quota_rows if r.get("ok")),
            "failed": sum(1 for r in quota_rows if not r.get("ok")),
            "accounts": quota_rows,
        },
        "cross_serial": {
            "rounds": len(serial.get("rounds") or []),
            "cases": len(serial_rows),
            "bandwidth_total": serial_bw,
            "rows": serial_rows,
        },
        "cross_concurrent": {
            "rounds": len(conc.get("rounds") or []),
            "jobs": len(conc_rows),
            "bandwidth_total": conc_bw,
            "rows": conc_rows,
        },
        "bandwidth_grand_total": serial_bw + conc_bw + sum(int(r.get("bandwidth_total") or 0) for r in quota_rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--write", default="", help="optional path to write summary json")
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    doc = summarize(out_dir)
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    print(text)
    if args.write:
        Path(args.write).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
