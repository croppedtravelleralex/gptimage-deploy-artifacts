#!/usr/bin/env python3
import json
import subprocess

REMOTE = "panda"
REMOTE_DIR = "/root/gptimage"


def remote(cmd: str) -> str:
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr or p.stdout)
    return (p.stdout or "").strip()


auth = remote(f"python3 -c \"import json; print(json.load(open('{REMOTE_DIR}/config.json')).get('auth-key',''))\"")
warmup = json.loads(remote(f"curl -fsS --max-time 25 -H 'Authorization: Bearer {auth}' 'http://127.0.0.1:8012/api/ops/warmup/status'"))
health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
accounts = json.loads(remote(f"curl -fsS --max-time 30 -H 'Authorization: Bearer {auth}' 'http://127.0.0.1:8012/api/accounts'"))

rows = []
for acc in accounts if isinstance(accounts, list) else accounts.get("accounts", []):
    if not isinstance(acc, dict):
        continue
    email = str(acc.get("email") or "")
    cf = acc.get("cf_daily") or []
    today = cf[-1] if cf else {}
    rows.append({
        "email": email,
        "schedulable": acc.get("image_schedulable"),
        "hot": email.lower() in {e.lower() for e in warmup.get("hot", [])},
        "cf_today_ok": today.get("ok", 0) if isinstance(today, dict) else 0,
        "cf_today_cf": today.get("cf", 0) if isinstance(today, dict) else 0,
        "proxy": (acc.get("proxy_binding") or acc.get("proxy") or "")[:40],
    })

rows.sort(key=lambda r: (-int(r["hot"]), -int(r["cf_today_cf"]), r["email"]))

print("=== WARMUP ===")
print(json.dumps({
    "enabled": warmup.get("enabled"),
    "worker_alive": warmup.get("worker_alive"),
    "hot_count": warmup.get("hot_count"),
    "hot": warmup.get("hot"),
    "totals": warmup.get("totals"),
    "last_error": warmup.get("last_error"),
    "demoted_until": warmup.get("demoted_until"),
    "settings": warmup.get("settings"),
}, ensure_ascii=False, indent=2))

print("\n=== HEALTH ===")
a = health.get("accounts") or {}
print(json.dumps({
    "healthy": health.get("healthy"),
    "image_schedulable": a.get("image_schedulable"),
    "dispatchable": a.get("dispatchable_candidate_count"),
    "inflight": a.get("inflight"),
}, ensure_ascii=False, indent=2))

print("\n=== ACCOUNTS CF TODAY (schedulable) ===")
for r in rows:
    if not r.get("schedulable"):
        continue
    mark = "HOT" if r["hot"] else "   "
    print(f"{mark} ok={r['cf_today_ok']:>3} cf={r['cf_today_cf']:>3}  {r['email']}")

ok_accounts = [r for r in rows if r.get("schedulable") and int(r["cf_today_ok"]) > 0 and int(r["cf_today_cf"]) == 0]
cf_accounts = [r for r in rows if r.get("schedulable") and int(r["cf_today_cf"]) > 0]
print("\n=== SUMMARY ===")
print(json.dumps({
    "schedulable_with_probe_ok": len(ok_accounts),
    "schedulable_with_cf_hits": len(cf_accounts),
    "hot_pool": warmup.get("hot_count"),
    "ticks": (warmup.get("totals") or {}).get("ticks"),
    "warmed_promotions": (warmup.get("totals") or {}).get("warmed"),
    "warmup_errors": (warmup.get("totals") or {}).get("errors"),
    "rotated": (warmup.get("totals") or {}).get("rotated"),
}, ensure_ascii=False, indent=2))
