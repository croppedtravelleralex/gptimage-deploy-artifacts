#!/usr/bin/env bash
set -euo pipefail
cd /root/gptimage
UPSTREAM="${1:?upstream required}"
pkill -f '_tmp_http_upstream_forwarder.py' || true
sleep 1
nohup python3 scripts/_tmp_http_upstream_forwarder.py 127.0.0.1 18443 "$UPSTREAM" > /tmp/chain_forwarder_18443.log 2>&1 &
sleep 2
ss -lntp | grep 18443 || true
python3 - <<'PY'
from curl_cffi import requests
proxies = {'http': 'http://127.0.0.1:18443', 'https': 'http://127.0.0.1:18443'}
r = requests.get('https://chatgpt.com/api/auth/csrf', proxies=proxies, timeout=30, impersonate='chrome')
print('csrf', r.status_code, r.text[:80])
PY
