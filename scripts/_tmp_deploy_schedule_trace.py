#!/usr/bin/env python3
"""DEPRECATED: use artifacts flow instead of scp.

Local build:
  python scripts/build_schedule_trace_linux.py --target linux
  python scripts/build_schedule_optimization_artifact.py

Panda deploy (on server, after pushing artifact repo):
  git clone https://github.com/croppedtravelleralex/gptimage-deploy-artifacts.git /tmp/gptimage-deploy-artifacts
  cd /tmp/gptimage-deploy-artifacts && bash deploy_panda_schedule_core.sh "$(git rev-parse HEAD)"
"""
from __future__ import annotations

import sys


def main() -> int:
    raise SystemExit(
        "scp deploy disabled per SOP. Use:\n"
        "  python scripts/build_schedule_optimization_artifact.py\n"
        "  # push .artifact-schedule-deploy to gptimage-deploy-artifacts\n"
        "  # on Panda: bash deploy_panda_schedule_core.sh <commit>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
