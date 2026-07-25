from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator

import tiktoken

from services.account_service import account_service
from services.config import config
from services.image_return_window_service import image_return_window_service
from services.image_storage_service import image_storage_service
from services.openai_backend_api import (
    ImageContentPolicyError,
    ImagePollRateLimitedError,
    ImagePollTimeoutError,
    ImageStreamCancelledError,
    InvalidAccessTokenError,
    OpenAIBackendAPI,
)
from utils.helper import (
    IMAGE_MODELS,
    extract_image_from_message_content,
    is_codex_image_model,
    is_supported_image_model,
    split_image_model,
)
from utils.image_tokens import count_image_content_tokens
from utils.log import logger


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        access_token: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.access_token = access_token

    def to_openai_error(self) -> dict[str, Any]:
        error_dict = {
            "error": {
                "message": public_image_error_message(str(self)),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        if self.account_email:
            error_dict["error"]["account_email"] = self.account_email
        return error_dict


def public_image_error_message(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return "The image generation request failed. Please try again later."
    return text or "The image generation request failed. Please try again later."


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


def is_cf_edge_chat_error(message: str) -> bool:
    """CF/边缘拦截：可换号重试，不等于 token 失效。"""
    text = str(message or "").lower()
    return (
        "cloudflare_or_edge_html_block" in text
        or "cf_edge_block" in text
        or ("status=403" in text and ("conversation" in text or "chat_requirements" in text or "bootstrap" in text))
        or ("http 403" in text and ("conversation" in text or "chat_requirements" in text))
    )


def is_poll_cf_abort_error(exc: BaseException) -> bool:
    if bool(getattr(exc, "cf_abort", False)):
        return True
    text = str(exc or "").lower()
    return "cloudflare_or_edge_html_block" in text and "image poll aborted" in text


def _reload_backend_sticky_proxy(backend: OpenAIBackendAPI, access_token: str) -> dict[str, Any]:
    """Reload account sticky proxy into an in-flight backend session (poll CF swap retry)."""
    from curl_cffi import requests as crequests

    from services.proxy_service import proxy_settings

    account_service.reload_from_storage()
    account = account_service.get_account(access_token) or {}
    proxy = str(account.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy"}
    old_session = backend.session
    backend.account = account
    backend.session = crequests.Session(
        **proxy_settings.build_session_kwargs(
            account=account,
            impersonate=str(getattr(old_session, "impersonate", "") or backend.fp.get("impersonate") or ""),
            verify=getattr(old_session, "verify", True),
            upstream=True,
        )
    )
    return {"ok": True, "proxy_egress_ip": account.get("proxy_egress_ip")}


def _resolve_image_urls_with_poll_cf_swap_retry(
    backend: OpenAIBackendAPI,
    access_token: str,
    *,
    conversation_id: str,
    file_ids: list[str],
    sediment_ids: list[str],
    poll_timeout: float,
    account_email: str,
    is_text_reply: bool,
    request: object | None,
    index: int,
    sediment_notify_ids: list[str],
) -> list[str]:
    """Resolve image URLs; on poll cf_abort swap egress and retry up to configured max rounds."""
    from services.proxy_cf_failover import reset_cf_streak, swap_account_proxy_on_cf
    from services.proxy_quarantine import proxy_endpoint_key

    swap_max = int(config.image_poll_cf_swap_retry_max) if config.image_poll_cf_swap_retry else 0
    tried_endpoints: set[str] = set()
    account = account_service.get_account(access_token) or {}
    current_endpoint = proxy_endpoint_key(account.get("proxy"))
    if current_endpoint:
        tried_endpoints.add(current_endpoint)

    swap_round = 0
    while True:
        try:
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id,
                file_ids,
                sediment_ids,
                poll_timeout_secs=poll_timeout,
                sse_image_gen_ms=getattr(backend, "_last_image_sse_gen_ms", None),
            )
            _pipeline_notify_sediment(request, index, sediment_notify_ids)
            return image_urls
        except Exception as exc:
            can_swap = (
                conversation_id
                and not is_text_reply
                and config.image_poll_cf_swap_retry
                and is_poll_cf_abort_error(exc)
                and swap_round < swap_max
            )
            if not can_swap:
                raise
            swap_round += 1
            try:
                account_service.reload_from_storage()
                swap_res = swap_account_proxy_on_cf(
                    access_token,
                    threshold=1,
                    reason="image_poll_cf_abort",
                    exclude_endpoints=set(tried_endpoints),
                    force=True,
                )
                reset_cf_streak(access_token)
                reload = _reload_backend_sticky_proxy(backend, access_token)
                old_endpoint = str(swap_res.get("old_endpoint") or "").strip()
                new_endpoint = str(swap_res.get("new_endpoint") or "").strip()
                swap_ok = bool(swap_res.get("ok") and reload.get("ok"))
                logger.warning({
                    "event": "image_poll_cf_swap_retry",
                    "conversation_id": conversation_id,
                    "account_email": account_email,
                    "swap_round": swap_round,
                    "swap_max": swap_max,
                    "old_endpoint": old_endpoint,
                    "new_endpoint": new_endpoint,
                    "new_egress": reload.get("proxy_egress_ip"),
                    "swap_ok": swap_ok,
                    "swap_error": swap_res.get("error"),
                    "error": str(exc)[:240],
                })
                if not swap_ok:
                    raise exc
                if old_endpoint:
                    tried_endpoints.add(old_endpoint)
                if new_endpoint:
                    tried_endpoints.add(new_endpoint)
            except Exception as retry_exc:
                if retry_exc is exc:
                    raise
                logger.warning({
                    "event": "image_poll_cf_swap_retry_failed",
                    "conversation_id": conversation_id,
                    "account_email": account_email,
                    "swap_round": swap_round,
                    "error": repr(retry_exc)[:300],
                })
                raise exc from retry_exc


def prefer_stream_for_multi_image(body: dict[str, Any]) -> dict[str, Any]:
    """n>1 且未显式指定 stream 时默认开启 SSE，便于逐张返回结果。"""
    n = int(body.get("n") or 1)
    if n > 1 and body.get("stream") is None:
        return {**body, "stream": True}
    return body


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/代理/网络连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    # CF 边缘 HTML/403 不是 TLS 断连；避免 "proxy" 子串把 CF 误判成 connection failed
    if is_cf_edge_chat_error(text):
        return False
    return (
        "curl: (35)" in text
        or "curl: (56)" in text
        or "status=502" in text
        or "status=503" in text
        or "status=504" in text
        or "connect tunnel failed" in text
        or "upstream connect error" in text
        or "disconnect/reset before headers" in text
        or "reset reason: connection termination" in text
        or "connection termination" in text
        or "response 502" in text
        or "response 503" in text
        or "response 504" in text
        or "http 408" in text
        or "http 429" in text
        or "http 500" in text
        or "http 502" in text
        or "http 503" in text
        or "http 504" in text
        or "http_408" in text
        or "http_429" in text
        or "http_500" in text
        or "http_502" in text
        or "http_503" in text
        or "http_504" in text
        or "too many requests" in text
        or "rate limit" in text
        or "bad gateway" in text
        or "gateway timeout" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "connection closed abruptly" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
        or "proxy connect" in text
        or "proxy error" in text
        or "socks proxy" in text
        or "network unreachable" in text
        or "network is unreachable" in text
        or "service unavailable" in text
    )


def is_rate_limit_http_error(message: object = None, status_code: object = None) -> bool:
    """仅识别明确的 HTTP 429 / too-many-requests；避免泛化「rate limit」误伤。"""
    try:
        if int(status_code or 0) == 429:
            return True
    except (TypeError, ValueError):
        pass
    text = str(message or "").lower()
    return (
        "http 429" in text
        or "http_429" in text
        or "status=429" in text
        or "status_code=429" in text
        or "too many requests" in text
        or "rate_limit_exceeded" in text
    )


def is_pre_conversation_transient_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        is_cf_edge_chat_error(text)
        or is_tls_connection_error(text)
        or is_connection_timeout_error(text)
        or "http/2 stream" in text
        or "http2 stream" in text
        or "internal_error" in text
        or "stream was not closed cleanly" in text
        or "first payload timeout" in text
        or "conversation metadata timeout" in text
        or "ended before conversation metadata" in text
        or "remote end closed connection" in text
        or "remote disconnected" in text
        or "connection reset" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
    )


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if is_token_invalid_error(text):
        return "image generation failed"
    if is_cf_edge_chat_error(text):
        return "upstream CF edge blocked (403), please retry or switch proxy egress"
    if is_tls_connection_error(text):
        return "upstream image connection failed, please retry later"
    if is_connection_timeout_error(text):
        return "upstream connection timed out, please retry later"
    return text or "image generation failed"


REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(
    r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}'
)


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。

    检测两种模式：
    1. 完整的工具调用 JSON（含 referenced_image_ids）
    2. 部分的工具参数 JSON（如 {"size":"1920x1088","n":1}）
    """
    if not message:
        return False
    # 注意：bare {"skipped_mainline":true} 是 picture_v2 对 image_gen 工具的调用载荷，
    # 不是「主链路失败 / 可换号」信号；不得在此当成文本代图。
    if REFERENCED_IMAGE_IDS_RE.search(message):
        return True
    # 检测部分工具参数 JSON（模型返回了工具参数但未触发工具）
    if TOOL_PARAMS_JSON_RE.search(message):
        return True
    return False


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(image_data: bytes, base_url: str | None = None) -> str:
    return image_storage_service.save(image_data, base_url).url


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    quality_text = str(quality or "").strip()
    if quality_text and quality_text.lower() != "auto":
        hints.append(f"输出图片质量为 {quality_text}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        if response_format == "b64_json":
            data.append({
                "b64_json": b64_json,
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
        else:
            data.append({
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    return result


def download_and_format_image_urls(
    backend: OpenAIBackendAPI,
    image_urls: list[str],
    request: "ConversationRequest",
    created: int | None = None,
) -> list[dict[str, Any]]:
    """在回传窗口内下载图片并构造 OpenAI image data。"""
    if not image_urls:
        return []
    selected_image_urls = image_urls[:1]
    if len(image_urls) > 1:
        logger.warning({
            "event": "image_upstream_extra_results_dropped",
            "received_count": len(image_urls),
            "accepted_count": len(selected_image_urls),
            "request_n": request.n,
        })
    try:
        pipeline_run = request.pipeline_run
        if pipeline_run is not None:
            pipeline_run.acquire_download()
        with image_return_window_service.acquire(len(selected_image_urls)):
            image_items = [
                {"b64_json": base64.b64encode(image_data).decode("ascii")}
                for image_data in backend.download_image_bytes(selected_image_urls)
            ]
            return format_image_result(
                image_items,
                request.prompt,
                request.response_format,
                request.base_url,
                created or int(time.time()),
            )["data"]
    except TimeoutError as exc:
        raise ImageGenerationError(
            str(exc),
            status_code=503,
            error_type="server_error",
            code="image_return_window_timeout",
        ) from exc
    finally:
        if request.pipeline_run is not None:
            request.pipeline_run.release_download()


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[object], None] | None
    cancel_event: threading.Event | None = None
    poll_timeout_secs: float | None = None
    queue_coordinated: bool = False
    prompt_enhance: bool = False
    prompt_enhance_locale: str = "en"
    multi_image_mode: str = "fast"
    pipeline_run: Any = None
    preferred_account_email: str = ""


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    last_message_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


_LEAKED_TOOL_CALL_RE = re.compile(
    r"(?is)\b(?:search|web_search|browser|open_url|web\.search)\s*\(\s*"
    r"(?:\[.*?\]|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*\)\s*"
)


def strip_leaked_tool_calls(text: str) -> str:
    """Remove model-leaked tool-call stubs like search(\"...\") from visible text."""
    value = str(text or "")
    if not value:
        return value
    prev = None
    while prev != value:
        prev = value
        value = _LEAKED_TOOL_CALL_RE.sub("", value)
    return value


def sanitize_output_text(text: str) -> str:
    text = strip_leaked_tool_calls(str(text or ""))

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        msg_id = str(message.get("id") or "").strip()
        author = message.get("author") or {}
        role = str(author.get("role") or "").strip().lower()
        # Parent for next turn should be the latest assistant (or tool) leaf, not the user echo.
        if msg_id and role in {"assistant", "tool"}:
            state.last_message_id = msg_id
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "parent_message_id": state.last_message_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        **extra,
    }


def iter_conversation_payloads(
    payloads: Iterator[str],
    history_text: str = "",
    history_messages: list[str] | None = None,
    *,
    on_payload: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if on_payload is not None:
            try:
                on_payload(payload)
            except Exception:
                pass
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    thinking_effort: str = "",
    *,
    on_payload: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(build_image_prompt(prompt, size, quality)) if image_model else prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
    )
    yield from iter_conversation_payloads(payloads, history_text, history_messages, on_payload=on_payload)


def text_backend() -> OpenAIBackendAPI:
    from services.request_account_context import get_preferred_account_email

    prefer = get_preferred_account_email()
    token = account_service.get_text_access_token(preferred_email=prefer) if prefer else account_service.get_text_access_token()
    return OpenAIBackendAPI(access_token=token)


def _json_payload_bytes(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return len(str(value or "").encode("utf-8"))


def _text_request_bytes(request: ConversationRequest) -> int:
    return _json_payload_bytes({
        "model": request.model,
        "prompt": request.prompt,
        "messages": request.messages or [],
        "thinking_effort": request.thinking_effort,
    })


def _image_request_bytes(request: ConversationRequest) -> int:
    return _json_payload_bytes({
        "model": request.model,
        "prompt": request.prompt,
        "images": request.images or [],
        "n": request.n,
        "size": request.size,
        "quality": request.quality,
    })


def _image_outputs_bytes(outputs: list[ImageOutput]) -> int:
    return _json_payload_bytes([
        {"kind": output.kind, "text": output.text, "data": output.data}
        for output in outputs
    ])


def _close_backend_quietly(backend: object) -> None:
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    emitted = False
    first_attempt = True
    while True:
        if token and token in attempted_tokens:
            raise RuntimeError("no available text account")
        if token:
            attempted_tokens.add(token)
        attempt_token = token
        uploaded_bytes = _text_request_bytes(request)
        downloaded_bytes = 0
        request_started = False
        active_backend: OpenAIBackendAPI | None = None
        started_at = time.monotonic()
        outcome = "ok"
        outcome_code = ""
        seen_conversation_id = ""
        seen_parent_message_id = ""
        try:
            if first_attempt:
                active_backend = backend
                first_attempt = False
            else:
                active_backend = OpenAIBackendAPI(access_token=token)
            try:
                from services.account_warmup_service import account_warmup_service

                acc = account_service.get_account(token) or {}
                account_warmup_service.begin_chat_session(str(acc.get("email") or ""))
            except Exception:
                pass
            request_started = True
            for event in conversation_events(
                active_backend,
                messages=request.messages,
                model=request.model,
                prompt=request.prompt,
                thinking_effort=request.thinking_effort,
            ):
                cid = str(event.get("conversation_id") or "").strip()
                if cid:
                    seen_conversation_id = cid
                parent = str(event.get("parent_message_id") or "").strip()
                if parent:
                    seen_parent_message_id = parent
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    downloaded_bytes += len(delta.encode("utf-8"))
                    yield delta
            account_service.mark_text_used(token)
            if token and (seen_conversation_id or seen_parent_message_id):
                account_service.remember_text_conversation(
                    token,
                    conversation_id=seen_conversation_id,
                    parent_message_id=seen_parent_message_id,
                )
            return
        except Exception as exc:
            outcome = "error"
            outcome_code = type(exc).__name__
            error_message = str(exc)
            if token and not emitted and (
                is_token_invalid_error(error_message) or is_cf_edge_chat_error(error_message)
            ):
                if is_token_invalid_error(error_message):
                    refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                    if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                        token = refreshed_token
                    else:
                        account_service.remove_invalid_token(token, "text_stream")
                        token = account_service.get_text_access_token(attempted_tokens)
                else:
                    # CF 边缘：勿删号；换其它可调度号继续，并下预热池
                    try:
                        from services.account_warmup_service import account_warmup_service

                        acc = account_service.get_account(token) or {}
                        account_warmup_service.demote(str(acc.get("email") or ""), reason="cf_edge_chat")
                    except Exception:
                        pass
                    try:
                        account_service.record_cf_sample(token, kind="cf")
                    except Exception:
                        pass
                    logger.warning(
                        {
                            "event": "text_stream_cf_failover",
                            "from_token_prefix": str(token or "")[:16],
                            "error": error_message[:240],
                        }
                    )
                    token = account_service.get_text_access_token(attempted_tokens)
                if token:
                    continue
            raise
        finally:
            try:
                if request_started:
                    from services.log_service import log_llm_ops

                    prompt_len = len(str(request.prompt or ""))
                    if request.messages:
                        prompt_len = max(
                            prompt_len,
                            sum(len(str(m.get("content") or "")) for m in request.messages if isinstance(m, dict)),
                        )
                    log_llm_ops(
                        source="L0",
                        kind="chat",
                        access_token=attempt_token or token,
                        latency_ms=int((time.monotonic() - started_at) * 1000),
                        outcome=outcome,
                        outcome_code=outcome_code,
                        prompt_shape={
                            "chars": prompt_len,
                            "has_images": False,
                            "model": str(request.model or ""),
                            "has_conversation_id": bool(seen_conversation_id),
                        },
                    )
            except Exception:
                pass
            try:
                if request_started and attempt_token:
                    account_service.record_account_traffic(
                        attempt_token,
                        uploaded_bytes=uploaded_bytes,
                        downloaded_bytes=downloaded_bytes,
                    )
            finally:
                try:
                    from services.account_warmup_service import account_warmup_service

                    acc = account_service.get_account(attempt_token or token) or {}
                    account_warmup_service.end_chat_session(str(acc.get("email") or ""))
                except Exception:
                    pass
                if active_backend is not None:
                    _close_backend_quietly(active_backend)


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": str(exc),
        })
        return ""


def _pipeline_notify_sediment(request: ConversationRequest | None, index: int, sediment_ids: list[str]) -> None:
    if request is None:
        return
    pipeline_run = request.pipeline_run
    if pipeline_run is not None and sediment_ids:
        pipeline_run.on_sediment_captured(image_index=index, sediment_ids=[str(item) for item in sediment_ids if item])


def _mark_poll_resolve_if_needed(request: ConversationRequest) -> None:
    if request.pipeline_run is not None:
        try:
            request.pipeline_run.mark_poll_resolve_end()
        except Exception:
            pass


def _request_image_poll_timeout(request: ConversationRequest) -> float:
    if request.poll_timeout_secs is not None:
        try:
            return max(1.0, float(request.poll_timeout_secs))
        except (TypeError, ValueError):
            pass
    reference_count = len(request.images or [])
    if reference_count > 1:
        return float(config.image_multi_reference_poll_timeout_secs)
    if reference_count == 1:
        return float(config.image_edit_poll_timeout_secs)
    return float(config.image_generation_poll_timeout_secs)


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    # 请求发出前的时间戳：conversation 恢复必须用 submit 前时间，禁止用恢复时刻。
    submit_started_at = time.time()
    last: dict[str, Any] = {}
    from services.image_pipeline.schedule_core import SedimentParser

    sediment_parser = SedimentParser()
    notified_sediment: set[str] = set()

    def _on_sse_payload(chunk: str) -> None:
        try:
            sediment_parser.feed(chunk)
        except Exception:
            return
        new_ids = [sid for sid in sediment_parser.ids() if sid not in notified_sediment]
        if new_ids:
            notified_sediment.update(new_ids)
            _pipeline_notify_sediment(request, index, new_ids)

    for event in conversation_events(
            backend,
            prompt=request.prompt,
            model=request.model,
            images=request.images or [],
            size=request.size,
            quality=request.quality,
            on_payload=_on_sse_payload,
    ):
        last = event
        if event.get("type") == "conversation.delta":
            yield ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                text=str(event.get("delta") or ""),
                upstream_event_type="conversation.delta",
            )
            continue
        if event.get("type") == "conversation.event":
            raw = event.get("raw")
            raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
            yield ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                upstream_event_type=raw_type,
            )

    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    logger.info({
        "event": "image_stream_resolve_start",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
    })
    if sediment_ids:
        _pipeline_notify_sediment(request, index, sediment_ids)
    if request.pipeline_run is not None:
        try:
            request.pipeline_run.mark_sse_stream_end()
        except Exception:
            pass
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")
    if message and not file_ids and not sediment_ids and last.get("blocked"):
        # 尝试从 /backend-api/tasks/ 获取详细错误信息
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id)
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=error_text, conversation_id=conversation_id)
        return
    # 生图模型请求一律允许按 conversation 轮询：SSE 可能只先出现 tool 调用
    # （含 skipped_mainline），图在随后数十秒才写入 conversation。
    should_poll_for_image = (
        bool(request.images)
        or last.get("turn_use_case") == "image gen"
        or is_supported_image_model(request.model)
    )
    if message and not file_ids and not sediment_ids and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
    # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开，
    # 图片可能正在异步生成中。需要使用更积极的轮询策略来获取结果。
    is_text_reply = bool(message and is_model_text_reply_instead_of_image(message))
    if is_text_reply:
        logger.info({
            "event": "image_detected_text_reply_with_ids",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    # 当检测到文本回复但 conversation_id 丢失时，尝试从最近对话列表中恢复
    # SSE 流太短时（模型返回文本而非触发图片工具），conversation_id 可能未被捕获，
    # 但图片已在上游异步生成。通过列出最近对话来恢复 conversation_id。
    if is_text_reply and not conversation_id:
        try:
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, submit_started_at, timeout_secs=5.0,
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_conversation_id_recovered",
                    "conversation_id": conversation_id,
                    "message_preview": message[:200],
                    "submit_started_at": submit_started_at,
                })
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })

    # 在轮询图片之前，先检查 /backend-api/tasks/ 是否有 moderation 拦截
    # 这样可以避免不必要的长时间轮询超时
    # 注意：当 should_poll_for_image 为 True 或检测到文本回复时，
    # 即使 tasks 报告了"错误"，也不能直接返回——因为上游可能将工具调用的 JSON 参数
    # （如 {"size":"1792x1024","n":1}）标记为 is_error，而实际上图片正在异步生成中。
    # 此时应继续轮询图片。
    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id:
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id, timeout_secs=5.0, wait_secs=1.0)
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error and (should_poll_for_image or is_text_reply):
            logger.info({
                "event": "image_task_error_skipped_for_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    # 当检测到文本回复（含 referenced_image_ids）时，使用更长的超时来轮询图片结果。
    # 因为上游可能将图片生成作为异步任务执行，SSE 流在工具完成前就断开了，
    # 导致对话文档中尚未写入图片工具的响应记录。
    poll_timeout = _request_image_poll_timeout(request)
    if is_text_reply and conversation_id:
        # 文本回复场景下图片可能仍在异步生成，使用更长超时（默认 120s → 额外 180s = 300s）
        poll_timeout = max(poll_timeout, 300)
        logger.info({
            "event": "image_text_reply_extended_poll",
            "conversation_id": conversation_id,
            "poll_timeout_secs": poll_timeout,
        })

    access_token = str(getattr(backend, "access_token", "") or "").strip()
    account_email = ""
    if access_token:
        account = account_service.get_account(access_token) or {}
        account_email = str(account.get("email") or "").strip()

    try:
        image_urls = _resolve_image_urls_with_poll_cf_swap_retry(
            backend,
            access_token,
            conversation_id=conversation_id,
            file_ids=file_ids,
            sediment_ids=sediment_ids,
            poll_timeout=poll_timeout,
            account_email=account_email,
            is_text_reply=is_text_reply,
            request=request,
            index=index,
            sediment_notify_ids=sediment_ids,
        )
    except ImagePollRateLimitedError as exc:
        try:
            account_service.apply_429_cooldown(access_token, exc)
        except Exception:
            pass
        try:
            from services.proxy_cf_failover import swap_account_proxy_on_cf

            swap_account_proxy_on_cf(access_token, threshold=1)
        except Exception:
            pass
        raise ImageGenerationError(
            str(exc) or "Image poll rate limited by upstream (HTTP 429).",
            status_code=429,
            error_type="rate_limit_error",
            code="image_poll_rate_limited",
            account_email=account_email,
            conversation_id=getattr(exc, "conversation_id", conversation_id or ""),
            access_token=access_token,
        ) from exc
    except (ImageContentPolicyError, ImagePollTimeoutError) as exc:
        # 当检测到文本回复时，task error 不应直接判定为内容策略违规，
        # 因为图片可能仍在后台异步生成中
        if is_text_reply and isinstance(exc, ImageContentPolicyError):
            logger.warning({
                "event": "image_text_reply_task_error_ignored",
                "conversation_id": conversation_id,
                "error": str(exc),
            })
            image_urls = []
        else:
            raise
    except Exception as exc:
        # 当检测到文本回复时，首次轮询的临时网络错误不应直接中断，
        # 因为图片可能仍在后台异步生成中，后续 retry poll 会继续尝试。
        if is_text_reply and conversation_id:
            logger.warning({
                "event": "image_text_reply_first_poll_error_ignored",
                "conversation_id": conversation_id,
                "error": repr(exc)[:300],
            })
            image_urls = []
        else:
            raise

    if image_urls:
        _mark_poll_resolve_if_needed(request)
        if request.progress_callback:
            request.progress_callback("receiving_image")
        data = download_and_format_image_urls(
            backend,
            image_urls,
            request,
            int(time.time()),
        )
        if data:
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
        return

    if message:
        # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
        # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开。
        # 此时应再尝试轮询图片结果，而不是直接把文本当作最终输出。
        # 当 is_text_reply 但 conversation_id 丢失时，尝试从最近对话列表恢复
        if is_text_reply and not conversation_id:
            try:
                recovered_id = backend.find_conversation_by_prompt(
                    request.prompt, submit_started_at, timeout_secs=5.0,
                )
                if recovered_id:
                    conversation_id = recovered_id
                    logger.info({
                        "event": "image_text_reply_conversation_id_recovered",
                        "conversation_id": conversation_id,
                        "message_preview": message[:200],
                        "submit_started_at": submit_started_at,
                    })
            except Exception as exc:
                logger.warning({
                    "event": "image_text_reply_conversation_id_recovery_failed",
                    "error": repr(exc)[:300],
                })
        if is_text_reply and conversation_id:
            logger.info({
                "event": "image_model_text_reply_retry_poll",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
            # 单一协调：内层 _poll_image_results 已有 GET/wall 硬预算；外层仅允许一次 transient 补救。
            retry_poll_timeout = max(_request_image_poll_timeout(request), 300)
            MAX_OUTER_POLL_RETRIES = 1
            for poll_attempt in range(1, MAX_OUTER_POLL_RETRIES + 1):
                try:
                    polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                        conversation_id,
                        retry_poll_timeout,
                        file_ids,
                        sediment_ids,
                    )
                    file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                    sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                    break
                except Exception as exc:
                    error_str = str(exc)
                    is_transient = (
                        isinstance(exc, ImagePollTimeoutError)
                        or is_tls_connection_error(error_str)
                        or "upstream" in error_str.lower()
                        or "connection" in error_str.lower()
                        or "timeout" in error_str.lower()
                    )
                    logger.warning({
                        "event": "image_model_text_reply_poll_failed",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "error": repr(exc)[:300],
                        "is_transient": is_transient,
                    })
                    break

            if file_ids or sediment_ids:
                image_urls = backend.resolve_conversation_image_urls(
                    conversation_id, file_ids, sediment_ids, poll=False,
                )
                if image_urls:
                    _mark_poll_resolve_if_needed(request)
                    if request.progress_callback:
                        request.progress_callback("receiving_image")
                    data = download_and_format_image_urls(
                        backend,
                        image_urls,
                        request,
                        int(time.time()),
                    )
                    if data:
                        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
                        return
        elif is_text_reply:
            logger.warning({
                "event": "image_model_text_reply_no_image",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 兜底：当 message 为空且图片 URL 解析失败时，先尝试一次短延迟重试轮询
    # 然后抛出明确错误而非让调用方得到 "upstream completed without generating images" 这种模糊报错
    logger.warning({
        "event": "image_stream_no_result_fallback",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "should_poll_for_image": should_poll_for_image,
    })
    # 当 should_poll_for_image 为 True 但 conversation_id 丢失时，尝试恢复
    if should_poll_for_image and not conversation_id:
        try:
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, submit_started_at, timeout_secs=5.0,
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_fallback_conversation_id_recovered",
                    "conversation_id": conversation_id,
                    "submit_started_at": submit_started_at,
                })
        except Exception as exc:
            logger.warning({
                "event": "image_fallback_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })
    if should_poll_for_image and conversation_id:
        # 单一协调：内层 poll 硬预算；外层最多 1 次短等待后补救。
        retry_poll_timeout = max(_request_image_poll_timeout(request), 300)
        MAX_FALLBACK_POLL_RETRIES = 1
        for poll_attempt in range(1, MAX_FALLBACK_POLL_RETRIES + 1):
            retry_wait_secs = min(30.0 * poll_attempt, max(config.image_poll_initial_wait_secs, 10.0))
            logger.info({
                "event": "image_stream_retry_poll_after_wait",
                "conversation_id": conversation_id,
                "retry_wait_secs": retry_wait_secs,
                "poll_attempt": poll_attempt,
            })
            time.sleep(retry_wait_secs)
            try:
                polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                    conversation_id,
                    retry_poll_timeout,
                    file_ids,
                    sediment_ids,
                )
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                break  # 轮询成功，退出重试循环
            except Exception as exc:
                error_str = str(exc)
                is_transient = (
                    isinstance(exc, ImagePollTimeoutError)
                    or is_tls_connection_error(error_str)
                    or "upstream" in error_str.lower()
                    or "connection" in error_str.lower()
                    or "timeout" in error_str.lower()
                )
                logger.warning({
                    "event": "image_stream_retry_poll_failed",
                    "conversation_id": conversation_id,
                    "poll_attempt": poll_attempt,
                    "error": repr(exc)[:300],
                    "is_transient": is_transient,
                })
                # 如果还有重试次数且不是超时/内容违规错误，继续重试
                if poll_attempt < MAX_FALLBACK_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                    # 递增退避：30s, 60s
                    backoff = 30.0 * poll_attempt
                    logger.info({
                        "event": "image_stream_retry_poll_retry",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "backoff_secs": backoff,
                    })
                    time.sleep(backoff)
                    continue
                # 超时错误或重试次数用尽，停止重试
                break

        if file_ids or sediment_ids:
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if image_urls:
                _mark_poll_resolve_if_needed(request)
                if request.progress_callback:
                    request.progress_callback("receiving_image")
                data = download_and_format_image_urls(
                    backend,
                    image_urls,
                    request,
                    int(time.time()),
                )
                if data:
                    yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
                    return

        # 重试后仍然失败，yield 错误消息
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation completed upstream but the result could not be retrieved. "
                               "The image may still be processing. Please try again in a moment.",
                          conversation_id=conversation_id)
    elif message:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
    else:
        # conversation_id 也为空时（SSE 流极短、未捕获到会话 ID），
        # 仍然 yield 一条消息，避免 stream_image_outputs_with_pool 产生
        # "upstream completed without generating images" 模糊报错
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation started upstream but the response was incomplete. "
                               "Please try again.",
                          conversation_id=conversation_id)


def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    )))
    if not images:
        raise ImageGenerationError("No image result found in response")
    try:
        with image_return_window_service.acquire(len(images)):
            data = format_image_result(
                [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
                request.prompt,
                request.response_format,
                request.base_url,
                int(time.time()),
            )["data"]
    except TimeoutError as exc:
        raise ImageGenerationError(
            str(exc),
            status_code=503,
            error_type="server_error",
            code="image_return_window_timeout",
        ) from exc
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data)
        return
    raise ImageGenerationError("No image result found in response")


def _conversation_id_from_exception(exc: Exception) -> str:
    value = str(getattr(exc, "conversation_id", "") or "").strip()
    if value:
        return value
    match = re.search(r"\(([A-Za-z0-9_-]{8,})\)", str(exc))
    return match.group(1) if match else ""


def _generate_single_image(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。
    """
    # 模型返回文本而非图片的最大重试次数
    MAX_TEXT_REPLY_RETRIES = 3
    # 轮询超时错误最大重试次数（换账号重试）
    MAX_POLL_TIMEOUT_RETRIES = 4

    text_reply_retry_count = 0
    poll_timeout_retry_count = 0
    pre_conversation_transient_count = 0
    account_email = ""
    pipeline_run = request.pipeline_run

    while True:
        ps_slot: int | None = None
        ss_slot: int | None = None
        from services.image_pipeline.types import MultiImageMode

        diverse_ps = (
            pipeline_run is not None
            and pipeline_run.needs_ps
            and pipeline_run.multi_image_mode == MultiImageMode.DIVERSE
        )
        try:
            if diverse_ps and pipeline_run is not None:
                from services.image_pipeline.prompt_enhance import run_prompt_enhance

                enhanced = run_prompt_enhance(pipeline_run, request)
                request = replace(request, prompt=enhanced)
            if pipeline_run is not None:
                pipeline_run.mark_account_wait_start()
            lease = None
            try:
                if request.progress_callback:
                    request.progress_callback("getting_account")
                plan_type, _ = split_image_model(request.model)
                codex_model = is_codex_image_model(request.model)
                from services.request_account_context import get_preferred_account_email

                prefer = get_preferred_account_email() or str(request.preferred_account_email or "").strip()
                if pipeline_run is not None:
                    lease = pipeline_run.account_provider.acquire_for_ss(
                        plan_type=plan_type,
                        source_type="codex" if codex_model else None,
                        plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
                        skip_global_limit=bool(request.queue_coordinated),
                        preferred_email=prefer,
                    )
                    token = lease.access_token
                    pipeline_run.mark_account_acquired()
                    pipeline_run.bind_account_token(token)
                    ss_slot, _ = pipeline_run.acquire_ss(image_index=index)
                else:
                    token = account_service.get_available_access_token(
                        plan_type=plan_type,
                        source_type="codex" if codex_model else None,
                        plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
                        skip_global_limit=bool(request.queue_coordinated),
                        preferred_email=prefer,
                    )
                if request.progress_callback:
                    request.progress_callback({"step": "account_acquired", "access_token": token})
            except RuntimeError as exc:
                if lease is not None:
                    lease.release()
                raise ImageGenerationError(str(exc) or "image generation failed", account_email=account_email) from exc
            except TimeoutError as exc:
                if lease is not None:
                    lease.release()
                raise ImageGenerationError(str(exc) or "image pipeline slot timeout", account_email=account_email) from exc

            emitted_for_token = False
            returned_message = False
            returned_result = False
            account = account_service.get_account(token) or {}
            account_email = str(account.get("email") or "").strip()
            from services.image_pipeline import schedule_trace as _schedule_trace

            active_trace = _schedule_trace.active()
            if active_trace is not None and account_email:
                active_trace.set_account_email(account_email)
            logger.debug({
                "event": "image_account_lookup",
                "token_prefix": token[:12] + "..." if len(token) > 12 else token,
                "account_email": account_email,
                "account_found": bool(account),
                "index": index,
            })
            backend: OpenAIBackendAPI | None = None
            try:
                backend = OpenAIBackendAPI(access_token=token)
                backend.cancel_event = request.cancel_event
                if request.progress_callback:
                    backend.progress_callback = request.progress_callback
                stream_fn = stream_codex_image_outputs if is_codex_image_model(request.model) else stream_image_outputs
                outputs: list[ImageOutput] = []
                for output in stream_fn(backend, request, index, total):
                    if account_email and not output.account_email:
                        output.account_email = account_email
                    if output.kind == "message" and request.message_as_error:
                        raise ImageGenerationError(
                            output.text or "Image generation was rejected by upstream policy.",
                            status_code=400,
                            error_type="invalid_request_error",
                            code="content_policy_violation",
                            account_email=account_email,
                            conversation_id=output.conversation_id,
                        )
                    returned_message = output.kind == "message"
                    returned_result = returned_result or output.kind == "result"
                    emitted_for_token = returned_message or returned_result
                    outputs.append(output)
                if returned_message:
                    account_service.record_account_traffic(
                        token,
                        uploaded_bytes=_image_request_bytes(request),
                        downloaded_bytes=_image_outputs_bytes(outputs),
                    )
                    account_service.mark_image_result(token, False, error="upstream text message instead of image")
                    return outputs
                if not returned_result:
                    account_service.record_account_traffic(
                        token,
                        uploaded_bytes=_image_request_bytes(request),
                        downloaded_bytes=_image_outputs_bytes(outputs),
                    )
                    account_service.mark_image_result(token, False, error="upstream completed without generating images")
                    if emitted_for_token:
                        conv_id = outputs[-1].conversation_id if outputs else ""
                        raise ImageGenerationError(
                            "upstream completed without generating images",
                            status_code=400,
                            error_type="invalid_request_error",
                            code="no_image_generated",
                            account_email=account_email,
                            conversation_id=conv_id,
                        )
                    return outputs
                account_service.record_account_traffic(
                    token,
                    uploaded_bytes=_image_request_bytes(request),
                    downloaded_bytes=_image_outputs_bytes(outputs),
                )
                account_service.mark_image_result(token, True)
                return outputs
            except ImageStreamCancelledError:
                # cancel_event 的所有者（ImageTaskService）负责释放账号槽位和决定
                # error/timeout_pending 状态；这里不得再次 mark，避免重复释放和误增失败数。
                raise
            except InvalidAccessTokenError as exc:
                account_service.mark_image_result(token, False, error=exc)
                if account_email:
                    setattr(exc, "account_email", account_email)
                conversation_id = _conversation_id_from_exception(exc)
                if conversation_id:
                    logger.warning({
                        "event": "image_poll_token_invalid_timeout_pending",
                        "request_token": token,
                        "account_email": account_email,
                        "conversation_id": conversation_id,
                        "index": index,
                        "error": str(exc)[:200],
                    })
                    raise ImageGenerationError(
                        image_stream_error_message(str(exc)),
                        status_code=202,
                        error_type="server_error",
                        code="image_timeout_pending",
                        account_email=account_email,
                        conversation_id=conversation_id,
                        access_token=token,
                    ) from exc
                if not emitted_for_token:
                    logger.warning({
                        "event": "image_poll_token_invalid_retry",
                        "request_token": token,
                        "account_email": account_email,
                        "index": index,
                        "error": str(exc)[:200],
                    })
                    account_service.remove_invalid_token(token, "image_poll")
                    continue
                raise ImageGenerationError(
                    image_stream_error_message(str(exc)),
                    account_email=account_email,
                    conversation_id=getattr(exc, "conversation_id", ""),
                    access_token=token,
                ) from exc
            except ImagePollTimeoutError as exc:
                account_service.mark_image_result(token, False, error=exc)
                if account_email:
                    setattr(exc, "account_email", account_email)
                conversation_id = str(getattr(exc, "conversation_id", "") or "").strip()
                if conversation_id:
                    logger.warning({
                        "event": "image_poll_timeout_pending",
                        "request_token": token,
                        "account_email": account_email,
                        "conversation_id": conversation_id,
                        "index": index,
                        "error": str(exc)[:200],
                    })
                    raise ImageGenerationError(
                        str(exc) or "ChatGPT 生图超时，已进入后台续轮询。",
                        status_code=202,
                        error_type="server_error",
                        code="image_timeout_pending",
                        account_email=account_email,
                        conversation_id=conversation_id,
                        access_token=token,
                    ) from exc
                # 轮询超时但还没有 conversation_id：才允许换账号做有限重试。
                if not emitted_for_token:
                    poll_timeout_retry_count += 1
                    if poll_timeout_retry_count <= MAX_POLL_TIMEOUT_RETRIES:
                        logger.warning({
                            "event": "image_poll_timeout_retry",
                            "request_token": token,
                            "account_email": account_email,
                            "retry_count": poll_timeout_retry_count,
                            "index": index,
                            "error": str(exc)[:200],
                        })
                        # pin 住故障号时若不清除 preferred，会反复拿到同一账号。
                        try:
                            from services.request_account_context import set_preferred_account_email

                            set_preferred_account_email("")
                        except Exception:
                            pass
                        continue
                    logger.warning({
                        "event": "image_poll_timeout_exhausted_retries",
                        "request_token": token,
                        "account_email": account_email,
                        "retry_count": poll_timeout_retry_count,
                        "index": index,
                    })
                    raise
                raise
            except ImageContentPolicyError as exc:
                account_service.mark_image_result(token, False, error=exc)
                logger.warning({
                    "event": "image_stream_content_policy_error",
                    "request_token": token,
                    "account_email": account_email,
                    "error": str(exc),
                    "index": index,
                })
                raise ImageGenerationError(
                    str(exc) or "Image generation was rejected by upstream policy.",
                    status_code=400,
                    error_type="invalid_request_error",
                    code="content_policy_violation",
                    account_email=account_email,
                    conversation_id=getattr(exc, "conversation_id", ""),
                ) from exc
            except ImageGenerationError as exc:
                account_service.mark_image_result(token, False, error=exc)
                if is_rate_limit_http_error(exc, getattr(exc, "status_code", None)):
                    try:
                        account_service.apply_429_cooldown(token, exc)
                    except Exception:
                        pass
                if account_email and not getattr(exc, "account_email", ""):
                    exc.account_email = account_email
                error_text = str(exc)
                # 如果是模型返回文本而非图片，尝试换账号重试
                if is_model_text_reply_instead_of_image(error_text) and not emitted_for_token:
                    text_reply_retry_count += 1
                    if text_reply_retry_count <= MAX_TEXT_REPLY_RETRIES:
                        logger.warning({
                            "event": "image_model_text_reply_retry",
                            "request_token": token,
                            "account_email": account_email,
                            "retry_count": text_reply_retry_count,
                            "index": index,
                            "error": error_text[:200],
                        })
                        continue
                    logger.warning({
                        "event": "image_model_text_reply_exhausted_retries",
                        "request_token": token,
                        "account_email": account_email,
                        "retry_count": text_reply_retry_count,
                        "index": index,
                    })
                    raise ImageGenerationError(
                        "Image generation failed: the upstream model returned a text description "
                        "instead of generating an image. Please try again later.",
                        status_code=502,
                        error_type="server_error",
                        code="upstream_text_reply",
                        account_email=account_email,
                        conversation_id=getattr(exc, "conversation_id", ""),
                        access_token=token,
                    ) from exc
                logger.warning({
                    "event": "image_stream_generation_error",
                    "request_token": token,
                    "account_email": account_email,
                    "error": error_text,
                    "index": index,
                })
                raise
            except Exception as exc:
                account_service.mark_image_result(token, False, error=exc)
                last_error = str(exc)
                if is_rate_limit_http_error(last_error, getattr(exc, "status_code", None)):
                    try:
                        account_service.apply_429_cooldown(token, exc)
                    except Exception:
                        pass
                logger.warning({
                    "event": "image_stream_fail",
                    "request_token": token,
                    "account_email": account_email,
                    "error": last_error,
                    "index": index,
                })
                if not emitted_for_token and is_token_invalid_error(last_error):
                    refreshed_token = account_service.refresh_access_token(token, force=True, event="image_stream")
                    if refreshed_token and refreshed_token != token:
                        token = refreshed_token
                        continue
                    account_service.remove_invalid_token(token, "image_stream")
                    continue
                if not emitted_for_token and is_cf_edge_chat_error(last_error):
                    pre_conversation_transient_count += 1
                    max_attempts = max(1, int(config.image_pre_conversation_max_attempts))
                    try:
                        from services.account_warmup_service import account_warmup_service

                        account_warmup_service.demote(account_email, reason="cf_edge_image")
                    except Exception:
                        pass
                    try:
                        from services.request_account_context import set_preferred_account_email

                        set_preferred_account_email("")
                    except Exception:
                        pass
                    try:
                        account_service.record_image_transient_backoff(token, last_error)
                    except Exception:
                        pass
                    logger.warning({
                        "event": "image_stream_cf_failover",
                        "request_token": token,
                        "account_email": account_email,
                        "attempt": pre_conversation_transient_count,
                        "max_attempts": max_attempts,
                        "index": index,
                        "error": last_error[:240],
                    })
                    if pre_conversation_transient_count < max_attempts:
                        wait_secs = max(0.0, float(config.image_pre_conversation_retry_backoff_secs))
                        if wait_secs > 0:
                            time.sleep(min(wait_secs, 1.5))
                        continue
                    raise ImageGenerationError(
                        image_stream_error_message(last_error),
                        account_email=account_email,
                        conversation_id="",
                    ) from exc
                if not emitted_for_token and is_pre_conversation_transient_error(last_error):
                    pre_conversation_transient_count += 1
                    max_attempts = max(1, int(config.image_pre_conversation_max_attempts))
                    try:
                        account_service.record_image_transient_backoff(token, last_error)
                    except Exception:
                        pass
                    if pre_conversation_transient_count < max_attempts:
                        wait_secs = max(0.0, float(config.image_pre_conversation_retry_backoff_secs))
                        logger.warning({
                            "event": "image_pre_conversation_transient_retry",
                            "request_token": token,
                            "account_email": account_email,
                            "attempt": pre_conversation_transient_count,
                            "max_attempts": max_attempts,
                            "index": index,
                            "wait_secs": wait_secs,
                            "error": last_error[:200],
                        })
                        if wait_secs > 0:
                            time.sleep(wait_secs)
                        continue
                    logger.warning({
                        "event": "image_pre_conversation_transient_exhausted",
                        "request_token": token,
                        "account_email": account_email,
                        "attempt": pre_conversation_transient_count,
                        "max_attempts": max_attempts,
                        "index": index,
                        "error": last_error[:200],
                    })
                raise ImageGenerationError(image_stream_error_message(last_error), account_email=account_email, conversation_id="") from exc
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
        finally:
            if pipeline_run is not None:
                if ss_slot is not None:
                    pipeline_run.release_ss(image_index=index, slot=ss_slot)


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    if not is_supported_image_model(request.model):
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)))

    pipeline_run = request.pipeline_run
    if pipeline_run is not None and pipeline_run.needs_ps:
        from services.image_pipeline.prompt_enhance import run_prompt_enhance
        from services.image_pipeline.types import MultiImageMode

        if pipeline_run.multi_image_mode == MultiImageMode.FAST:
            enhanced = run_prompt_enhance(pipeline_run, request)
            request = replace(request, prompt=enhanced)

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(request, 1, 1)
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        for index in range(1, request.n + 1):
            outputs = _generate_single_image(request, index, request.n)
            for output in outputs:
                yield output
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    futures: dict[Any, int] = {}
    errors: dict[int, Exception] = {}
    emitted = False
    last_error = ""
    with ThreadPoolExecutor(max_workers=request.n) as executor:
        for index in range(1, request.n + 1):
            future = executor.submit(_generate_single_image, request, index, request.n)
            futures[future] = index

        for future in as_completed(futures):
            index = futures[future]
            try:
                for output in future.result():
                    emitted = True
                    yield output
            except Exception as exc:
                errors[index] = exc
                last_error = str(exc)
                logger.warning({
                    "event": "image_parallel_generation_error",
                    "index": index,
                    "error": last_error[:300],
                })
                if not emitted:
                    logger.warning({
                        "event": "image_parallel_failure_before_success",
                        "failed_index": index,
                        "error": last_error[:200],
                    })

    if emitted:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })
        return

    if not last_error:
        last_error = "no account in the pool could generate images — check account quota and rate-limit status"
    raise ImageGenerationError(image_stream_error_message(last_error), conversation_id="")


def stream_image_chunks(outputs: Iterable[ImageOutput]) -> Iterator[dict[str, Any]]:
    for output in outputs:
        yield output.to_chunk()


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    for output in outputs:
        created = created or output.created
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    return result
