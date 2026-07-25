# gptimage-deploy-artifacts — schedule core optimization

Release: schedule-core-optimization-20260725

## Deploy on Panda

```bash
git clone https://github.com/croppedtravelleralex/gptimage-deploy-artifacts.git /tmp/gptimage-deploy-artifacts
cd /tmp/gptimage-deploy-artifacts
COMMIT=$(git rev-parse HEAD)
bash deploy_panda_schedule_core.sh "$COMMIT"
```

## Contents

- Python: schedule_trace, schedule_core, account_lease_pool, orchestrator, image_task_service, conversation, config
- Native: `native/libimage_schedule_trace.so`, `native/libimage_schedule_core.so` (built locally via Docker, not on Panda)

Wipe this repo after successful deploy per convention.
