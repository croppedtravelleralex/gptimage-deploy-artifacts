#!/usr/bin/env bash
# 一次性运维辅助：全量 CF scan（auto_quarantine 已在 config 中临时关闭）
set -u
cd /root/gptimage || exit 1
K=$(python3 -c 'import json;c=json.load(open("config.json"));print(c.get("auth-key") or c.get("auth_key"))')
BASE=http://127.0.0.1:8012

echo "=== scan 前 status ==="
curl -s "$BASE/api/ops/webshare-cf-scan/status" -H "Authorization: Bearer $K" \
  | python3 -m json.tool 2>/dev/null | head -30

echo "=== 触发 run-once (force) ==="
curl -s -X POST "$BASE/api/ops/webshare-cf-scan/run-once" -H "Authorization: Bearer $K" \
  | python3 -m json.tool 2>/dev/null | head -60

echo "=== scan 后 status ==="
curl -s "$BASE/api/ops/webshare-cf-scan/status" -H "Authorization: Bearer $K" \
  | python3 -m json.tool 2>/dev/null | head -30

echo "=== scan 后账号供给 ==="
curl -s "$BASE/health?format=json" | python3 -c '
import json,sys
a=json.load(sys.stdin).get("accounts",{})
for k in ["schedulable","image_schedulable","ready_candidate_count","dispatchable_candidate_count","total_quota"]:
    print(f"{k:30s} = {a.get(k)}")
'
