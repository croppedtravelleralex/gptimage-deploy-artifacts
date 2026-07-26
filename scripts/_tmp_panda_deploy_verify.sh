#!/bin/bash
set -euo pipefail
cd /root/gptimage
docker restart chatgpt2api-local
sleep 8
echo "=== health ==="
curl -sS 'http://127.0.0.1:8012/health?format=json' | python3 -m json.tool | head -15
echo "=== nurture ==="
AUTH=$(python3 -c "import json; print(json.load(open('config.json')).get('auth-key',''))")
curl -sS -H "Authorization: Bearer $AUTH" 'http://127.0.0.1:8012/api/ops/nurture/status' | python3 -m json.tool
echo "=== ip-nurture presets ==="
curl -sS -H "Authorization: Bearer $AUTH" 'http://127.0.0.1:8012/api/ops/ip-nurture/presets' | python3 -c "import sys,json; d=json.load(sys.stdin); p=d.get('presets') or []; print('count', len(p)); print('first', p[0]['id'] if p else 'none')"
echo "=== manifest ==="
cat web_dist/web_dist-manifest.json
echo "=== container import ==="
docker exec chatgpt2api-local python3 -c "from services.ip_nurture_schedule import list_presets; from services.text_nurture_service import text_nurture_service; s=text_nurture_service.status(); print('presets', len(list_presets())); print('enabled', s.get('enabled'), 'running', s.get('running'), 'turns', s.get('turns_per_session'), 'daily_cap', s.get('max_per_account_per_day'))"
