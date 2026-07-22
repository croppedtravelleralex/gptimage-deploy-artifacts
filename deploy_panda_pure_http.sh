#!/usr/bin/env bash
set -euo pipefail

ROOT=/root/gptimage
ARTIFACT_ROOT=$(cd "$(dirname "$0")" && pwd)
EXPECTED_COMMIT=${1:?expected artifact commit is required}
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$ROOT/backups/pure-http-prod-$TS"
DEPLOYED=0

rollback_on_error() {
  rc=$?
  if [ "$DEPLOYED" = "1" ]; then
    echo "ROLLBACK_ON_ERROR rc=$rc"
    cp -a "$BACKUP/services/openai_backend_api.py" "$ROOT/services/openai_backend_api.py"
    cp -a "$BACKUP/services/protocol/chatgpt_web_request.py" "$ROOT/services/protocol/chatgpt_web_request.py"
    cp -a "$BACKUP/services/config.py" "$ROOT/services/config.py"
    cp -a "$BACKUP/utils/turnstile.py" "$ROOT/utils/turnstile.py"
    docker restart chatgpt2api-local >/dev/null || true
    sleep 4
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

mkdir -p "$BACKUP/services/protocol" "$BACKUP/utils"
cp -a "$ROOT/services/openai_backend_api.py" "$BACKUP/services/openai_backend_api.py"
cp -a "$ROOT/services/protocol/chatgpt_web_request.py" "$BACKUP/services/protocol/chatgpt_web_request.py"
cp -a "$ROOT/services/config.py" "$BACKUP/services/config.py"
cp -a "$ROOT/utils/turnstile.py" "$BACKUP/utils/turnstile.py"
curl -fsS --max-time 10 'http://127.0.0.1:8012/health?format=json' > "$BACKUP/health-before.json"
printf '%s\n' "$EXPECTED_COMMIT" > "$BACKUP/artifact-commit.txt"
echo "BACKUP=$BACKUP"

install -m 0644 "$ARTIFACT_ROOT/services/openai_backend_api.py" "$ROOT/services/openai_backend_api.py.new-$TS"
install -m 0644 "$ARTIFACT_ROOT/services/protocol/chatgpt_web_request.py" "$ROOT/services/protocol/chatgpt_web_request.py.new-$TS"
install -m 0644 "$ARTIFACT_ROOT/services/config.py" "$ROOT/services/config.py.new-$TS"
install -m 0644 "$ARTIFACT_ROOT/utils/turnstile.py" "$ROOT/utils/turnstile.py.new-$TS"
mv "$ROOT/services/openai_backend_api.py.new-$TS" "$ROOT/services/openai_backend_api.py"
mv "$ROOT/services/protocol/chatgpt_web_request.py.new-$TS" "$ROOT/services/protocol/chatgpt_web_request.py"
mv "$ROOT/services/config.py.new-$TS" "$ROOT/services/config.py"
mv "$ROOT/utils/turnstile.py.new-$TS" "$ROOT/utils/turnstile.py"
DEPLOYED=1

python3 -m py_compile \
  "$ROOT/services/openai_backend_api.py" \
  "$ROOT/services/protocol/chatgpt_web_request.py" \
  "$ROOT/services/config.py" \
  "$ROOT/utils/turnstile.py"
grep -q 'CF edge block is a stop signal' "$ROOT/services/openai_backend_api.py"
grep -q 'chat_requirements_turnstile_required_but_unsolved' "$ROOT/services/openai_backend_api.py"
grep -q 'image_spa_tool_path' "$ROOT/services/config.py"
grep -q '_execute_program' "$ROOT/utils/turnstile.py"
sha256sum \
  "$ROOT/services/openai_backend_api.py" \
  "$ROOT/services/protocol/chatgpt_web_request.py" \
  "$ROOT/services/config.py" \
  "$ROOT/utils/turnstile.py" | tee "$BACKUP/sha256-after.txt"

docker exec \
  -e PYTHONPYCACHEPREFIX="/tmp/gptimage-pycache-$TS" \
  chatgpt2api-local \
  python -m py_compile \
  /app/services/openai_backend_api.py \
  /app/services/protocol/chatgpt_web_request.py \
  /app/services/config.py \
  /app/utils/turnstile.py

docker restart chatgpt2api-local >/dev/null
HEALTH_OK=0
for attempt in $(seq 1 15); do
  sleep 2
  if curl -fsS --max-time 8 'http://127.0.0.1:8012/health?format=json' > "$BACKUP/health-after.json" 2>/dev/null; then
    if jq -e '.healthy == true' "$BACKUP/health-after.json" >/dev/null; then
      HEALTH_OK=1
      break
    fi
  fi
  echo "health_wait=$attempt"
done
test "$HEALTH_OK" = "1"

jq '{healthy, total:.accounts.total, schedulable:.accounts.schedulable, image_schedulable:.accounts.image_schedulable, dispatchable:.accounts.dispatchable_candidate_count, inflight:.accounts.image_inflight_count}' "$BACKUP/health-after.json"
docker logs chatgpt2api-local --tail 60 2>&1 | python3 -c 'import sys; lines=sys.stdin.read().splitlines(); bad=[line for line in lines if any(marker in line for marker in ("Traceback", "ImportError", "ModuleNotFoundError"))]; print("startup_errors", len(bad)); [print(line) for line in bad[:8]]'

DEPLOYED=0
echo "DEPLOY_COMMIT=$ACTUAL_COMMIT"
echo "ROLLBACK=cp -a $BACKUP/services/openai_backend_api.py $ROOT/services/openai_backend_api.py && cp -a $BACKUP/services/protocol/chatgpt_web_request.py $ROOT/services/protocol/chatgpt_web_request.py && cp -a $BACKUP/services/config.py $ROOT/services/config.py && cp -a $BACKUP/utils/turnstile.py $ROOT/utils/turnstile.py && docker restart chatgpt2api-local"
