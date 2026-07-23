#!/usr/bin/env python3
"""SPA 生图串行/并发压测（sticky Webshare）。

用法:
  python scripts/spa_image_load_test.py --mode serial --email a@x.com --rounds 5
  python scripts/spa_image_load_test.py --mode concurrent --emails a@x.com,b@x.com --per-account 1
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))


def _load_bench():
    override = str(os.environ.get("SPA_BENCH_PATH") or "").strip()
    path = Path(override) if override else ROOT / "scripts" / "_tmp_spa_image_bench3.py"
    spec = importlib.util.spec_from_file_location("spa_image_bench3", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load spa bench3")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_one(
    bench: Any,
    email: str,
    proxy: str,
    access_token: str,
    proxy_provider: str,
    prompt: str,
    out_dir: Path,
    idx: int,
    *,
    protocol: str = "picture_v2",
    image_gen_deadline: float = 25.0,
    sse_diagnostic_read_secs: float = 90.0,
) -> dict[str, Any]:
    started = time.time()
    secret = {
        "email": email,
        "access_token": access_token,
        "proxy": proxy,
        "proxy_provider": proxy_provider,
        "fp": {},
    }
    try:
        result = bench.run_once(
            secret,
            proxy,
            "panda_webshare",
            prompt,
            protocol=protocol,
            image_gen_deadline=image_gen_deadline,
            sse_diagnostic_read_secs=sse_diagnostic_read_secs,
            out_dir=out_dir,
        )
        result["index"] = idx
        result["elapsed_sec"] = round(time.time() - started, 2)
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "index": idx,
            "account": {"hash": bench.account_hash(access_token)},
            "proxy": {"provider": proxy_provider, "hash": bench.proxy_hash(proxy)},
            "error": bench._sanitize_error(exc, 400),
            "cf_classification": bench._cf_classification(exc),
            "elapsed_sec": round(time.time() - started, 2),
        }


def _load_accounts(emails: list[str]) -> list[dict[str, Any]]:
    from services.account_service import account_service

    account_service.reload_from_storage()
    wanted = {e.strip().lower() for e in emails if e.strip()}
    found: list[dict[str, Any]] = []
    for item in account_service.list_accounts():
        email = str(item.get("email") or "").strip().lower()
        if email in wanted:
            found.append(item)
    missing = wanted - {str(a.get("email") or "").strip().lower() for a in found}
    if missing:
        raise SystemExit(f"accounts not found: {sorted(missing)}")
    return found


def _load_accounts_by_hash(account_hashes: list[str], bench: Any) -> list[dict[str, Any]]:
    from services.account_service import account_service

    account_service.reload_from_storage()
    wanted = {str(value or "").strip().lower() for value in account_hashes if str(value or "").strip()}
    found = [
        item
        for item in account_service.list_accounts()
        if bench.account_hash(item.get("access_token")) in wanted
    ]
    missing = wanted - {bench.account_hash(item.get("access_token")) for item in found}
    if missing:
        raise SystemExit(f"account hashes not found: {sorted(missing)}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPA image serial/concurrent load test")
    parser.add_argument("--mode", choices=("serial", "concurrent"), required=True)
    parser.add_argument("--email", default="", help="serial 模式单账号")
    parser.add_argument("--account-hash", default="", help="serial 模式账号哈希（优先，避免暴露邮箱）")
    parser.add_argument("--emails", default="", help="concurrent 模式逗号分隔邮箱")
    parser.add_argument("--rounds", type=int, default=5, help="serial 连续次数")
    parser.add_argument("--per-account", type=int, default=1, help="concurrent 每号次数")
    parser.add_argument("--out-dir", default="")
    parser.add_argument(
        "--protocol",
        default="picture_v2",
        choices=("picture_v2", "spa_tool"),
        help="picture_v2=conduit+hints（生产链路）；spa_tool=空hints文本shape",
    )
    parser.add_argument(
        "--image-gen-deadline",
        type=float,
        default=25.0,
        help="SSE 内 N 秒无 image_gen 验收门禁（默认 25s）",
    )
    parser.add_argument(
        "--sse-diagnostic-read-secs",
        type=float,
        default=90.0,
        help="SSE 总读取墙钟秒数（默认 90s）",
    )
    parser.add_argument("--round-gap-secs", type=float, default=0.0, help="serial 轮间冷却秒数")
    parser.add_argument("--max-workers", type=int, default=0, help="concurrent 最大 worker，0=全部并发")
    args = parser.parse_args(argv)

    bench = _load_bench()
    prompt = bench.MEDIUM_PROMPT
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "data" / "runlogs" / "spa_repro" / f"load-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "serial":
        if args.account_hash.strip():
            accounts = _load_accounts_by_hash([args.account_hash], bench)
        else:
            emails = [args.email.strip()] if args.email.strip() else []
            if not emails:
                raise SystemExit("--account-hash or --email required for serial mode")
            accounts = _load_accounts(emails)
        account = accounts[0]
        proxy = str(account.get("proxy") or "").strip()
        token = str(account.get("access_token") or "").strip()
        if not proxy or not token:
            raise SystemExit("account missing sticky proxy or access_token")
        rows = []
        for i in range(1, max(1, args.rounds) + 1):
            row = _run_one(
                bench,
                str(account.get("email")),
                proxy,
                token,
                str(account.get("proxy_provider") or "webshare"),
                prompt,
                out_dir,
                i,
                protocol=args.protocol,
                image_gen_deadline=args.image_gen_deadline,
                sse_diagnostic_read_secs=args.sse_diagnostic_read_secs,
            )
            rows.append(row)
            print(json.dumps({"index": i, "ok": row.get("ok"), "error": row.get("error"), "failure_class": row.get("failure_class"), "timings": row.get("timings_ms")}, ensure_ascii=False), flush=True)
            if i < max(1, args.rounds) and args.round_gap_secs > 0:
                time.sleep(args.round_gap_secs)
    else:
        emails = [e.strip() for e in str(args.emails or "").split(",") if e.strip()]
        if len(emails) < 2:
            raise SystemExit("--emails needs >=2 for concurrent mode")
        accounts = _load_accounts(emails)
        jobs: list[tuple[str, str, str, str, int]] = []
        idx = 0
        for account in accounts:
            proxy = str(account.get("proxy") or "").strip()
            token = str(account.get("access_token") or "").strip()
            email = str(account.get("email") or "")
            if not proxy or not token:
                raise SystemExit(f"account missing proxy/token: {email}")
            for _ in range(max(1, args.per_account)):
                idx += 1
                jobs.append((email, proxy, token, str(account.get("proxy_provider") or "webshare"), idx))
        rows = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.max_workers or len(jobs))) as pool:
            futs = [
                pool.submit(
                    _run_one,
                    bench,
                    email,
                    proxy,
                    token,
                    proxy_provider,
                    prompt,
                    out_dir,
                    i,
                    protocol=args.protocol,
                    image_gen_deadline=args.image_gen_deadline,
                    sse_diagnostic_read_secs=args.sse_diagnostic_read_secs,
                )
                for email, proxy, token, proxy_provider, i in jobs
            ]
            for fut in concurrent.futures.as_completed(futs):
                row = fut.result()
                rows.append(row)
                print(
                    json.dumps(
                        {
                            "index": row.get("index"),
                            "account_hash": (row.get("account") or {}).get("hash"),
                            "ok": row.get("ok"),
                            "error": row.get("error"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        rows.sort(key=lambda r: int(r.get("index") or 0))

    ok = sum(1 for r in rows if r.get("ok"))
    cf_abort = sum(
        1
        for r in rows
        if (not r.get("ok"))
        and (
            "cf_abort" in str(r.get("error") or "").lower()
            or "cloudflare_or_edge_html_block" in str(r.get("error") or "").lower()
            or "image poll aborted" in str(r.get("error") or "").lower()
        )
    )
    no_image_gen = sum(
        1
        for r in rows
        if (not r.get("ok"))
        and ("no_image_gen_within" in str(r.get("error") or "").lower() or r.get("image_gen_deadline_hit"))
    )
    failure_classes = {
        "tool_args_as_text": 0,
        "late_image_gen_after_gate": 0,
        "no_image_gen_quiet_stream": 0,
        "no_image_gen_within_gate": 0,
    }
    for row in rows:
        fc = str(row.get("failure_class") or "").strip()
        if fc in failure_classes:
            failure_classes[fc] += 1
    summary = {
        "mode": args.mode,
        "protocol": args.protocol,
        "image_gen_deadline_secs": args.image_gen_deadline,
        "sse_diagnostic_read_secs": args.sse_diagnostic_read_secs,
        "total": len(rows),
        "ok": ok,
        "failed": len(rows) - ok,
        "cf_abort": cf_abort,
        "no_image_gen": no_image_gen,
        "failure_classes": failure_classes,
        "success_rate": round(ok / max(1, len(rows)), 3),
        "generated_at": stamp,
    }
    bench.write_evidence(out_dir / "summary.json", summary)
    bench.write_evidence(out_dir / "rows.json", {"schema_version": "pure-http-image-canary-batch/v1", "rows": rows})
    if len(rows) == 1:
        bench.write_evidence(out_dir / "canary_result.json", rows[0])
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if ok == len(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
