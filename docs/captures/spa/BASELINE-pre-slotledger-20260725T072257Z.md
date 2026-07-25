# BASELINE pre-SlotLedger — 20260725T072257Z

- Source: **static_docs_26**
- Base URL: `http://127.0.0.1:8000`
- Generated: 2026-07-25T07:22:57.943053+00:00

## Comparison fields

| Field | Value |
|-------|-------|
| version | — |
| rss_mb | 104 |
| image_inflight_count | 3 |
| ready_candidate_count | 6 |
| dispatchable_candidate_count | 6 |
| preflight_backoff_count | 0 |
| schedulable | 16 |
| text_queue_depth | 0 |
| image_queue_depth | 0 |
| pipeline_in_flight | — |
| ps_active | — |
| ps_queued | — |
| ss_active | — |
| ss_queued | — |
| upload_queued | — |
| download_queued | — |
| image_global_concurrency_limit | — |
| image_global_limit_reached | — |

## Static reference (docs/26)

### RSS (MB)

| Scenario | MB |
|----------|-----|
| after_restart | 104 |
| post_conc10_evict_compact | 259 |
| post_conc10_pre_fix | 443 |

### conc10 references

| Capture | Result | Note |
|---------|--------|------|
| PROD-conc10-20260724T150152Z | 10/10 | reference pass |
| PROD-conc10-20260725T023900Z | 10/10 | reference pass |
| PROD-conc10-20260725T040240Z | 4/10 | post inflight-leak fix |
| PROD-conc10-20260725T034701Z | 0/10 | image_inflight=10 leak |
| PROD-conc10-20260725T033622Z | 6/10 | shared egress |

### dispatchable=6 snapshot

- schedulable=16
- ready_candidate_count=6
- dispatchable_candidate_count=6
- image_inflight_count=3
- note: humanlike image_next_ok_ts gap after conc10
