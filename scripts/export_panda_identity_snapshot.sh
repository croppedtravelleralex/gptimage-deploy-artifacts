#!/bin/bash
# Read-only desensitized account snapshot for identity audit. No secrets/emails in output.
set -euo pipefail
python3 <<'PY'
import hashlib, json, sqlite3
from pathlib import Path
from urllib.parse import urlsplit, unquote

db = Path('/root/gptimage/data/accounts.db')
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
rows = con.execute('select data from accounts').fetchall()
out = []
for (raw,) in rows:
    try:
        d = json.loads(raw)
    except Exception:
        continue
    proxy = str(d.get('proxy') or '')
    host = ''
    user = ''
    if proxy:
        try:
            p = urlsplit(proxy if '://' in proxy else f'http://{proxy}')
            host = f'{p.hostname}:{p.port or 80}'
            user = unquote(p.username or '')
        except Exception:
            host = 'unparsed'
    token = str(d.get('access_token') or '')
    email = str(d.get('email') or '').strip().lower()
    item = {
        'access_token': 'tok_' + hashlib.sha256(token.encode()).hexdigest()[:24],
        'email': ('e_' + hashlib.sha256(email.encode()).hexdigest()[:16]) if email else None,
        'status': d.get('status'),
        'quota': d.get('quota'),
        'invalid_count': d.get('invalid_count'),
        'panda_receive_state': d.get('panda_receive_state'),
        'outlook_recovery_state': d.get('outlook_recovery_state'),
        'proxy': f'http://{user}:redacted@{host}' if host else '',
        'proxy_provider': d.get('proxy_provider'),
        'proxy_scope': d.get('proxy_scope'),
        'proxy_egress_hash': d.get('proxy_egress_hash'),
        'proxy_egress_ip': d.get('proxy_egress_ip'),
        'registration_proxy_hash': d.get('registration_proxy_hash'),
        'registration_proxy_scope': d.get('registration_proxy_scope'),
        'registration_proxy_endpoint': d.get('registration_proxy_endpoint'),
        'lifecycle_ip_mode': d.get('lifecycle_ip_mode'),
        'fp': d.get('fp') if isinstance(d.get('fp'), dict) else {},
        'success': d.get('success'),
        'fail': d.get('fail'),
        'created_at': d.get('created_at'),
        'last_used_at': d.get('last_used_at'),
        'restore_at': d.get('restore_at'),
        'traffic_total_bytes': d.get('traffic_total_bytes'),
    }
    out.append(item)
print(json.dumps({'accounts': out, 'total': len(out)}, ensure_ascii=False))
PY
