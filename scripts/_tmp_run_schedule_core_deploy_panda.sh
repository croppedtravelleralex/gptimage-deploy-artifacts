#!/usr/bin/env bash
set -euo pipefail
ART_DIR=/tmp/gptimage-deploy-artifacts-slotledger
rm -rf "$ART_DIR"
git clone --depth 1 https://github.com/croppedtravelleralex/gptimage-deploy-artifacts.git "$ART_DIR"
cd "$ART_DIR"
COMMIT=$(git rev-parse HEAD)
echo "ARTIFACT_COMMIT=$COMMIT"
bash deploy_panda_schedule_core.sh "$COMMIT"
