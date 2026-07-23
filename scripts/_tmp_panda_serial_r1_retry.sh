#!/usr/bin/env bash
set -euo pipefail

EMAIL=qaflowgq5wyuxhe9@proton.me
STAMP=$(date +%Y%m%d-%H%M%S)
OUT_HOST=/root/gptimage/data/runlogs/spa_repro/serial-r1-retry-${STAMP}
OUT_APP=/app/data/runlogs/spa_repro/serial-r1-retry-${STAMP}
mkdir -p "$OUT_HOST"

python3 - <<'PY'
import json
from pathlib import Path
j = json.loads(Path("/root/gptimage/config.json").read_text(encoding="utf-8"))
Path("/tmp/panda_auth.key").write_text(j.get("auth-key") or j.get("auth_key") or "")
PY
AUTH=$(cat /tmp/panda_auth.key)
curl -fsS --max-time 30 -H "Authorization: Bearer ${AUTH}" \
  "http://127.0.0.1:8012/api/accounts?include_items=true" -o /tmp/acc_pre.json
python3 - <<PY
import json
email = "${EMAIL}".lower()
items = json.load(open("/tmp/acc_pre.json", encoding="utf-8")).get("items") or []
a = next(x for x in items if str(x.get("email") or "").lower() == email)
print("PREFLIGHT")
print("email", a.get("email"))
print("quota", a.get("quota"))
print("status", a.get("status"))
print("recv", a.get("panda_receive_state"))
print("egress", a.get("proxy_egress_ip"))
print("provider", a.get("proxy_provider"))
print("proxy", a.get("proxy"))
print("cf_daily", a.get("cf_daily"))
PY

# kill any leftover load test
pkill -f "spa_image_load_test.py" 2>/dev/null || true
sleep 1

echo "OUT_HOST=${OUT_HOST}"
echo "OUT_APP=${OUT_APP}"

# timeout INSIDE container so SSH drop cannot orphan the job
set +e
docker exec -e PYTHONUNBUFFERED=1 -e GPTIMAGE_ROOT=/app chatgpt2api-local \
  timeout 150s /app/.venv/bin/python /app/scripts/spa_image_load_test.py \
  --mode serial \
  --email "${EMAIL}" \
  --rounds 1 \
  --out-dir "${OUT_APP}" >"${OUT_HOST}/console.log" 2>&1
RC=$?
set -e
echo "exit=${RC}"

echo "=== console ==="
cat "${OUT_HOST}/console.log" || true
echo "=== summary ==="
if [ -f "${OUT_HOST}/summary.json" ]; then cat "${OUT_HOST}/summary.json"; else echo NO_SUMMARY; fi
echo "=== rows ==="
if [ -f "${OUT_HOST}/rows.json" ]; then python3 -m json.tool "${OUT_HOST}/rows.json" | head -n 160; else echo NO_ROWS; fi

curl -fsS --max-time 30 -H "Authorization: Bearer ${AUTH}" \
  "http://127.0.0.1:8012/api/accounts?include_items=true" -o /tmp/acc_post.json
python3 - <<PY
import json
email = "${EMAIL}".lower()
items = json.load(open("/tmp/acc_post.json", encoding="utf-8")).get("items") or []
a = next(x for x in items if str(x.get("email") or "").lower() == email)
print("POSTFLIGHT")
print("quota", a.get("quota"))
print("status", a.get("status"))
print("cf_daily", a.get("cf_daily"))
print("success", a.get("success"))
print("fail", a.get("fail"))
PY

# classify
python3 - <<PY
from pathlib import Path
text = Path("${OUT_HOST}/console.log").read_text(encoding="utf-8", errors="ignore")
print("HAS_SSE_DONE", "sse_done" in text)
print("HAS_HOME_SOFT", "home_soft_fail" in text)
print("HAS_OK_TRUE", '"ok": true' in text or '"ok":true' in text)
print("EXIT_HINT", "timeout" if ${RC} in (124, 137) else ("ok" if ${RC}==0 else "fail"))
PY
