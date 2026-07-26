#!/usr/bin/env python3
"""Query Panda image_tasks.db for conc10 run account distribution."""
import json
import sqlite3
import subprocess
import sys

RUN = sys.argv[1] if len(sys.argv) > 1 else "pipe-conc10-20260723T071912Z"

REMOTE_PY = f"""
import json
import sqlite3

run = {RUN!r}
conn = sqlite3.connect("/app/data/image_tasks.db")
cur = conn.cursor()
cur.execute(
    "SELECT task_id, data FROM image_tasks WHERE task_id LIKE ? ORDER BY task_id",
    (run + "%",),
)
rows = []
for tid, raw in cur.fetchall():
    d = json.loads(raw)
    tok = str(d.get("resume_access_token") or "")
    rows.append({{
        "slot": tid.split("-")[-1],
        "task_id": tid,
        "token_prefix": tok[:24] if tok else "",
        "created_ts": d.get("created_ts"),
        "started_ts": d.get("started_ts"),
        "phase": d.get("phase_timings_ms") or {{}},
    }})
print(json.dumps(rows, ensure_ascii=False))
"""

def main() -> None:
    proc = subprocess.run(
        ["ssh", "panda", "docker", "exec", "-i", "chatgpt2api-local", "python3"],
        input=REMOTE_PY,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        sys.exit(proc.returncode)
    rows = json.loads(proc.stdout.strip())
    # resolve emails via accounts store on Panda
    email_py = """
import json,sqlite3
conn=sqlite3.connect("/app/data/accounts.db")
cur=conn.cursor()
cur.execute("SELECT access_token,email FROM accounts")
print(json.dumps({r[0]: r[1] for r in cur.fetchall() if r[0]}))
"""
    email_proc = subprocess.run(
        ["ssh", "panda", "docker", "exec", "-i", "chatgpt2api-local", "python3"],
        input=email_py,
        capture_output=True,
        text=True,
        check=False,
    )
    token_emails: dict[str, str] = {}
    if email_proc.returncode == 0 and email_proc.stdout.strip():
        token_emails = json.loads(email_proc.stdout.strip())

    tokens: dict[str, list[str]] = {}
    for row in rows:
        pref = row["token_prefix"] or "(none)"
        tokens.setdefault(pref, []).append(row["slot"])
    print(f"run={RUN} tasks={len(rows)} unique_accounts={len(tokens)}")
    for pref, slots in sorted(tokens.items(), key=lambda x: -len(x[1])):
        email = ""
        for tok, em in token_emails.items():
            if tok.startswith(pref) or pref.startswith(tok[:24]):
                email = em
                break
        label = f"{email} ({pref})" if email else pref
        print(f"  {label} -> slots {','.join(sorted(slots))} (n={len(slots)})")
    print("\nper-slot:")
    for row in rows:
        pt = row["phase"] or {}
        created = row["created_ts"] or 0
        started = row["started_ts"] or 0
        wait_s = round(started - created, 2) if started and created else None
        print(
            f"  #{row['slot']}: wait_to_started={wait_s}s "
            f"ss_queue={pt.get('ss_queue_ms')} ss_ms={pt.get('ss_ms')} "
            f"token={row['token_prefix'][:16]}..."
        )


if __name__ == "__main__":
    main()
