#!/usr/bin/env bash
set -eu

PROJECT_WSL_PATH="${1:-/mnt/d/SelfMadeTool/AutoRegister/gptimage}"
cd "$PROJECT_WSL_PATH"

HOST_PROXY_FORWARD_ENABLED="${GPTIMAGE_HOST_PROXY_FORWARD_ENABLED:-true}"
HOST_PROXY_LISTEN_HOST="${GPTIMAGE_HOST_PROXY_LISTEN_HOST:-0.0.0.0}"
HOST_PROXY_LISTEN_PORT="${GPTIMAGE_HOST_PROXY_LISTEN_PORT:-17897}"
HOST_PROXY_TARGET_HOST="${GPTIMAGE_HOST_PROXY_TARGET_HOST:-127.0.0.1}"
HOST_PROXY_TARGET_PORT="${GPTIMAGE_HOST_PROXY_TARGET_PORT:-7897}"
ALLOW_DOCKER_START="${GPTIMAGE_WSL_ALLOW_DOCKER_START:-false}"
DOCKERD_PROXY_URL="${GPTIMAGE_DOCKERD_PROXY_URL:-${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}}"
DOCKERD_NO_PROXY="${NO_PROXY:-${no_proxy:-172.31.*,172.30.*,172.29.*,172.28.*,172.27.*,172.26.*,172.25.*,172.24.*,172.23.*,172.22.*,172.21.*,172.20.*,172.19.*,172.18.*,172.17.*,172.16.*,10.*,192.168.*,127.*,localhost,<local>}}"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

start_dockerd_with_proxy_env() {
  if command -v dockerd >/dev/null 2>&1 && pgrep -x dockerd >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v dockerd >/dev/null 2>&1; then
    return 0
  fi
  if [ -n "$DOCKERD_PROXY_URL" ]; then
    run_root env \
      HTTP_PROXY="$DOCKERD_PROXY_URL" HTTPS_PROXY="$DOCKERD_PROXY_URL" \
      http_proxy="$DOCKERD_PROXY_URL" https_proxy="$DOCKERD_PROXY_URL" \
      NO_PROXY="$DOCKERD_NO_PROXY" no_proxy="$DOCKERD_NO_PROXY" \
      nohup dockerd --host=unix:///var/run/docker.sock >/tmp/gptimage-dockerd.log 2>&1 &
  else
    run_root nohup dockerd --host=unix:///var/run/docker.sock >/tmp/gptimage-dockerd.log 2>&1 &
  fi
}

start_host_proxy_forwarder() {
  if [ "$HOST_PROXY_FORWARD_ENABLED" != "true" ]; then
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found; host proxy forwarder not started" >&2
    return 0
  fi
  if [ ! -f "$PROJECT_WSL_PATH/scripts/host_proxy_forwarder.py" ]; then
    echo "host_proxy_forwarder.py not found; host proxy forwarder not started" >&2
    return 0
  fi
  if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq "(:|\\])${HOST_PROXY_LISTEN_PORT}$"; then
    return 0
  fi
  nohup python3 "$PROJECT_WSL_PATH/scripts/host_proxy_forwarder.py" \
    "$HOST_PROXY_LISTEN_HOST" "$HOST_PROXY_LISTEN_PORT" \
    "$HOST_PROXY_TARGET_HOST" "$HOST_PROXY_TARGET_PORT" \
    >/tmp/gptimage-hostproxy-forwarder.log 2>&1 &
  i=0
  while [ "$i" -lt 20 ]; do
    if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq "(:|\\])${HOST_PROXY_LISTEN_PORT}$"; then
      return 0
    fi
    i=$((i + 1))
    sleep 0.2
  done
  echo "host proxy forwarder did not open port ${HOST_PROXY_LISTEN_PORT}" >&2
}

start_host_proxy_forwarder

if ! command -v docker >/dev/null 2>&1; then
  echo "docker command not found in WSL; refusing to touch WSL services. Install/enable Docker integration, or use Windows 40080 fallback." >&2
  exit 12
fi

if ! docker info >/dev/null 2>&1 && [ "$ALLOW_DOCKER_START" = "true" ]; then
  if command -v service >/dev/null 2>&1; then
    run_root service docker start >/dev/null 2>&1 || true
  fi

  start_dockerd_with_proxy_env
  i=0
  while [ "$i" -lt 40 ]; do
    if docker info >/dev/null 2>&1; then
      break
    fi
    i=$((i + 1))
    sleep 1
  done
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is unavailable in WSL; safe mode will not auto-start dockerd. Set GPTIMAGE_WSL_ALLOW_DOCKER_START=true only when you intentionally want this script to start Docker." >&2
  exit 13
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is unavailable in WSL; proxy stack not started." >&2
  exit 14
fi

docker compose -f docker-compose.warp.yml config --services | grep -qx flaresolverr
if ! docker compose -f docker-compose.warp.yml up -d --no-build warp-proxy privoxy flaresolverr; then
  sleep 3
  docker compose -f docker-compose.warp.yml up -d --no-build warp-proxy privoxy flaresolverr
fi
