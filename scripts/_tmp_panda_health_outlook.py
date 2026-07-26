#!/usr/bin/env python3
import json
import sys
import urllib.request

req = urllib.request.Request("http://127.0.0.1:8012/health?format=json")
with urllib.request.urlopen(req, timeout=30) as resp:
    d = json.loads(resp.read().decode("utf-8"))
accts = d.get("accounts") or []
outlook = [a for a in accts if "outlook" in str(a.get("email", "")).lower()]
print(json.dumps({"total": d.get("total"), "outlook_count": len(outlook), "accounts": outlook}, ensure_ascii=False, indent=2))
