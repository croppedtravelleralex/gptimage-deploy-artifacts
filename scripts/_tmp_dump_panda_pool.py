#!/usr/bin/env python3
import json, sqlite3, re, sys
c = sqlite3.connect(sys.argv[1])
emails = sorted({json.loads(r[0] or "{}").get("email", "").lower() for r in c.execute("select data from accounts") if json.loads(r[0] or "{}").get("email")})
hosts = set()
for (raw,) in c.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    p = str(d.get("proxy") or "")
    m = re.search(r"@([^:/]+)", p)
    if m:
        hosts.add(m.group(1))
print(json.dumps({"emails": emails, "hosts": sorted(hosts)}, ensure_ascii=False, indent=2))
