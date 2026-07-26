#!/usr/bin/env python3
import collections
import json
import re
import subprocess

logs = subprocess.check_output(
    ["docker", "logs", "chatgpt2api-local", "--since", "15m"],
    stderr=subprocess.STDOUT,
    text=True,
    errors="replace",
)
promotes = re.findall(r'"event": "account_warmup_promote".*?"email": "([^"]+)"', logs)
fails = re.findall(r'"event": "account_warmup_fail".*?"email": "([^"]+)"', logs)
pc = collections.Counter(promotes)
fc = collections.Counter(fails)

print("=== LOG SUMMARY (15m) ===")
print(json.dumps({
    "promote_events": len(promotes),
    "promote_unique_accounts": len(pc),
    "fail_events": len(fails),
    "fail_unique_accounts": len(fc),
}, indent=2))
print("\n-- promoted (unique) --")
for email, count in pc.most_common():
    print(f"  {count}x  {email}")
print("\n-- top failures --")
for email, count in fc.most_common(15):
    print(f"  {count}x  {email}")
