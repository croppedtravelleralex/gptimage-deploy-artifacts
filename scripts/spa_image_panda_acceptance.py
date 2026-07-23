#!/usr/bin/env python3
"""Panda PROTO-PURE-HTTP acceptance orchestration.

Phases:
  cf_scan5    — 5 concurrent Webshare nodes, home+requirements CF probe
  serial5     — sticky Webshare serial 5 with strict gate
  concurrent4 — 4-account concurrent image gen (requires serial5 pass)
  full        — serial5 then concurrent4
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))


def _load_acceptance_gates():
    try:
        from scripts.spa_acceptance_gates import (  # noqa: WPS433
            concurrent4_allowed,
            serial5_passed,
            should_stop_serial5,
            summarize_cf_layers,
            summarize_failure_classes,
        )
        return (
            concurrent4_allowed,
            serial5_passed,
            should_stop_serial5,
            summarize_cf_layers,
            summarize_failure_classes,
        )
    except ImportError:
        import importlib.util

        sibling = Path(__file__).resolve().parent / "spa_acceptance_gates.py"
        spec = importlib.util.spec_from_file_location("spa_acceptance_gates", sibling)
        if spec is None or spec.loader is None:
            raise
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return (
            mod.concurrent4_allowed,
            mod.serial5_passed,
            mod.should_stop_serial5,
            mod.summarize_cf_layers,
            mod.summarize_failure_classes,
        )


(
    concurrent4_allowed,
    serial5_passed,
    should_stop_serial5,
    summarize_cf_layers,
    summarize_failure_classes,
) = _load_acceptance_gates()


def _load_bench() -> Any:
    override = str(os.environ.get("SPA_BENCH_PATH") or "").strip()
    path = Path(override) if override else ROOT / "scripts" / "_tmp_spa_image_bench3.py"
    spec = importlib.util.spec_from_file_location("spa_image_bench3", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load spa bench3")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _normalize_proxy_line(line: str) -> str:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    parts = text.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    return text


def _load_proxy_pool(pool_path: Path, *, limit: int = 5) -> list[str]:
    lines = [
        _normalize_proxy_line(line)
        for line in pool_path.read_text(encoding="utf-8").splitlines()
    ]
    proxies = [line for line in lines if line]
    if not proxies:
        raise SystemExit(f"empty proxy pool: {pool_path}")
    return proxies[:limit]


def _load_accounts_by_hash(bench: Any, account_hashes: list[str]) -> list[dict[str, Any]]:
    from services.account_service import account_service

    account_service.reload_from_storage()
    wanted = {str(v or "").strip().lower() for v in account_hashes if str(v or "").strip()}
    found = [
        item
        for item in account_service.list_accounts()
        if bench.account_hash(item.get("access_token")) in wanted
    ]
    missing = wanted - {bench.account_hash(item.get("access_token")) for item in found}
    if missing:
        raise SystemExit(f"account hashes not found: {sorted(missing)}")
    return found


def _load_accounts_emails(emails: list[str]) -> list[dict[str, Any]]:
    from services.account_service import account_service

    account_service.reload_from_storage()
    wanted = {e.strip().lower() for e in emails if e.strip()}
    found = [
        item
        for item in account_service.list_accounts()
        if str(item.get("email") or "").strip().lower() in wanted
    ]
    missing = wanted - {str(a.get("email") or "").strip().lower() for a in found}
    if missing:
        raise SystemExit(f"accounts not found: {sorted(missing)}")
    return found


def _summarize_row(row: dict[str, Any], *, round_no: int | None = None) -> dict[str, Any]:
    timings = row.get("timings_ms") or {}
    layers = row.get("cf_layers") or row.get("cf_observability") or {}
    return {
        "round": round_no,
        "ok": bool(row.get("ok")),
        "error": str(row.get("error") or ""),
        "failure_class": row.get("failure_class"),
        "cf_classification": row.get("cf_classification"),
        "cf_layers": layers,
        "account_email": (row.get("account") or {}).get("email"),
        "proxy_hash": (row.get("proxy") or {}).get("hash"),
        "conversation_id": (row.get("conversation") or {}).get("id") or row.get("conversation_id"),
        "has_image_gen": (row.get("conversation") or {}).get("has_image_gen") or row.get("has_image_gen"),
        "download_ok": row.get("download_ok"),
        "total_ms": timings.get("total_ms"),
        "timings_ms": timings,
        "sse_chunks": row.get("sse_chunks"),
        "sse_diagnostic": row.get("sse_diagnostic"),
        "sse_event_timeline": (row.get("sse_event_timeline") or [])[:40],
    }


def run_cf_scan5(
    bench: Any,
    proxies: list[str],
    *,
    workers: int,
    out_dir: Path,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes: list[dict[str, Any]] = []

    def _one(proxy: str, idx: int) -> dict[str, Any]:
        result = bench.run_cf_probe(proxy)
        result["index"] = idx
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_one, proxy, i + 1) for i, proxy in enumerate(proxies)]
        for fut in concurrent.futures.as_completed(futs):
            nodes.append(fut.result())
    nodes.sort(key=lambda n: int(n.get("index") or 0))

    cf403 = sum(
        1
        for n in nodes
        if not n.get("ok")
        and (
            str(n.get("cf_classification") or "") == "cf403"
            or int((n.get("cf_observability") or {}).get("propagated_cf") or 0) > 0
        )
    )
    evidence = {
        "schema_version": "pure-http-webshare-cf-scan/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workers": workers,
        "probe": "home+requirements_prepare",
        "summary": {
            "total": len(nodes),
            "ok": sum(1 for n in nodes if n.get("ok")),
            "cf403": cf403,
        },
        "nodes": nodes,
    }
    bench.write_evidence(out_dir / "cf_scan5.json", evidence)
    print(json.dumps(evidence["summary"], ensure_ascii=False), flush=True)
    return evidence


def _load_prior_serial_rows(out_dir: Path, only_round: int) -> list[dict[str, Any]]:
    if only_round <= 1:
        return []
    path = out_dir / "serial5.json"
    if not path.is_file():
        raise SystemExit(f"missing prior evidence for round {only_round}: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = list(data.get("rows") or [])
    if len(rows) != only_round - 1:
        raise SystemExit(f"expected {only_round - 1} prior rows, got {len(rows)}")
    for row in rows:
        if not row.get("ok"):
            raise SystemExit(f"prior round {row.get('round')} not ok; fix before continuing")
    return rows


def run_serial5(
    bench: Any,
    account: dict[str, Any],
    *,
    rounds: int,
    round_gap_secs: float,
    protocol: str,
    image_gen_deadline: float,
    sse_diagnostic_read_secs: float,
    out_dir: Path,
    gate_policy: str,
    only_round: int | None = None,
    planned_rounds: int | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    proxy = str(account.get("proxy") or "").strip()
    token = str(account.get("access_token") or "").strip()
    email = str(account.get("email") or "")
    if not proxy or not token:
        raise SystemExit("account missing sticky proxy or access_token")

    from services.account_service import account_service

    planned = int(planned_rounds or rounds)
    if only_round is not None:
        if only_round < 1 or only_round > planned:
            raise SystemExit(f"--only-round must be 1..{planned}")
        loop_rounds = [only_round]
        prior_rows = _load_prior_serial_rows(out_dir, only_round)
    else:
        loop_rounds = list(range(1, max(1, rounds) + 1))
        prior_rows = []

    rows: list[dict[str, Any]] = []
    stopped_early = False
    stop_reason = ""
    for i in loop_rounds:
        secret = {
            "email": email,
            "access_token": token,
            "proxy": proxy,
            "proxy_provider": str(account.get("proxy_provider") or "webshare"),
            "fp": account.get("fp") or {},
        }
        try:
            row = bench.run_once(
                secret,
                proxy,
                "panda_webshare",
                bench.MEDIUM_PROMPT,
                protocol=protocol,
                image_gen_deadline=image_gen_deadline,
                sse_diagnostic_read_secs=sse_diagnostic_read_secs,
                out_dir=out_dir,
            )
        except Exception as exc:  # noqa: BLE001
            row = {
                "ok": False,
                "error": bench._sanitize_error(exc, 400),
                "cf_classification": bench._cf_classification(exc),
            }
        row["round"] = i
        rows.append(row)
        bench.write_evidence(out_dir / f"round-{i}-canary.json", row)
        print(
            json.dumps(
                {
                    "round": i,
                    "account_email": email,
                    "ok": row.get("ok"),
                    "failure_class": row.get("failure_class"),
                    "error": row.get("error"),
                    "timings_ms": row.get("timings_ms"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        stop, reason = should_stop_serial5(prior_rows + rows, policy=gate_policy)
        if stop:
            stopped_early = True
            stop_reason = reason
            break
        layers = row.get("cf_layers") or row.get("cf_observability") or {}
        if int(layers.get("propagated_cf") or 0) > 0 or int(layers.get("tasks_cf403") or 0) > 0:
            try:
                from services.proxy_cf_failover import maybe_swap_after_cf_layers

                swap = maybe_swap_after_cf_layers(token, layers)
                row["proxy_failover"] = swap
                bench.write_evidence(out_dir / f"round-{i}-proxy-failover.json", swap)
                if swap.get("ok"):
                    proxy = str((account_service.get_account(token) or {}).get("proxy") or proxy)
            except Exception:
                pass
        if only_round is not None:
            break
        if i < max(loop_rounds) and round_gap_secs > 0 and only_round is None:
            time.sleep(round_gap_secs)

    all_rows_raw = list(prior_rows) + rows
    summary_rows = [_summarize_row(r, round_no=int(r.get("round") or 0)) for r in all_rows_raw]
    ok = sum(1 for r in all_rows_raw if r.get("ok"))
    no_image_gen = sum(
        1
        for r in all_rows_raw
        if not r.get("ok")
        and (
            "no_image_gen" in str(r.get("error") or "").lower()
            or r.get("image_gen_deadline_hit")
            or str(r.get("failure_class") or "").startswith("no_image_gen")
            or r.get("failure_class") == "tool_args_as_text"
        )
    )
    cf403 = sum(
        1
        for r in all_rows_raw
        if str(r.get("cf_classification") or "") == "cf403"
        or int((r.get("cf_layers") or r.get("cf_observability") or {}).get("propagated_cf") or 0) > 0
    )
    evidence = {
        "schema_version": "pure-http-image-serial5/v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate_policy": gate_policy,
        "constraints": {
            "planned_requests": planned,
            "image_gen_deadline_secs": image_gen_deadline,
            "sse_diagnostic_read_secs": sse_diagnostic_read_secs,
            "round_gap_secs": round_gap_secs,
            "concurrency": 1,
        },
        "identity": {
            "account_email": email,
            "proxy_hash": bench.proxy_hash(proxy),
        },
        "summary": {
            "planned": planned,
            "attempted": len(all_rows_raw),
            "ok": ok,
            "failed": len(all_rows_raw) - ok,
            "not_run": max(0, planned - len(all_rows_raw)),
            "no_image_gen": no_image_gen,
            "cf403_propagated": cf403,
            "stopped_early": stopped_early,
            "stop_reason": stop_reason or None,
            "serial5_passed": False,
            "failure_classes": summarize_failure_classes(all_rows_raw),
        },
        "cf_layers_aggregate": summarize_cf_layers(all_rows_raw),
        "rows": summary_rows,
        "operations": {
            "whole_request_retried": False,
            "account_switched": False,
            "concurrent_test_started": False,
        },
    }
    evidence["summary"]["serial5_passed"] = serial5_passed(evidence)
    bench.write_evidence(out_dir / "serial5.json", evidence)
    print(json.dumps(evidence["summary"], ensure_ascii=False), flush=True)
    return evidence


def run_concurrent4(
    bench: Any,
    accounts: list[dict[str, Any]],
    *,
    serial5_evidence: Path,
    protocol: str,
    image_gen_deadline: float,
    sse_diagnostic_read_secs: float,
    out_dir: Path,
    max_workers: int = 4,
) -> dict[str, Any]:
    allowed, reason = concurrent4_allowed(serial5_evidence)
    if not allowed:
        blocked = {
            "schema_version": "pure-http-acceptance-gate/v1",
            "blocked_phase": "concurrent4",
            "reason": reason,
            "serial5_evidence": str(serial5_evidence),
        }
        bench.write_evidence(out_dir / "gate_blocked.json", blocked)
        raise SystemExit(f"concurrent4 blocked: {reason}")

    if len(accounts) < max_workers:
        raise SystemExit(f"need at least {max_workers} accounts for concurrent4")

    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = accounts[:max_workers]

    def _one(account: dict[str, Any], idx: int) -> dict[str, Any]:
        proxy = str(account.get("proxy") or "").strip()
        token = str(account.get("access_token") or "").strip()
        secret = {
            "email": str(account.get("email") or ""),
            "access_token": token,
            "proxy": proxy,
            "proxy_provider": str(account.get("proxy_provider") or "webshare"),
            "fp": account.get("fp") or {},
        }
        row = bench.run_once(
            secret,
            proxy,
            "panda_webshare",
            bench.MEDIUM_PROMPT,
            protocol=protocol,
            image_gen_deadline=image_gen_deadline,
            sse_diagnostic_read_secs=sse_diagnostic_read_secs,
            out_dir=out_dir,
        )
        row["index"] = idx
        return row

    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_one, account, i + 1) for i, account in enumerate(jobs)]
        for fut in concurrent.futures.as_completed(futs):
            rows.append(fut.result())
    rows.sort(key=lambda r: int(r.get("index") or 0))

    ok = sum(1 for r in rows if r.get("ok"))
    evidence = {
        "schema_version": "pure-http-image-concurrent4/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prerequisite": {"serial5_evidence": str(serial5_evidence), "serial5_passed": True},
        "workers": max_workers,
        "summary": {
            "ok": ok,
            "failed": len(rows) - ok,
            "no_image_gen": sum(1 for r in rows if not r.get("ok") and "no_image_gen" in str(r.get("error") or "").lower()),
            "cf_abort": sum(1 for r in rows if str(r.get("cf_classification") or "") == "cf403"),
        },
        "rows": [_summarize_row(r, round_no=int(r.get("index") or 0)) for r in rows],
    }
    bench.write_evidence(out_dir / "concurrent4.json", evidence)
    print(json.dumps(evidence["summary"], ensure_ascii=False), flush=True)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Panda PROTO-PURE-HTTP acceptance")
    parser.add_argument(
        "--phase",
        choices=("cf_scan5", "serial5", "concurrent4", "full"),
        required=True,
    )
    parser.add_argument("--account-hash", default="", help="deprecated: prefer --account-email")
    parser.add_argument("--account-email", default="")
    parser.add_argument("--concurrent-emails", default="", help="comma-separated, need >=4 for concurrent4")
    parser.add_argument("--cf-scan-pool", default="", help="webshare proxy list, one per line")
    parser.add_argument("--cf-scan-workers", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--only-round", type=int, default=0, help="run a single serial5 round (1-based)")
    parser.add_argument("--round-gap-secs", type=float, default=20.0)
    parser.add_argument("--protocol", default="spa_tool", choices=("spa_tool", "picture_v2"))
    parser.add_argument("--image-gen-deadline", type=float, default=65.0)
    parser.add_argument("--sse-diagnostic-read-secs", type=float, default=90.0)
    parser.add_argument("--require-serial5-evidence", default="")
    parser.add_argument("--gate-policy", default="strict", choices=("strict", "diagnostic"))
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args(argv)

    bench = _load_bench()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "data" / "runlogs" / "spa_repro" / f"acceptance-{stamp}"

    if args.phase in ("cf_scan5",):
        if not args.cf_scan_pool.strip():
            raise SystemExit("--cf-scan-pool required for cf_scan5")
        proxies = _load_proxy_pool(Path(args.cf_scan_pool), limit=args.cf_scan_workers)
        run_cf_scan5(bench, proxies, workers=args.cf_scan_workers, out_dir=out_dir)
        return 0

    if args.phase in ("serial5", "full"):
        if args.account_email.strip():
            accounts = _load_accounts_emails([args.account_email.strip()])
        elif args.account_hash.strip():
            accounts = _load_accounts_by_hash(bench, [args.account_hash])
        else:
            raise SystemExit("--account-email required for serial5/full (hash is deprecated)")
        serial_evidence = run_serial5(
            bench,
            accounts[0],
            rounds=args.rounds,
            round_gap_secs=args.round_gap_secs,
            protocol=args.protocol,
            image_gen_deadline=args.image_gen_deadline,
            sse_diagnostic_read_secs=args.sse_diagnostic_read_secs,
            out_dir=out_dir,
            gate_policy=args.gate_policy,
            only_round=args.only_round or None,
            planned_rounds=args.rounds,
        )
        if args.phase == "serial5":
            if args.only_round:
                row = (serial_evidence.get("rows") or [])[-1] if serial_evidence.get("rows") else {}
                return 0 if row.get("ok") else 2
            return 0 if serial_evidence["summary"]["serial5_passed"] else 2
        serial5_path = out_dir / "serial5.json"
    else:
        serial5_path = Path(args.require_serial5_evidence or out_dir / "serial5.json")

    if args.phase in ("concurrent4", "full"):
        emails = [e.strip() for e in str(args.concurrent_emails or "").split(",") if e.strip()]
        if len(emails) < args.max_workers:
            raise SystemExit(f"--concurrent-emails needs >={args.max_workers}")
        accounts = _load_accounts_emails(emails)
        evidence = run_concurrent4(
            bench,
            accounts,
            serial5_evidence=serial5_path,
            protocol=args.protocol,
            image_gen_deadline=args.image_gen_deadline,
            sse_diagnostic_read_secs=args.sse_diagnostic_read_secs,
            out_dir=out_dir,
            max_workers=args.max_workers,
        )
        return 0 if evidence["summary"]["ok"] == args.max_workers else 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
