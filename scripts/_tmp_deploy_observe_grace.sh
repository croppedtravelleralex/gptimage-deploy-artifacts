#!/usr/bin/env bash
set -euo pipefail
ROOT=/root/gptimage
TMP=/tmp/gptimage-deploy-observe
BACKUP="$ROOT/backups/observe-grace-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP/services" "$BACKUP/scripts"
cp -a "$ROOT/services/account_service.py" "$BACKUP/services/" 2>/dev/null || true
cp -a "$ROOT/services/account_refresh_all_service.py" "$BACKUP/services/" 2>/dev/null || true
cp -a "$ROOT/scripts/_tmp_panda_import_observe_blob.py" "$BACKUP/scripts/" 2>/dev/null || true
tar -czf "$BACKUP/web_dist.tgz" -C "$ROOT" web_dist 2>/dev/null || true
echo "BACKUP=$BACKUP"

install -m 0644 "$TMP/account_service.py" "$ROOT/services/account_service.py"
install -m 0644 "$TMP/account_refresh_all_service.py" "$ROOT/services/account_refresh_all_service.py"
install -m 0644 "$TMP/_tmp_panda_import_observe_blob.py" "$ROOT/scripts/_tmp_panda_import_observe_blob.py"
install -m 0644 "$TMP/_tmp_delete_account_quarantine_proxy.py" "$ROOT/scripts/_tmp_delete_account_quarantine_proxy.py"

if [ -f "$TMP/web_dist-deploy.tgz" ]; then
  rm -rf "$ROOT/web_dist.new"
  mkdir -p "$ROOT/web_dist.new"
  tar -xzf "$TMP/web_dist-deploy.tgz" -C "$ROOT/web_dist.new"
  if [ -d "$ROOT/web_dist.new/web_dist" ]; then
    mv "$ROOT/web_dist" "$ROOT/web_dist.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mv "$ROOT/web_dist.new/web_dist" "$ROOT/web_dist"
  else
    mv "$ROOT/web_dist" "$ROOT/web_dist.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mv "$ROOT/web_dist.new" "$ROOT/web_dist"
  fi
fi

cd "$ROOT"
docker compose -f docker-compose.panda.yml up -d --force-recreate
sleep 10

docker cp "$ROOT/scripts/_tmp_delete_account_quarantine_proxy.py" chatgpt2api-local:/tmp/_tmp_delete_account_quarantine_proxy.py
docker exec chatgpt2api-local /app/.venv/bin/python /tmp/_tmp_delete_account_quarantine_proxy.py \
  --root /app \
  --email gibsonarthur3532@outlook.com \
  --reason account_deactivated

python3 "$ROOT/scripts/_tmp_reload_panda_accounts.py"

curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json' | python3 - <<'PY'
import json, sys
d = json.load(sys.stdin)
a = d.get("accounts") or {}
print(
    "healthy=", d.get("healthy"),
    "total=", a.get("total"),
    "image_schedulable=", a.get("image_schedulable"),
    "available_image_quota=", a.get("available_image_quota"),
)
PY
