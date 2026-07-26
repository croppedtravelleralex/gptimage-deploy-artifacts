#!/usr/bin/env python3
import json, sqlite3, re, sys
c = sqlite3.connect(sys.argv[1])
hosts = set()
emails = []
for (raw,) in c.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    e = str(d.get("email") or "").lower()
    if e:
        emails.append(e)
    p = str(d.get("proxy") or "")
    m = re.search(r"@([^:/]+)", p)
    if m:
        hosts.add(m.group(1))
print(json.dumps({"count": len(emails), "outlook": [e for e in sorted(emails) if e.endswith("@outlook.com")], "hosts": sorted(hosts)}, ensure_ascii=False, indent=2))
