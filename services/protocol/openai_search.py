from __future__ import annotations

from typing import Any, Iterator

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI, SEARCH_MODEL

MODEL = SEARCH_MODEL


def handle(body: dict[str, object]) -> dict[str, object] | Iterator[dict[str, object]]:
    from services.request_account_context import get_preferred_account_email

    prefer = get_preferred_account_email() or str(body.get("preferred_account_email") or "").strip()
    token = account_service.get_text_access_token(preferred_email=prefer) if prefer else account_service.get_text_access_token()
    account = account_service.get_account(token) or {}
    email = str(account.get("email") or "")
    images_raw = body.get("images")
    images: list[str] = []
    if isinstance(images_raw, list):
        images = [str(item or "").strip() for item in images_raw if str(item or "").strip()]
    prompt = str(body.get("prompt") or "")
    stream = bool(body.get("stream"))

    backend = OpenAIBackendAPI(token)
    try:
        from services.account_warmup_service import account_warmup_service

        account_warmup_service.begin_chat_session(email)
    except Exception:
        pass

    if stream:
        return _iter_stream(backend, token, email, prompt, images)

    uploaded = max(1, len(prompt.encode("utf-8")))
    downloaded = 0
    try:
        result = backend.search(prompt, images=images)
        downloaded = len(str((result or {}).get("answer") or "").encode("utf-8"))
    finally:
        backend.close()
        account_service.mark_text_used(token)
        try:
            account_service.record_account_traffic(
                token,
                uploaded_bytes=uploaded,
                downloaded_bytes=downloaded,
            )
        except Exception:
            pass
        try:
            from services.account_warmup_service import account_warmup_service

            account_warmup_service.end_chat_session(email)
        except Exception:
            pass
    result["_account_email"] = email
    return result


def _iter_stream(
    backend: OpenAIBackendAPI,
    token: str,
    email: str,
    prompt: str,
    images: list[str],
) -> Iterator[dict[str, object]]:
    first = True
    uploaded = max(1, len(prompt.encode("utf-8")))
    downloaded = 0
    try:
        for chunk in backend.iter_search(prompt, images=images):
            if not isinstance(chunk, dict):
                continue
            item = dict(chunk)
            delta = str(item.get("delta") or item.get("content") or "")
            if delta:
                downloaded += len(delta.encode("utf-8"))
            choices = item.get("choices")
            if isinstance(choices, list) and choices:
                ch0 = choices[0] if isinstance(choices[0], dict) else {}
                delta_obj = ch0.get("delta") if isinstance(ch0, dict) else {}
                if isinstance(delta_obj, dict):
                    downloaded += len(str(delta_obj.get("content") or "").encode("utf-8"))
            if first:
                item["_account_email"] = email
                first = False
            yield item
    finally:
        backend.close()
        account_service.mark_text_used(token)
        try:
            account_service.record_account_traffic(
                token,
                uploaded_bytes=uploaded,
                downloaded_bytes=downloaded,
            )
        except Exception:
            pass
        try:
            from services.account_warmup_service import account_warmup_service

            account_warmup_service.end_chat_session(email)
        except Exception:
            pass
