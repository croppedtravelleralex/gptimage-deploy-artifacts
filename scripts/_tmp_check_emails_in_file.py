#!/usr/bin/env python3
import sys
from pathlib import Path

targets = {e.strip().lower() for e in sys.argv[1:] if e.strip()}
path = Path("/root/gptimage/data/runlogs/panda-outlook-recovery.credentials.secret.txt")
text = path.read_text(encoding="utf-8-sig", errors="ignore").lower()
for tgt in sorted(targets):
    print(f"{tgt}: {tgt in text}")
