#!/usr/bin/env python3
"""Per-task account + proxy egress for conc10 failure run."""
import json
import subprocess
import sys

RUN = sys.argv[1] if len(sys.argv) > 1 else "pipe-conc10-20260723T085640Z"

REMOTE = f"""
import json, sqlite3
from pathlib import Path

run = {RUN!r}
conn = sqlite3.connect("/app/data/accounts.db")
email_by_token = {{}}
for row in conn.execute("SELECT access_token, email, data FROM accounts"):
    tok, email, raw = row[0], row[1] or "", row[2] or "{{}}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else {{}}
    except Exception:
        data = {{}}
    email_by_token[tok] = {{
        "email": email,
        "proxy_binding_hash": data.get("proxy_binding_hash") or "",
        "proxy_url": (data.get("proxy_url") or data.get("proxy") or "")[:80],
    }}

conn2 = sqlite3.connect("/app/data/image_tasks.db")
cur = conn2.cursor()
cur.execute("SELECT task_id, data FROM image_tasks WHERE task_id LIKE ? ORDER BY task_id", (run + "%",))
for tid, raw in cur.fetchall():
    d = json.loads(raw)
    tok = d.get("resume_access_token") or ""
    meta = email_by_token.get(tok, {{}})
    print(json.dumps({{
        "slot": tid.split("-")[-1],
        "status": d.get("status"),
        "error": (d.get("error") or "")[:160],
        "preferred": d.get("preferred_account_email") or "",
        "email": meta.get("email") or "",
        "binding": (meta.get("proxy_binding_hash") or "")[:16],
        "proxy": meta.get("proxy_url") or "",
    }}, ensure_ascii=False))
"""

proc = subprocess.run(
    ["ssh", "panda", "docker", "exec", "-i", "chatgpt2api-local", "python3"],
    input=REMOTE,
    capture_output=True,
    text=True,
    errors="replace",
)
if proc.returncode != 0:
    print(proc.stderr, file=sys.stderr)
    sys.exit(proc.returncode)
for line in proc.stdout.splitlines():
    if line.strip():
        print(line)
