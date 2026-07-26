#!/usr/bin/env bash
set -euo pipefail
ROOT=/root/gptimage
TMP=/tmp/gptimage-deploy
BACKUP="$ROOT/backups/quota-ui-unrestricted-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP" "$TMP"
cp -a "$ROOT/services/account_service.py" "$ROOT/services/config.py" "$BACKUP/" 2>/dev/null || true
tar -czf "$BACKUP/web_dist.tgz" -C "$ROOT" web_dist 2>/dev/null || true
echo "BACKUP=$BACKUP"
install -m 0644 "$TMP/account_service.py" "$ROOT/services/account_service.py"
install -m 0644 "$TMP/config.py" "$ROOT/services/config.py"
rm -rf "$ROOT/web_dist.new"
mkdir -p "$ROOT/web_dist.new"
tar -xzf "$TMP/web_dist-deploy.tgz" -C "$ROOT/web_dist.new"
mv "$ROOT/web_dist" "$ROOT/web_dist.bak.$(date +%Y%m%d-%H%M%S)"
mv "$ROOT/web_dist.new" "$ROOT/web_dist"
cd "$ROOT"
docker compose -f docker-compose.panda.yml restart
sleep 6
curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json' | python3 - <<'PY'
import json, sys
d = json.load(sys.stdin)
a = d.get("accounts") or {}
print(
    "healthy=", d.get("healthy"),
    "image_schedulable=", a.get("image_schedulable"),
    "dispatchable=", a.get("dispatchable_candidate_count"),
    "verified_quota=", a.get("verified_total_quota"),
    "available_image_quota=", a.get("available_image_quota"),
    "latest_quota_refresh_at=", a.get("latest_quota_refresh_at"),
)
PY
