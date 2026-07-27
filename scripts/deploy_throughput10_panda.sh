#!/usr/bin/env bash
# THROUGHPUT-10 compliant deploy: git fetch + path checkout + restart (no build on Panda).
set -euo pipefail

BRANCH="${1:-codex/throughput10-20260727}"
REMOTE="${DEPLOY_REMOTE:-deploy}"
PANDA_HOST="${PANDA_HOST:-panda}"
APP_DIR="${PANDA_APP_DIR:-/root/gptimage}"

echo "==> Panda: rescue staged drift (P29-1)"
ssh "${PANDA_HOST}" "cd ${APP_DIR} && \
  if git diff --cached --quiet; then echo 'no staged drift'; else \
    git commit -m 'chore: land staged prod drift before throughput10 deploy' || true; \
  fi"

echo "==> Panda: fetch ${REMOTE}/${BRANCH}"
ssh "${PANDA_HOST}" "cd ${APP_DIR} && git fetch ${REMOTE} ${BRANCH}"

echo "==> Panda: checkout paths"
ssh "${PANDA_HOST}" "cd ${APP_DIR} && git checkout FETCH_HEAD -- \
  api/ services/ utils/ scripts/ native/ web_dist/ docs/ test/ crates/"

echo "==> Panda: install proxy pools (if present on host)"
ssh "${PANDA_HOST}" "cd ${APP_DIR} && \
  if [ -f data/runlogs/webshare_residential_proxies.secret.txt ]; then \
    echo residential=\$(wc -l < data/runlogs/webshare_residential_proxies.secret.txt); \
  fi && \
  if [ -f data/runlogs/webshare_100_proxies.secret.txt ]; then \
    echo datacenter=\$(wc -l < data/runlogs/webshare_100_proxies.secret.txt); \
  fi"

echo "==> Panda: patch throughput config"
ssh "${PANDA_HOST}" "docker exec -w /app chatgpt2api-local /app/.venv/bin/python /app/scripts/patch_throughput10_config.py || true"

echo "==> Panda: restart containers (no compose up)"
ssh "${PANDA_HOST}" "docker restart chatgpt2api-local gptimage-gateway-rs-helper"

echo "==> Panda: health check"
sleep 10
ssh "${PANDA_HOST}" "curl -fsS 'http://127.0.0.1:8012/health?format=json' | python3 -m json.tool | head -40"

echo "==> Panda: record deploy commit"
ssh "${PANDA_HOST}" "cd ${APP_DIR} && git add -A && git commit -m 'deploy: ${BRANCH} throughput10 @ \$(git rev-parse --short FETCH_HEAD)' || true"

echo "Deploy complete."
