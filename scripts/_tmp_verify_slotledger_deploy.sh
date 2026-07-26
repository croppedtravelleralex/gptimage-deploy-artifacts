#!/usr/bin/env bash
set -euo pipefail
curl -fsS 'http://127.0.0.1:8012/health?format=json' > /tmp/health-slotledger.json
python3 <<'PY'
import json
from pathlib import Path
d = json.loads(Path("/tmp/health-slotledger.json").read_text())
w = d.get("pipeline_watchdog") or {}
lr = w.get("last_report") or w
sl = lr.get("slot_ledger") or {}
print("healthy", d.get("healthy"))
print("version", d.get("version"))
print("slot_ledger_backend", sl.get("backend"))
print("slot_ledger_stats", sl)
print("pre_ticket_pool", d.get("pre_ticket_pool"))
acc = d.get("accounts") or {}
rt = d.get("image_runtime") or {}
print("inflight", acc.get("image_inflight_count"), rt.get("image_inflight_count"))
print("dispatchable", rt.get("dispatchable_candidate_count"))
print("rss_mb", (d.get("process_memory") or {}).get("rss_mb"))
PY
docker exec chatgpt2api-local ls -la /app/native/
docker exec chatgpt2api-local env PYTHONPATH=/app /app/.venv/bin/python -c "from services.image_pipeline.slot_ledger import slot_ledger; print('snapshot', slot_ledger.snapshot())"
