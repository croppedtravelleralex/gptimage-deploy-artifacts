#!/usr/bin/env bash
# Deploy schedule-core optimization overlay onto Panda /root/gptimage (bind-mount production).
set -euo pipefail

ROOT=/root/gptimage
ARTIFACT_ROOT=$(cd "$(dirname "$0")" && pwd)
EXPECTED_COMMIT=${1:?expected artifact commit is required}
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$ROOT/backups/schedule-core-deploy-$TS"
DEPLOYED=0

PY_FILES=(
  services/image_pipeline/schedule_trace.py
  services/image_pipeline/schedule_trace_model.py
  services/image_pipeline/schedule_core.py
  services/image_pipeline/account_lease_pool.py
  services/image_pipeline/account_provider.py
  services/image_pipeline/orchestrator.py
  services/image_task_service.py
  services/protocol/conversation.py
  services/openai_backend_api.py
  services/config.py
  services/storage/database_storage.py
  api/system.py
  utils/process_memory.py
)

NATIVE_LIBS=(
  libimage_schedule_trace.so
  libimage_schedule_core.so
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
    for name in "${NATIVE_LIBS[@]}"; do
      if [ -f "$BACKUP/native/$name" ]; then
        install -m 0644 "$BACKUP/native/$name" "$ROOT/native/$name"
      fi
    done
    if [ -f "$BACKUP/docker-compose.panda.yml" ]; then
      cp -a "$BACKUP/docker-compose.panda.yml" "$ROOT/docker-compose.panda.yml"
    fi
    docker compose -f "$ROOT/docker-compose.panda.yml" up -d --force-recreate >/dev/null || true
    sleep 8
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

mkdir -p "$BACKUP/services/image_pipeline" "$BACKUP/services/protocol" "$BACKUP/native"
for rel in "${PY_FILES[@]}"; do
  if [ -f "$ROOT/$rel" ]; then
    mkdir -p "$BACKUP/$(dirname "$rel")"
    cp -a "$ROOT/$rel" "$BACKUP/$rel"
  fi
done
for name in "${NATIVE_LIBS[@]}"; do
  if [ -f "$ROOT/native/$name" ]; then
    cp -a "$ROOT/native/$name" "$BACKUP/native/$name"
  fi
done
cp -a "$ROOT/docker-compose.panda.yml" "$BACKUP/docker-compose.panda.yml"
curl -fsS --max-time 10 'http://127.0.0.1:8012/health?format=json' > "$BACKUP/health-before.json" || true
printf '%s\n' "$EXPECTED_COMMIT" > "$BACKUP/artifact-commit.txt"
echo "BACKUP=$BACKUP"

for rel in "${PY_FILES[@]}"; do
  mkdir -p "$ROOT/$(dirname "$rel")"
  install -m 0644 "$ARTIFACT_ROOT/$rel" "$ROOT/$rel.new-$TS"
  mv "$ROOT/$rel.new-$TS" "$ROOT/$rel"
done
mkdir -p "$ROOT/native"
for name in "${NATIVE_LIBS[@]}"; do
  install -m 0644 "$ARTIFACT_ROOT/native/$name" "$ROOT/native/$name.new-$TS"
  mv "$ROOT/native/$name.new-$TS" "$ROOT/native/$name"
done

COMPOSE="$ROOT/docker-compose.panda.yml"
if ! grep -q './native:/app/native' "$COMPOSE"; then
  sed -i '/- \.\/scripts:\/app\/scripts:ro/a\      - ./native:/app/native:ro' "$COMPOSE"
  echo "compose_native_mount_added"
fi

DEPLOYED=1

python3 -m py_compile "${PY_FILES[@]/#/$ROOT/}"

docker compose -f "$COMPOSE" up -d --force-recreate
HEALTH_OK=0
for attempt in $(seq 1 25); do
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

DEPLOYED=0
echo "DEPLOY_COMMIT=$ACTUAL_COMMIT"
echo "BACKUP=$BACKUP"
jq '{healthy, total:.accounts.total, schedulable:.accounts.schedulable}' "$BACKUP/health-after.json" 2>/dev/null || cat "$BACKUP/health-after.json"
