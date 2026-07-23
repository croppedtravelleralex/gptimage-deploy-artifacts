# gptimage-deploy-artifacts — nurture / heatmap overlay

Release: nurture-heatmap-20260723

## Deploy on Panda

```bash
git clone https://github.com/croppedtravelleralex/gptimage-deploy-artifacts.git /tmp/gptimage-deploy-artifacts
cd /tmp/gptimage-deploy-artifacts
COMMIT=$(git rev-parse HEAD)
bash deploy_panda_nurture.sh "$COMMIT"
```

## Files

- `services/ip_nurture_schedule.py` — 30 SG 7×12 presets
- `services/text_nurture_service.py` — turns_per_session + per-account daily cap
- `services/config.py`, `account_service.py`, `risk_dashboard_service.py`, `api/ops.py`
- `web_dist.tgz` — accounts IP group + CF 7 lights UI

Wipe this repo after successful deploy per convention.
