#!/bin/bash
set -euo pipefail
cd /root/gptimage
BACKUP="/root/gptimage/backups/deploy-nurture-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -a services/account_service.py services/text_nurture_service.py services/config.py services/risk_dashboard_service.py api/ops.py config.json "$BACKUP/" 2>/dev/null || true
tar -czf "$BACKUP/web_dist.tgz" -C /root/gptimage web_dist
echo "BACKUP=$BACKUP"
