#!/usr/bin/env python3
"""Verify OpenAI ticket+image via Panda /v1/images/generations (production path)."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EMAIL = "qaflowakjewai6ps@proton.me"
CONFIG = Path("/root/gptimage/config.json")
OUT_DIR = Path("/root/gptimage/data/runlogs/spa_repro/ticket-verify-20260723")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    auth = json.loads(CONFIG.read_text(encoding="utf-8")).get("auth-key", "")
    if not auth:
        print(json.dumps({"ok": False, "error": "missing_auth_key"}))
        return 2

    body = json.dumps(
        {
            "model": "gpt-image-2",
            "prompt": "a simple red circle on white background",
            "n": 1,
            "response_format": "b64_json",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8012/v1/images/generations",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {auth}",
            "Content-Type": "application/json",
            "X-Preferred-Account-Email": EMAIL,
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = exc.code
    elapsed = time.time() - t0
    out_path = OUT_DIR / "v1_images_response.json"
    out_path.write_bytes(raw)
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        data = {"raw": raw.decode("utf-8", errors="replace")[:500]}

    images = data.get("data") if isinstance(data, dict) else None
    b64_len = 0
    if isinstance(images, list) and images:
        b64_len = len(str(images[0].get("b64_json") or ""))

    summary = {
        "ok": code == 200 and b64_len > 1000,
        "http_code": code,
        "elapsed_secs": round(elapsed, 2),
        "email": EMAIL,
        "b64_len": b64_len,
        "error": data.get("detail") or (data.get("error") if isinstance(data.get("error"), str) else (data.get("error") or {}).get("message")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence": str(out_path),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
