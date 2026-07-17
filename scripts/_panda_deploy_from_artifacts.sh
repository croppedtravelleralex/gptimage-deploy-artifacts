#!/bin/bash
# Deploy selected paths from a one-shot clone of gptimage-deploy-artifacts.
# Expects: ARTIFACT_TOKEN in env. Does not print the token.
set -euo pipefail

ROOT=/root/gptimage
TS=$(date +%Y%m%d-%H%M%S)
BACKUP=$ROOT/backups/git-artifacts-deploy-$TS
CLONE=/tmp/gptimage-deploy-artifacts-$TS

mkdir -p "$BACKUP"
cp -a "$ROOT/services" "$BACKUP/services"
cp -a "$ROOT/api" "$BACKUP/api"
if [ -d "$ROOT/web_dist" ]; then
  tar -czf "$BACKUP/web_dist.tgz" -C "$ROOT" web_dist
fi
cp -a "$ROOT/docker-compose.panda.yml" "$BACKUP/" 2>/dev/null || true
curl -fsS --max-time 15 'http://127.0.0.1:8012/health?format=json' > "$BACKUP/health-before.json" || true
echo "BACKUP=$BACKUP"

test -n "${ARTIFACT_TOKEN:-}" || { echo "ARTIFACT_TOKEN missing"; exit 1; }

git clone --depth 1 "https://x-access-token:${ARTIFACT_TOKEN}@github.com/croppedtravelleralex/gptimage-deploy-artifacts.git" "$CLONE"
# scrub remote URL so token is not left in clone config
git -C "$CLONE" remote set-url origin https://github.com/croppedtravelleralex/gptimage-deploy-artifacts.git

# Overlay deployable paths (no --delete: keep any Panda-only files)
rsync -a "$CLONE/services/" "$ROOT/services/"
rsync -a "$CLONE/api/" "$ROOT/api/"
if [ -d "$CLONE/web_dist" ]; then
  rm -rf "$ROOT/web_dist.new"
  mkdir -p "$ROOT/web_dist.new"
  rsync -a "$CLONE/web_dist/" "$ROOT/web_dist.new/"
  if [ -d "$ROOT/web_dist" ]; then
    mv "$ROOT/web_dist" "$ROOT/web_dist.bak.$TS"
  fi
  mv "$ROOT/web_dist.new" "$ROOT/web_dist"
fi

# Optional scripts used operationally
mkdir -p "$ROOT/scripts"
for f in repair_panda_account_identity.py panda_readonly_deep_audit.py account_identity_baseline.py export_panda_identity_snapshot.sh panda_canary_observe.py panda_isolate_binding_peers.py panda_isolation_preflight.py panda_canary_me_gate.py recover_panda_outlook_accounts.py run_plan_matrix_local.py; do
  if [ -f "$CLONE/scripts/$f" ]; then
    cp -a "$CLONE/scripts/$f" "$ROOT/scripts/$f"
  fi
done

rm -rf "$CLONE"

cd "$ROOT"
python3 -m py_compile \
  services/account_fingerprint.py \
  services/account_identity.py \
  services/account_service.py \
  services/openai_backend_api.py \
  services/request_shape.py \
  services/request_phase.py \
  services/proxy_url_utils.py \
  services/proxy_service.py \
  services/account_refresh_all_service.py \
  services/account_workload_policy.py \
  services/account_workload_policy_service.py \
  services/text_task_queue.py \
  services/protocol/chatgpt_web_request.py \
  services/config.py \
  api/accounts.py \
  api/system.py
docker compose -f docker-compose.panda.yml up -d --force-recreate app
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 3
  if curl -fsS --max-time 15 'http://127.0.0.1:8012/health?format=json' >/tmp/gptimage-health-after.json 2>/dev/null; then
    break
  fi
  echo "health_wait_$i"
done
docker ps --filter name=chatgpt2api-local --format '{{.Names}} {{.Status}}'
python3 -c "import json; d=json.load(open('/tmp/gptimage-health-after.json')); a=d.get('accounts') or {}; print('healthy', d.get('healthy')); print('total', a.get('total')); print('schedulable', a.get('schedulable')); print('inflight', a.get('image_inflight_count')); print('workload', d.get('workload'))"
docker logs chatgpt2api-local --tail 40 2>&1 | python3 -c "import sys; lines=sys.stdin.read().splitlines(); bad=[l for l in lines if 'ImportError' in l or 'ModuleNotFoundError' in l or 'Traceback' in l]; print('startup_errors', len(bad));
[print(x) for x in bad[:12]]"
test -f "$ROOT/services/account_fingerprint.py" && echo fingerprint=PRESENT
test -f "$ROOT/services/protocol/chatgpt_web_request.py" && echo builder=PRESENT
test -f "$ROOT/web_dist/web_dist-manifest.json" && echo manifest=PRESENT
# recover-outlook must exist after api overlay
python3 - <<'PY'
import urllib.request
req=urllib.request.Request('http://127.0.0.1:8012/api/accounts/recover-outlook', data=b'{}', method='POST', headers={'Content-Type':'application/json'})
try:
    urllib.request.urlopen(req, timeout=10)
except Exception as e:
    code=getattr(e,'code',None)
    print('recover_outlook_http', code)
    if code == 405:
        raise SystemExit('recover-outlook still 405')
PY
echo "ROLLBACK=cp -a $BACKUP/services/. $ROOT/services/ && cp -a $BACKUP/api/. $ROOT/api/ && (test -f $BACKUP/web_dist.tgz && tar -xzf $BACKUP/web_dist.tgz -C $ROOT) && cd $ROOT && docker compose -f docker-compose.panda.yml up -d --force-recreate app"
