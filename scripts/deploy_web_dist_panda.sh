#!/usr/bin/env bash
# Safe Panda web_dist deploy: in-place rsync (no rm/mv) + mandatory container restart + smoke checks.
# Usage (on Panda):
#   ./scripts/deploy_web_dist_panda.sh /tmp/web_dist-deploy.tgz
#   ./scripts/deploy_web_dist_panda.sh   # verify only
set -euo pipefail

ROOT="${GPTIMAGE_ROOT:-/root/gptimage}"
COMPOSE_FILE="${GPTIMAGE_COMPOSE:-$ROOT/docker-compose.panda.yml}"
CONTAINER="${GPTIMAGE_CONTAINER:-chatgpt2api-local}"
SERVICE="${GPTIMAGE_SERVICE:-app}"
BASE_URL="${GPTIMAGE_BASE_URL:-http://127.0.0.1:8012}"
TARBALL="${1:-}"

verify_web_dist() {
  local label="$1"
  echo "== verify: $label =="
  docker exec "$CONTAINER" test -s /app/web_dist/index.html
  docker exec "$CONTAINER" test -s /app/web_dist/accounts/index.html
  for path in / /accounts /chat /login; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL$path")"
    echo "  GET $path -> $code"
    test "$code" = "200"
  done
  curl -fsS "$BASE_URL/health?format=json" | python3 -c 'import json,sys; d=json.load(sys.stdin); wd=d.get("web_dist") or {}; print("  health.ok=", d.get("healthy"), "web_dist.ok=", wd.get("ok"), "missing=", wd.get("missing"));
import sys
if not wd.get("ok"): sys.exit("web_dist health check failed")'
}

if [[ -z "$TARBALL" ]]; then
  verify_web_dist "current"
  exit 0
fi

if [[ ! -f "$TARBALL" ]]; then
  echo "tarball not found: $TARBALL" >&2
  exit 1
fi

BACKUP="$ROOT/backups/web_dist-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
tar -czf "$BACKUP/web_dist.tgz" -C "$ROOT" web_dist 2>/dev/null || true
echo "BACKUP=$BACKUP"

STAGING="$(mktemp -d /tmp/web_dist-stage.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT
tar -xzf "$TARBALL" -C "$STAGING"
test -f "$STAGING/index.html"

# In-place sync keeps the bind-mount inode stable; still restart to flush any stale handles.
rsync -a --delete "$STAGING"/ "$ROOT/web_dist/"

cd "$ROOT"
docker compose -f "$COMPOSE_FILE" restart "$SERVICE"
sleep 8
verify_web_dist "post-deploy"
echo "deploy_web_dist_ok=1"
