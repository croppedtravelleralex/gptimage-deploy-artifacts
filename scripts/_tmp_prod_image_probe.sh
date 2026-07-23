AK=$(cat /root/gptimage/data/runlogs/spa_repro/authkey.txt)
OUT=/root/gptimage/data/runlogs/spa_repro/prod_image_resp.json
echo "POST /v1/images/generations model=gpt-image-2"
code=$(curl -s -o "$OUT" -w '%{http_code}' -m 240 \
  http://127.0.0.1:8012/v1/images/generations \
  -H "Authorization: Bearer $AK" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-image-2","prompt":"A rainy Tokyo side street at dusk, neon signs reflecting on wet asphalt, cinematic, no text","n":1,"size":"1024x1024"}')
echo "HTTP $code  bytes=$(wc -c < "$OUT")"
python3 - "$OUT" <<'PY'
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception as e:
    print('NOT_JSON', str(e)[:200]); raise SystemExit
if isinstance(d, dict) and d.get('data'):
    for i, it in enumerate(d['data']):
        b = it.get('b64_json') or ''
        print('item', i, 'has_b64', bool(b), 'b64_len', len(b), 'url', str(it.get('url'))[:80])
else:
    print('KEYS', list(d.keys()) if isinstance(d, dict) else type(d))
    print(json.dumps(d, ensure_ascii=False)[:600])
PY
