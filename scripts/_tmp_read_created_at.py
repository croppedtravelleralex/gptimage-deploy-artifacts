#!/usr/bin/env python3
import json, sqlite3, sys
from pathlib import Path
email = sys.argv[1].strip().lower()
db = Path(sys.argv[2])
c = sqlite3.connect(db)
for (raw,) in c.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    if str(d.get("email") or "").lower() == email:
        print(json.dumps({"created_at": d.get("created_at"), "updated_at": d.get("updated_at")}, ensure_ascii=False))
        break
