#!/usr/bin/env python3
"""Compare cold vs warm search TTFT after live-SSE + bootstrap cache."""
from __future__ import annotations

import time

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI, _SEARCH_BOOTSTRAP_CACHE


def run_once(label: str, api: OpenAIBackendAPI, prompt: str) -> None:
    t0 = time.time()
    first = None
    text = ""
    n = 0
    for chunk in api.iter_search(prompt, timeout_secs=90):
        n += 1
        delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
        piece = str(delta.get("content") or "")
        if piece and first is None:
            first = time.time()
            print(label, "TTFT", round(first - t0, 2), repr(piece[:40]), flush=True)
        text += piece
        if ((chunk.get("choices") or [{}])[0].get("finish_reason")):
            print(
                label,
                "DONE",
                round(time.time() - t0, 2),
                "chunks",
                n,
                "len",
                len(text),
                "src",
                len(chunk.get("sources") or []),
                "cache_keys",
                len(_SEARCH_BOOTSTRAP_CACHE),
                repr(text[:60]),
                flush=True,
            )
            return
    print(label, "NO_FINISH", round(time.time() - t0, 2), flush=True)


def main() -> None:
    excluded: set[str] = set()
    last_err = ""
    for i in range(6):
        tok = account_service.get_text_access_token(excluded_tokens=excluded)
        email = (account_service.get_account(tok) or {}).get("email")
        api = OpenAIBackendAPI(tok)
        try:
            api._ensure_bootstrap()
            print("try", i, email, "bootstrap_ok", bool(api.pow_script_sources), flush=True)
            run_once("COLD", api, "罗马是哪个国家的首都？只答国名。")
            run_once("WARM", api, "马德里是哪个国家的首都？只答国名。")
            api.close()
            return
        except Exception as exc:
            last_err = f"{email}: {type(exc).__name__}: {str(exc)[:160]}"
            print("fail", last_err, flush=True)
            excluded.add(tok)
            api.close()
    raise SystemExit(last_err or "no account")


if __name__ == "__main__":
    main()
