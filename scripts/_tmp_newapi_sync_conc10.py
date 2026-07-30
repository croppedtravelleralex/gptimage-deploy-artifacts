#!/usr/bin/env python3
"""NewAPI sync /v1/images/generations conc10 (verify 180s handoff budget)."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _load_auth_key(config_path: str) -> str:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return str(cfg.get("auth-key") or cfg.get("auth_key") or "").strip()


def _fetch_newapi_token() -> str:
    dsn = subprocess.check_output(
        ["docker", "exec", "new-api", "sh", "-lc", 'printf %s "$SQL_DSN"'],
        text=True,
    ).strip()
    sql = """
select t.key
from tokens t
join abilities a on a."group" = t."group"
where t.status = 1
  and t.deleted_at is null
  and a.model = 'gpt-image-2'
  and a.enabled = true
  and (t.unlimited_quota = true or t.remain_quota > 1000)
order by t.unlimited_quota desc, t.remain_quota desc, t.id asc
limit 1;
"""
    key = subprocess.check_output(
        ["docker", "exec", "new-api-postgres", "psql", dsn, "-Atc", sql],
        text=True,
    ).strip()
    if not key:
        raise RuntimeError("no usable NewAPI token with gpt-image-2 ability")
    return key


def _http_json(
    base: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict | None = None,
    timeout: float,
) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8"))
            return {
                "ok": True,
                "status": resp.status,
                "body": parsed,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw[:800].decode("utf-8", "replace")
        return {
            "ok": False,
            "status": exc.code,
            "body": parsed,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
        }
    except Exception as exc:
        return {"ok": False, "status": 0, "body": str(exc), "elapsed_ms": round((time.time() - started) * 1000, 2)}


def _dispatch_emails(panda_base: str, auth_key: str) -> list[str]:
    res = _http_json(
        panda_base,
        "/api/accounts?limit=500",
        headers={"Authorization": f"Bearer {auth_key}"},
        timeout=60,
    )
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    items = body.get("items") if isinstance(body.get("items"), list) else []
    return sorted(
        {
            str(item.get("email") or "").strip()
            for item in items
            if isinstance(item, dict)
            and item.get("image_schedulable")
            and str(item.get("email") or "").strip()
        }
    )


def _one_sync(
    idx: int,
    *,
    newapi_base: str,
    newapi_key: str,
    run_id: str,
    email: str,
    timeout: float,
) -> dict:
    body = {
        "model": "gpt-image-2",
        "prompt": f"{run_id}: ceramic mug on wood table, soft daylight, no text, variant {idx}",
        "n": 1,
        "size": "1024x1024",
        "quality": "auto",
        "response_format": "b64_json",
    }
    headers = {
        "Authorization": f"Bearer {newapi_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if email:
        headers["X-Preferred-Account-Email"] = email
    res = _http_json(newapi_base, "/v1/images/generations", headers=headers, body=body, timeout=timeout)
    parsed = res.get("body") if isinstance(res.get("body"), dict) else {}
    b64_len = 0
    task_id = parsed.get("task_id")
    images = parsed.get("data")
    if isinstance(images, list) and images and isinstance(images[0], dict):
        b64_len = len(str(images[0].get("b64_json") or ""))
    ok = bool(res.get("ok")) and b64_len > 1000
    handoff = bool(task_id) and not ok and int(res.get("status") or 0) in {200, 202, 504}
    return {
        "idx": idx,
        "account_email": email,
        "http_status": res.get("status"),
        "elapsed_ms": res.get("elapsed_ms"),
        "ok": ok,
        "b64_len": b64_len,
        "task_id": task_id,
        "handoff_like": handoff,
        "error": parsed.get("error") if isinstance(parsed, dict) else res.get("body"),
        "panda_error": parsed.get("panda_error") if isinstance(parsed, dict) else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--newapi-base", default=os.environ.get("NEWAPI_BASE", "http://127.0.0.1:8081"))
    ap.add_argument("--panda-base", default=os.environ.get("PANDA_BASE", "http://127.0.0.1:8012"))
    ap.add_argument("--config", default="/root/gptimage/config.json")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=200.0, help="per-request client timeout (secs)")
    ap.add_argument("--out-dir", default="/app/data/runlogs/spa_repro/newapi-sync-conc10")
    args = ap.parse_args()

    run_id = f"newapi-sync-conc10-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    newapi_key = _fetch_newapi_token()
    panda_auth = _load_auth_key(args.config)
    emails = _dispatch_emails(args.panda_base, panda_auth)

    wall0 = time.time()
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.count) as pool:
        futures = [
            pool.submit(
                _one_sync,
                i,
                newapi_base=args.newapi_base.rstrip("/"),
                newapi_key=newapi_key,
                run_id=run_id,
                email=emails[i % len(emails)] if emails else "",
                timeout=args.timeout,
            )
            for i in range(args.count)
        ]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r.get("idx", 0))
    wall_ms = round((time.time() - wall0) * 1000, 2)

    ok_n = sum(1 for r in results if r.get("ok"))
    handoff_n = sum(1 for r in results if r.get("handoff_like"))
    report = {
        "run_id": run_id,
        "newapi_base": args.newapi_base,
        "panda_base": args.panda_base,
        "count": args.count,
        "client_timeout_secs": args.timeout,
        "dispatch_accounts": emails,
        "wall_clock_ms": wall_ms,
        "ok_count": ok_n,
        "handoff_like_count": handoff_n,
        "results": results,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": ok_n == args.count,
                "report": str(out_path),
                "summary": {
                    "ok": ok_n,
                    "failed": args.count - ok_n,
                    "handoff_like": handoff_n,
                    "wall_clock_ms": wall_ms,
                },
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok_n == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
