#!/usr/bin/env bash
# Deploy nurture/heatmap overlay onto Panda /root/gptimage (bind-mount production).
set -euo pipefail

ROOT=/root/gptimage
ARTIFACT_ROOT=$(cd "$(dirname "$0")" && pwd)
EXPECTED_COMMIT=${1:?expected artifact commit is required}
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$ROOT/backups/git-artifacts-deploy-$TS"
DEPLOYED=0

PY_FILES=(
  services/ip_nurture_schedule.py
  services/text_nurture_service.py
  services/config.py
  services/account_service.py
  services/risk_dashboard_service.py
  api/ops.py
)

rollback_on_error() {
  rc=$?
  if [ "$DEPLOYED" = "1" ]; then
    echo "ROLLBACK_ON_ERROR rc=$rc"
    for rel in "${PY_FILES[@]}"; do
      if [ -f "$BACKUP/$rel" ]; then
        install -m 0644 "$BACKUP/$rel" "$ROOT/$rel"
      fi
    done
    if [ -f "$BACKUP/web_dist.tgz" ]; then
      rm -rf "$ROOT/web_dist"
      tar -xzf "$BACKUP/web_dist.tgz" -C "$ROOT"
    fi
    if [ -f "$BACKUP/config.json" ]; then
      cp -a "$BACKUP/config.json" "$ROOT/config.json"
    fi
    docker restart chatgpt2api-local >/dev/null || true
    sleep 5
    curl -fsS --max-time 10 'http://127.0.0.1:8012/health?format=json' >/dev/null || true
  fi
  exit "$rc"
}
trap rollback_on_error ERR

ACTUAL_COMMIT=$(git -C "$ARTIFACT_ROOT" rev-parse HEAD)
test "$ACTUAL_COMMIT" = "$EXPECTED_COMMIT"

python3 - "$ARTIFACT_ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "deployment-manifest.json").read_text(encoding="utf-8"))
for relative_path, expected in manifest["files"].items():
    actual = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"hash mismatch: {relative_path}")
print("ARTIFACT_HASHES_OK", len(manifest["files"]))
PY

mkdir -p "$BACKUP/services" "$BACKUP/api"
for rel in "${PY_FILES[@]}"; do
  mkdir -p "$BACKUP/$(dirname "$rel")"
  cp -a "$ROOT/$rel" "$BACKUP/$rel"
done
tar -czf "$BACKUP/web_dist.tgz" -C "$ROOT" web_dist
cp -a "$ROOT/config.json" "$BACKUP/config.json"
curl -fsS --max-time 10 'http://127.0.0.1:8012/health?format=json' > "$BACKUP/health-before.json" || true
printf '%s\n' "$EXPECTED_COMMIT" > "$BACKUP/artifact-commit.txt"
echo "BACKUP=$BACKUP"

for rel in "${PY_FILES[@]}"; do
  install -m 0644 "$ARTIFACT_ROOT/$rel" "$ROOT/$rel.new-$TS"
  mv "$ROOT/$rel.new-$TS" "$ROOT/$rel"
done

rm -rf "$ROOT/web_dist.old-$TS"
if [ -d "$ROOT/web_dist" ]; then
  mv "$ROOT/web_dist" "$ROOT/web_dist.old-$TS"
fi
tar -xzf "$ARTIFACT_ROOT/web_dist.tgz" -C "$ROOT"
test -f "$ROOT/web_dist/index.html"

python3 - <<'PY'
import json
from pathlib import Path

path = Path("/root/gptimage/config.json")
data = json.loads(path.read_text(encoding="utf-8"))
data["text_chat_persist_history"] = True
data["text_chat_reuse_conversation"] = True
nurture = dict(data.get("text_nurture") or {})
nurture.update(
    {
        "enabled": True,
        "worker_enabled": True,
        "auto_enqueue": True,
        "auto_enqueue_every_sec": 120,
        "poll_interval_sec": 3,
        "max_per_hour": 70,
        "max_per_account_per_day": 8,
        "turns_per_session": 3,
        "turn_gap_sec": 8,
        "require_persist_history": True,
        "auto_enqueue_rotate_accounts": True,
    }
)
data["text_nurture"] = nurture
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("config_nurture_ok")
PY

DEPLOYED=1

python3 -m py_compile "${PY_FILES[@]/#/$ROOT/}" "$ROOT/api/ops.py"

docker exec \
  -e PYTHONPYCACHEPREFIX="/tmp/gptimage-pycache-$TS" \
  chatgpt2api-local \
  python3 -m py_compile \
  /app/services/ip_nurture_schedule.py \
  /app/services/text_nurture_service.py \
  /app/services/config.py \
  /app/services/account_service.py \
  /app/services/risk_dashboard_service.py \
  /app/api/ops.py

docker restart chatgpt2api-local >/dev/null
HEALTH_OK=0
for attempt in $(seq 1 20); do
  sleep 2
  if curl -fsS --max-time 8 'http://127.0.0.1:8012/health?format=json' > "$BACKUP/health-after.json" 2>/dev/null; then
    if python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("healthy"))' "$BACKUP/health-after.json" | grep -q True; then
      HEALTH_OK=1
      break
    fi
  fi
  echo "health_wait=$attempt"
done
test "$HEALTH_OK" = "1"

AUTH=$(python3 -c "import json; print(json.load(open('$ROOT/config.json')).get('auth-key',''))")
curl -fsS -H "Authorization: Bearer $AUTH" 'http://127.0.0.1:8012/api/ops/nurture/status' > "$BACKUP/nurture-status.json"
curl -fsS -H "Authorization: Bearer $AUTH" 'http://127.0.0.1:8012/api/ops/ip-nurture/presets' > "$BACKUP/ip-nurture-presets.json"
python3 - "$BACKUP" <<'PY'
import json
import sys
from pathlib import Path

backup = Path(sys.argv[1])
nurture = json.loads((backup / "nurture-status.json").read_text(encoding="utf-8"))
presets = json.loads((backup / "ip-nurture-presets.json").read_text(encoding="utf-8"))
print("nurture_enabled", nurture.get("enabled"), "running", nurture.get("running"))
print("presets", len(presets.get("presets") or []))
assert len(presets.get("presets") or []) >= 25
assert nurture.get("enabled") is True
PY

DEPLOYED=0
echo "DEPLOY_COMMIT=$ACTUAL_COMMIT"
echo "BACKUP=$BACKUP"
jq '{healthy, total:.accounts.total, schedulable:.accounts.schedulable}' "$BACKUP/health-after.json" 2>/dev/null || cat "$BACKUP/health-after.json"
