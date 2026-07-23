# SPA vs local protocol field diff — 2026-07-21

Sources (local, gitignored HARs):

- `docs/captures/spa/spa-camoufox-20260721T044906Z.har` (~35MB)
- `docs/captures/spa/spa-image-20260721T045634Z.har` (~37MB)

Egress: Clash `127.0.0.1:7897` (JP). Account fixed: `qaflow0ytb7bbp0z@proton.me`.

## Request chain (SPA)

```text
sentinel chat-requirements prepare/finalize
  → POST /backend-api/f/conversation/prepare  (returns conduit_token)
  → POST /backend-api/f/conversation          (SSE; text path often omits X-Conduit-Token)
```

Authenticated **text** no longer uses bare `POST /backend-api/conversation` in this capture.

## Text `/f/conversation` body

| | SPA (HAR) | Local before this change |
|--|-----------|--------------------------|
| path | `/backend-api/f/conversation` | `/backend-api/conversation` |
| prepare | yes | no |
| `history_and_training_disabled` | **omitted** | forced `true` |
| `parent_message_id` (new) | `client-created-root` | random UUID |
| `client_prepare_state` | `none` | missing |
| `supports_buffering` | `true` | missing |
| `supported_encodings` | `["v1"]` | `[]` |
| `enable_message_followups` | `true` | missing |
| `force_parallel_switch` | `auto` | missing |
| `force_use_sse` / `websocket_request_id` / … | omitted | present |
| timezone | `Asia/Tokyo` / `-540` (follows egress) | often Shanghai |

## Headers (SSE start)

| Header | SPA | Local before |
|--------|-----|--------------|
| `OAI-Client-Version` | `prod-773467609da990104e0f78db96ed90bc4b199c3b` | `prod-a194cd50…` |
| `OAI-Client-Build-Number` | `8448714` | `6708908` |
| `OpenAI-Sentinel-Chat-Requirements-Prepare-Token` | set | missing (only legacy `…-Token`) |
| `X-Conduit-Token` | **absent** on text SSE | N/A (text) / required (image) |

Prepare response includes `conduit_token`, but text SSE in this HAR does **not** send `X-Conduit-Token`.

## Image

### NL 提示（「Create an image…」）

SPA：`system_hints: []`，SSE 出现 `image_gen` 元数据；prepare 有 `conduit_token`，SSE **可不发** `X-Conduit-Token`（亦见个别 HAR 带 conduit）。

HTTP repro：`scripts/_tmp_spa_camoufox_image_http_repro.py`；bench3 下载闭环。

### Create Image UI（Plus → Create image）— 2026-07-21 补抓

HAR：`spa-image-20260721T074733Z.har`（gitignore）

- 请求：`system_hints: ["picture_v2"]`；用户 parts 常为 `@Create image …`
- 头：`X-Conduit-Token` **有**
- 目录 API：`GET /backend-api/system_hints` 将 UI 名「Create image」映射为 hint id `picture_v2`

### 生产决策

反代 `/v1/images` **保持** `picture_v2`+conduit（与 UI 同族）。NL/`image_gen` 作对照不切默认。详见 [`C-image-path-decision-20260721.md`](./C-image-path-decision-20260721.md)。

## Applied minimal code changes

- Bump default OAI client version/build to HAR values
- Text target → `/backend-api/f/conversation` + prepare
- Sentinel Prepare-Token alias; chat body SPA fields; `build_text_prepare_body`
- Keep API Temporary Chat when `history_and_training_disabled=True` (field present only then)

## Three-env image+download bench

Full timings/traffic/IDs: [`bench3-20260721.md`](./bench3-20260721.md) (local Clash OK, panda direct CF403 FAIL, panda Webshare OK).

## CF note

This experiment used fixed Clash + fixed account. Success = SPA login + HAR + HTTP repro under that egress. **Does not** overturn `docs/17` (no protocol root bypass of Cloudflare).
