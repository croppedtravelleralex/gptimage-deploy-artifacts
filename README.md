# gptimage-deploy-artifacts — schedule core optimization

Release: slotledger-layer1-20260725

## Deploy on Panda

```bash
git clone https://github.com/croppedtravelleralex/gptimage-deploy-artifacts.git /tmp/gptimage-deploy-artifacts
cd /tmp/gptimage-deploy-artifacts
COMMIT=$(git rev-parse HEAD)
bash deploy_panda_schedule_core.sh "$COMMIT"
```

## Contents

- Python: schedule_trace, schedule_core, **slot_ledger**, **pipeline_watchdog**, **pre_ticket_pool**, account_lease_pool, orchestrator, image_task_service, conversation, config, system health
- Native: `native/libimage_schedule_trace.so`, `native/libimage_schedule_core.so` (SlotLedger FFI; built locally via Docker, not on Panda)

Wipe this repo after successful deploy per convention.
