#!/usr/bin/env bash
set -euo pipefail
cd /root/gptimage
UPSTREAM="${1:?upstream required}"
pkill -f '_tmp_http_upstream_forwarder.py' || true
sleep 1
nohup python3 scripts/_tmp_http_upstream_forwarder.py 127.0.0.1 18443 "$UPSTREAM" > /tmp/chain_forwarder_18443.log 2>&1 &
sleep 2
ss -lntp | grep 18443 || true
head -3 /tmp/chain_forwarder_18443.log
python3 - <<'PY'
from curl_cffi import requests
r=requests.get('https://api.ipify.org?format=json', proxies={'http':'http://127.0.0.1:18443','https':'http://127.0.0.1:18443'}, timeout=25, impersonate='chrome')
print('panda_chain', r.status_code, r.text)
r2=requests.get('https://chatgpt.com/api/auth/csrf', proxies={'http':'http://127.0.0.1:18443','https':'http://127.0.0.1:18443'}, timeout=30, impersonate='chrome')
print('csrf', r2.status_code, r2.text[:80])
PY
