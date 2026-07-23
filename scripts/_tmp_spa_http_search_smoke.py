#!/usr/bin/env python3
"""HTTP search on/off smoke — mirrors _tmp_spa_text_continue_ablate turn helpers."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reuse battle-tested turn/bootstrap from continue ablate
import scripts._tmp_spa_text_continue_ablate as ab  # noqa: E402

SECRET = ab.SECRET
OUT_DIR = ab.OUT_DIR
PROXY = ab.DEFAULT_PROXY


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    token = str(secret.get("access_token") or "")
    fp = ab._fp(secret)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "mode": "http_search_smoke",
        "proxy": PROXY,
        "email": secret.get("email"),
        "token_fp": ab.anonymize_token(token),
        "ok": False,
    }

    def _with_hints(prompt: str, hints: list[str]) -> dict[str, Any]:
        body = ab.build_chat_body(
            [ab._user_message(prompt)],
            "auto",
            timezone=ab.TZ,
            timezone_offset=ab.TZ_OFFSET,
            history_and_training_disabled=False,
            parent_message_id="client-created-root",
        )
        body["system_hints"] = list(hints)
        return body

    for attempt in range(1, 6):
        try:
            sess = ab.requests.Session(impersonate=fp["impersonate"])
            sess.proxies = {"http": PROXY, "https": PROXY}

            def run_on(s):
                return ab._turn(
                    s,
                    fp,
                    token,
                    prompt="What is the capital of Japan? One short sentence.",
                    body_override=_with_hints(
                        "What is the capital of Japan? One short sentence.", ["search"]
                    ),
                    max_chunks=16,
                )

            def run_off(s):
                return ab._turn(
                    s,
                    fp,
                    token,
                    prompt="Reply with exactly: hello",
                    body_override=_with_hints("Reply with exactly: hello", []),
                    max_chunks=8,
                )

            on = ab._with_retries(run_on, label="search_on")
            off = ab._with_retries(run_off, label="search_off")
            # _with_retries may not exist — fallback
            report["search_on"] = on if isinstance(on, dict) else None
            report["search_off"] = off if isinstance(off, dict) else None
            report["ok"] = bool((on or {}).get("ok") and (off or {}).get("ok"))
            _log(phase="turn_on", **{k: on.get(k) for k in ("ok", "prepare_status", "sse_status", "sse_chunks") if isinstance(on, dict)})
            _log(phase="turn_off", **{k: off.get(k) for k in ("ok", "prepare_status", "sse_status", "sse_chunks") if isinstance(off, dict)})
            break
        except AttributeError:
            # no _with_retries
            try:
                sess = ab.requests.Session(impersonate=fp["impersonate"])
                sess.proxies = {"http": PROXY, "https": PROXY}
                on = ab._turn(
                    sess,
                    fp,
                    token,
                    prompt="What is the capital of Japan? One short sentence.",
                    body_override=_with_hints(
                        "What is the capital of Japan? One short sentence.", ["search"]
                    ),
                    max_chunks=16,
                )
                off = ab._turn(
                    sess,
                    fp,
                    token,
                    prompt="Reply with exactly: hello",
                    body_override=_with_hints("Reply with exactly: hello", []),
                    max_chunks=8,
                )
                report["search_on"] = on
                report["search_off"] = off
                report["ok"] = bool(on.get("ok") and off.get("ok"))
                _log(phase="turn_on", ok=on.get("ok"), prepare=on.get("prepare_status"), sse=on.get("sse_status"), chunks=on.get("sse_chunks"))
                _log(phase="turn_off", ok=off.get("ok"), prepare=off.get("prepare_status"), sse=off.get("sse_status"), chunks=off.get("sse_chunks"))
                break
            except Exception as exc:
                _log(phase="retry", attempt=attempt, error=str(exc)[:180])
                time.sleep(1)
        except Exception as exc:
            _log(phase="retry", attempt=attempt, error=str(exc)[:180])
            time.sleep(1)

    # annotate hints used
    if isinstance(report.get("search_on"), dict):
        report["search_on"]["hints"] = ["search"]
    if isinstance(report.get("search_off"), dict):
        report["search_off"]["hints"] = []

    path = OUT_DIR / f"http_search_smoke_{int(time.time())}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(phase="done", path=str(path), ok=report.get("ok"))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
