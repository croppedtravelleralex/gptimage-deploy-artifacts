#!/usr/bin/env python3
"""Read-only Panda gptimage deep audit. No writes, no restart."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path

ROOT = Path("/root/gptimage")


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> None:
    print("=== VERSION / CODE HASH ===")
    for rel in [
        "services/openai_backend_api.py",
        "services/account_service.py",
        "services/image_task_service.py",
        "services/account_workload_policy.py",
        "services/account_fingerprint.py",
        "docker-compose.panda.yml",
        "main.py",
    ]:
        p = ROOT / rel
        if p.exists():
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
            print(f"{rel}: {sha12(p)} bytes={p.stat().st_size} mtime={mtime}")
        else:
            print(f"{rel}: MISSING")

    print("\n=== CONFIG (safe keys) ===")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    safe = [
        "image_global_concurrency",
        "image_account_concurrency",
        "submit_workers",
        "poll_workers",
        "image_generation_paused",
        "auto_remove_invalid_accounts",
        "auto_remove_rate_limited_accounts",
        "pre_conversation_timeout_secs",
        "pre_conversation_max_attempts",
        "post_conversation_sse_deadline_secs",
        "newapi_image_sync_admission_max",
        "newapi_image_sync_admission_max_eta_secs",
        "newapi_sync_wait_timeout_secs",
        "submit_start_min_interval_ms",
        "image_return_window_size",
        "burst_enabled",
        "per_user_running_base",
        "per_user_running_max",
        "per_user_running_burst",
    ]
    for k in safe:
        if k in cfg:
            print(k, cfg.get(k))
    for section in (
        "account_refresh_all",
        "account_maintenance_loop",
        "panda_sync",
        "outlook_recovery",
        "proxy_runtime",
    ):
        val = cfg.get(section)
        if not isinstance(val, dict):
            continue
        filtered = {
            k: v
            for k, v in val.items()
            if not any(x in k.lower() for x in ("key", "secret", "password", "token", "proxy", "auth"))
            or k
            in (
                "enabled",
                "staging_enabled",
                "queue_on_failure",
                "delete_invalid",
                "mode",
                "scope",
            )
        }
        print(section, filtered)

    print("\n=== ACCOUNTS DB ===")
    db = ROOT / "data" / "accounts.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    print("tables", [r[0] for r in cur.execute("select name from sqlite_master where type='table'")])
    rows = cur.execute("select access_token, data from accounts").fetchall()
    print("account_rows", len(rows))

    status_c: Counter[str] = Counter()
    recv_c: Counter[str] = Counter()
    sync_c: Counter[str] = Counter()
    recovery_c: Counter[str] = Counter()
    invalid_n = 0
    fp_n = 0
    egress_n = 0
    reg_egress_n = 0
    node_lease = 0
    cohort = 0
    maturity = 0
    have_proxy = 0
    quota_sum = 0
    traffic_n = 0
    proxy_sig: Counter[str] = Counter()
    sample_fields: Counter[str] = Counter()
    deactivated = 0

    for _tok, raw in rows:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        status_c[str(d.get("status") or "")] += 1
        recv_c[str(d.get("panda_receive_state") or "")] += 1
        sync_c[str(d.get("panda_sync_state") or "")] += 1
        recovery_c[str(d.get("outlook_recovery_state") or "")] += 1
        if int(d.get("invalid_count") or 0) > 0:
            invalid_n += 1
        fp = d.get("fp") or {}
        if isinstance(fp, dict) and fp.get("user_agent") and fp.get("device_id"):
            fp_n += 1
        if d.get("proxy_egress_ip") or d.get("proxy_egress_hash") or d.get("egress_hash"):
            egress_n += 1
        if d.get("registration_proxy_egress_hash") or d.get("register_egress_hash"):
            reg_egress_n += 1
        if d.get("node_lease_id") or d.get("proxy_node_id"):
            node_lease += 1
        if d.get("cohort_id") or d.get("source_batch_id"):
            cohort += 1
        if d.get("maturity_state") or d.get("maturity_stage"):
            maturity += 1
        if any(d.get(k) for k in ("traffic_total_bytes", "traffic_uploaded_bytes", "traffic_downloaded_bytes")):
            traffic_n += 1
        try:
            quota_sum += int(d.get("quota") or 0)
        except Exception:
            pass
        err = str(d.get("last_error") or d.get("failure_reason") or "")
        if "account_deactivated" in err or d.get("outlook_recovery_state") == "terminal":
            deactivated += 1
        proxy = str(d.get("proxy") or d.get("http_proxy") or "")
        if proxy:
            have_proxy += 1
            host = proxy.split("@")[-1].split("/")[0]
            proxy_sig[hashlib.sha256(host.encode()).hexdigest()[:12]] += 1
        for k in d.keys():
            sample_fields[k] += 1

    print("status", dict(status_c))
    print("receive_state", dict(recv_c))
    print("sync_state", dict(sync_c))
    print("outlook_recovery_state", dict(recovery_c))
    print("invalid_count>0", invalid_n)
    print("fp_completeish", fp_n)
    print("egress_evidence", egress_n)
    print("reg_egress_evidence", reg_egress_n)
    print("node_lease_field", node_lease)
    print("cohort_field", cohort)
    print("maturity_field", maturity)
    print("traffic_fields", traffic_n)
    print("have_account_proxy", have_proxy)
    print("quota_sum", quota_sum)
    print("deactivated_or_terminal_hint", deactivated)
    print("proxy_host_sig_top", proxy_sig.most_common(8))
    interesting = {
        k: sample_fields[k]
        for k in sorted(sample_fields)
        if any(
            x in k
            for x in (
                "proxy",
                "egress",
                "fp",
                "cohort",
                "lease",
                "maturity",
                "traffic",
                "restore",
                "invalid",
                "outlook",
                "lifecycle",
                "registration",
                "node",
            )
        )
    }
    print("field_coverage_interesting", interesting)

    print("\n=== IMAGE TASKS ===")
    tdb = ROOT / "data" / "image_tasks.db"
    if tdb.exists():
        tcon = sqlite3.connect(f"file:{tdb}?mode=ro", uri=True)
        tcur = tcon.cursor()
        print("tables", [r[0] for r in tcur.execute("select name from sqlite_master where type='table'")])
        for table in ("image_tasks", "tasks"):
            try:
                cols = [r[1] for r in tcur.execute(f"pragma table_info({table})").fetchall()]
            except Exception:
                continue
            print(f"{table}_cols", cols[:50])
            status_col = "status" if "status" in cols else ("state" if "state" in cols else None)
            if status_col:
                print(
                    f"{table}_by_{status_col}",
                    tcur.execute(f"select {status_col}, count(*) from {table} group by {status_col}").fetchall(),
                )
            tscol = "updated_at" if "updated_at" in cols else ("created_at" if "created_at" in cols else None)
            if status_col and tscol:
                try:
                    print(
                        f"{table}_2d",
                        tcur.execute(
                            f"select {status_col}, count(*) from {table} "
                            f"where {tscol} > datetime('now','-2 day') group by {status_col}"
                        ).fetchall(),
                    )
                except Exception as e:
                    print(f"{table}_2d_err", e)
    else:
        print("image_tasks.db missing")

    print("\n=== PROXY_NODES TABLE? ===")
    try:
        print(
            "proxy_nodes",
            cur.execute(
                "select name from sqlite_master where type='table' and name like '%proxy%' or name like '%node%' or name like '%cohort%' or name like '%lease%'"
            ).fetchall(),
        )
    except Exception as e:
        print("proxy_nodes_err", e)

    print("\n=== CONTAINER / DISK ===")
    os.system(
        'docker stats --no-stream --format "{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} net={{.NetIO}}" chatgpt2api-local'
    )
    os.system("df -h / | tail -1")
    os.system("du -sh /root/gptimage /root/gptimage/data /root/gptimage/backups 2>/dev/null")
    os.system("du -sh /root/gptimage/data/* 2>/dev/null | sort -h | tail -15")
    os.system("ls -lt /root/gptimage/backups 2>/dev/null | head -10")

    print("\n=== RECENT LOG HINTS (desensitized counts) ===")
    log_glob = sorted((ROOT / "data" / "runlogs").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    alt = sorted(Path("/root/gptimage").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    candidates = log_glob + alt
    docker_log = Path("/var/lib/docker/containers")
    print("runlog_candidates", [str(p) for p in candidates[:5]])
    # Prefer docker logs via docker
    os.system(
        "docker logs --since 24h chatgpt2api-local 2>&1 | "
        "python3 -c \"import sys,re; from collections import Counter; c=Counter(); "
        "keys=['account_deactivated','token invalidated','quota','timeout','429','403','401',"
        "'image_service_busy','timeout_pending','CLOSE_WAIT','Traceback','no available image quota']; "
        "n=0\n"
        "for line in sys.stdin:\n"
        " n+=1\n"
        " low=line.lower()\n"
        " for k in keys:\n"
        "  if k.lower() in low: c[k]+=1\n"
        "print('docker_log_lines_24h', n); print('keyword_counts', dict(c.most_common()))\""
    )


if __name__ == "__main__":
    main()
