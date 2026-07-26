import json
from pathlib import Path

lines = Path("/root/gptimage/data/logs.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
for raw in reversed(lines[-8000:]):
    try:
        item = json.loads(raw)
    except Exception:
        continue
    if item.get("type") != "call":
        continue
    summary = str(item.get("summary") or "")
    if "文生图" not in summary:
        continue
    if "06:28" not in str(item.get("time") or ""):
        continue
    d = item.get("detail") or {}
    print("===", item.get("time"), summary)
    print(json.dumps(d, ensure_ascii=False, indent=2)[:2000])
    print()
