import base64
import hashlib
import json
import mimetypes
import os
import queue
import random
import re
import threading
import time

import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from collections.abc import Callable
from typing import Any, Dict, Iterator, Optional
from urllib.parse import unquote, urlparse

from curl_cffi import requests
from curl_cffi.requests.models import RequestException, STREAM_END
from PIL import Image

from services.account_service import account_service
from services.config import config
from services.proxy_service import proxy_settings
from utils.helper import UpstreamHTTPError, ensure_ok, iter_sse_payloads, new_uuid, split_image_model
from utils.log import logger
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources
from utils.turnstile import solve_turnstile_token


class InvalidAccessTokenError(RuntimeError):
    pass


class ImagePollTimeoutError(RuntimeError):
    pass


class ImagePollRateLimitedError(ImagePollTimeoutError):
    """Poll aborted after sustained upstream HTTP 429 on conversation/tasks reads."""
    pass


class ImageContentPolicyError(RuntimeError):
    """Raised when image generation is blocked by content policy moderation."""
    pass


class ImageUpstreamTerminalError(RuntimeError):
    """Raised when upstream has reached a non-recoverable terminal state during poll."""

    def __init__(self, message: str, *, code: str = "upstream_terminal_error") -> None:
        super().__init__(message)
        self.code = code


class ImageStreamCancelledError(RuntimeError):
    pass


def _abort_curl_stream_without_waiting(response: requests.Response) -> None:
    """请求 curl_cffi 流停止，但不在当前任务线程等待底层 Future。"""
    quit_now = getattr(response, "quit_now", None)
    if quit_now is not None:
        try:
            quit_now.set()
        except Exception:
            pass

    if bool(getattr(response, "_stream_closed", False)):
        return
    try:
        response._stream_closed = True
    except Exception:
        pass

    stream_task = getattr(response, "stream_task", None)
    curl = getattr(response, "curl", None)

    def finalize() -> None:
        try:
            if stream_task is not None:
                stream_task.result()
        except Exception:
            pass
        try:
            if curl is not None:
                curl.close()
        except Exception:
            pass

    cleanup = threading.Thread(target=finalize, name="image-sse-abort-cleanup", daemon=True)
    cleanup.start()


def _iter_queue_backed_sse_payloads(
    response: requests.Response,
    timeout_secs: float,
    *,
    ready_predicate: Callable[[str], bool],
    ready_label: str,
    cancel_event: threading.Event | None = None,
    post_ready_timeout_secs: float | None = None,
    complete_predicate: Callable[[str], bool] | None = None,
) -> Iterator[str]:
    response_queue = getattr(response, "queue", None)
    if response_queue is None:
        raise TypeError("queue-backed response is required")

    started = time.monotonic()
    ready_deadline = started + timeout_secs
    post_ready_deadline: float | None = None
    ready_seen = False
    complete_seen = False
    pending = b""

    def timeout_error() -> TimeoutError:
        elapsed_secs = time.monotonic() - started
        try:
            logger.warning({
                "event": "image_pre_conversation_sse_ready_deadline",
                "ready_label": ready_label,
                "timeout_secs": timeout_secs,
                "elapsed_secs": round(elapsed_secs, 3),
            })
        except Exception:
            pass
        _abort_curl_stream_without_waiting(response)
        return TimeoutError(
            f"image pre-conversation SSE {ready_label} timeout after {timeout_secs:.0f}s"
        )

    def cancelled_error() -> ImageStreamCancelledError:
        _abort_curl_stream_without_waiting(response)
        return ImageStreamCancelledError("image SSE stream cancelled")

    def mark_ready(payload: str) -> None:
        nonlocal ready_seen, post_ready_deadline
        if ready_seen:
            return
        try:
            ready = bool(ready_predicate(payload))
        except Exception:
            ready = False
        if not ready:
            return
        ready_seen = True
        now = time.monotonic()
        if post_ready_timeout_secs is not None:
            post_ready_deadline = now + max(0.001, float(post_ready_timeout_secs))
        try:
            logger.info({
                "event": "image_pre_conversation_sse_ready",
                "ready_label": ready_label,
                "elapsed_secs": round(now - started, 3),
                "post_ready_timeout_secs": post_ready_timeout_secs,
            })
        except Exception:
            pass

    def iter_payloads(lines: list[bytes]) -> Iterator[str]:
        nonlocal complete_seen
        for raw_line in lines:
            line = raw_line.rstrip(b"\r").decode("utf-8", errors="ignore")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            mark_ready(payload)
            yield payload
            # After conversation_id: leave SSE as soon as image file ids appear
            # (do not wait for upstream EOF — that caused ~90s hangs).
            if (
                ready_seen
                and complete_predicate is not None
                and not complete_seen
                and complete_predicate(payload)
            ):
                complete_seen = True
                try:
                    logger.info({
                        "event": "image_sse_complete_predicate",
                        "ready_label": ready_label,
                        "elapsed_secs": round(time.monotonic() - started, 3),
                    })
                except Exception:
                    pass
                return

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise cancelled_error()

        now = time.monotonic()
        if not ready_seen:
            remaining = ready_deadline - now
            if remaining <= 0:
                raise timeout_error()
        elif post_ready_deadline is not None:
            remaining = post_ready_deadline - now
            if remaining <= 0:
                try:
                    logger.warning({
                        "event": "image_sse_post_ready_soft",
                        "ready_label": ready_label,
                        "timeout_secs": post_ready_timeout_secs,
                        "note": "leave SSE for poll without quit_now abort",
                    })
                except Exception:
                    pass
                # Soft leave: do NOT abort upstream curl — hard abort can cancel
                # in-flight image_gen and leave poll empty.
                return
        else:
            remaining = 0.1 if cancel_event is not None else None

        wait_timeout = min(0.1, remaining) if remaining is not None else None
        try:
            chunk = response_queue.get(timeout=wait_timeout) if wait_timeout is not None else response_queue.get()
        except queue.Empty:
            continue

        if isinstance(chunk, RequestException):
            raise chunk
        if chunk is STREAM_END:
            break
        if isinstance(chunk, str):
            chunk_bytes = chunk.encode("utf-8", errors="ignore")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            chunk_bytes = bytes(chunk)
        else:
            chunk_bytes = str(chunk).encode("utf-8", errors="ignore")

        pending += chunk_bytes
        lines = pending.split(b"\n")
        pending = lines.pop()
        yield from iter_payloads(lines)
        if complete_seen:
            _abort_curl_stream_without_waiting(response)
            return

    if pending:
        yield from iter_payloads([pending])
    if complete_seen:
        _abort_curl_stream_without_waiting(response)
        return

    if not ready_seen:
        _abort_curl_stream_without_waiting(response)
        raise TimeoutError(f"image pre-conversation SSE ended before {ready_label}")


def iter_sse_payloads_until_first_payload(
    response: requests.Response,
    timeout_secs: float,
    *,
    ready_predicate: Callable[[str], bool] | None = None,
    cancel_event: threading.Event | None = None,
    post_ready_timeout_secs: float | None = None,
    complete_predicate: Callable[[str], bool] | None = None,
) -> Iterator[str]:
    """迭代 SSE，并在真正可继续处理的 payload 出现前施加墙钟 deadline。

    默认兼容旧语义：任意非空 ``data:`` 即 ready。图片链路会传入
    conversation_id predicate，防止 ping/control payload 过早解除 deadline。
    complete_predicate：ready 之后命中则提前结束（例如已出现 file_id，不必等 EOF）。
    """
    timeout_secs = max(0.001, float(timeout_secs or 1.0))
    predicate = ready_predicate or (lambda payload: bool(payload))
    ready_label = "conversation metadata" if ready_predicate is not None else "first payload"
    if getattr(response, "queue", None) is not None:
        yield from _iter_queue_backed_sse_payloads(
            response,
            timeout_secs,
            ready_predicate=predicate,
            ready_label=ready_label,
            cancel_event=cancel_event,
            post_ready_timeout_secs=post_ready_timeout_secs,
            complete_predicate=complete_predicate,
        )
        return

    ready_seen = False
    timed_out = False
    post_ready_deadline: float | None = None
    sse_started = time.monotonic()

    def mark_deadline() -> None:
        nonlocal timed_out
        if not ready_seen:
            timed_out = True

    timer = threading.Timer(timeout_secs, mark_deadline)
    timer.daemon = True
    timer.start()
    try:
        for raw_line in response.iter_lines():
            if cancel_event is not None and cancel_event.is_set():
                raise ImageStreamCancelledError("image SSE stream cancelled")
            if timed_out and not ready_seen:
                raise TimeoutError(f"image pre-conversation SSE {ready_label} timeout after {timeout_secs:.0f}s")
            if post_ready_deadline is not None and time.monotonic() >= post_ready_deadline:
                try:
                    logger.warning({
                        "event": "image_sse_post_ready_soft",
                        "ready_label": ready_label,
                        "timeout_secs": post_ready_timeout_secs,
                        "note": "leave SSE for poll without quit_now abort",
                    })
                except Exception:
                    pass
                # Soft leave: do not abort curl mid-generation.
                break
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else str(raw_line)
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if not ready_seen and predicate(payload):
                ready_seen = True
                timer.cancel()
                if post_ready_timeout_secs is not None:
                    post_ready_deadline = time.monotonic() + max(0.001, float(post_ready_timeout_secs))
                try:
                    logger.info({
                        "event": "image_pre_conversation_sse_ready",
                        "ready_label": ready_label,
                        "post_ready_timeout_secs": post_ready_timeout_secs,
                    })
                except Exception:
                    pass
            yield payload
            if (
                ready_seen
                and complete_predicate is not None
                and complete_predicate(payload)
            ):
                try:
                    logger.info({
                        "event": "image_sse_complete_predicate",
                        "ready_label": ready_label,
                        "elapsed_secs": round(time.monotonic() - sse_started, 3),
                    })
                except Exception:
                    pass
                try:
                    _abort_curl_stream_without_waiting(response)
                except Exception:
                    pass
                break
        if timed_out and not ready_seen:
            raise TimeoutError(f"image pre-conversation SSE {ready_label} timeout after {timeout_secs:.0f}s")
    finally:
        timer.cancel()

def _is_invalid_access_token_error(exc: Exception) -> bool:
    text = f"{exc!r} {exc}".lower()
    return (
        "status=401" in text
        or "http 401" in text
        or "token_revoked" in text
        or "token invalidated" in text
        or "invalidated oauth token" in text
    )


@dataclass
class ChatRequirements:
    """保存一次对话请求所需的 sentinel token。"""
    token: str
    proof_token: str = ""
    turnstile_token: str = ""
    so_token: str = ""
    raw_finalize: Optional[Dict[str, Any]] = None


# Captured from chatgpt.com SPA HAR 2026-07-21 (docs/captures/spa/).
DEFAULT_CLIENT_VERSION = "prod-773467609da990104e0f78db96ed90bc4b199c3b"
DEFAULT_CLIENT_BUILD_NUMBER = "8448714"
# Verified with a real pure-HTTP image generation on 2026-07-22.  Keep this
# scoped to the legacy auto-tool image envelope; text/search use current SPA.
PURE_HTTP_IMAGE_CLIENT_VERSION = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"
PURE_HTTP_IMAGE_CLIENT_BUILD_NUMBER = "6708908"
DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
CODEX_IMAGE_MODEL = "codex-gpt-image-2"
CODEX_RESPONSES_MODEL = "gpt-5.5"
SEARCH_MODEL = "gpt-5-5"
SEARCH_TIMEOUT_SECS = 300.0
SEARCH_POLL_INTERVAL_SECS = 1.0
# Reuse homepage PoW scripts across requests for the same access token (sentinel still per-call).
_SEARCH_BOOTSTRAP_TTL_SECS = 600.0
_SEARCH_BOOTSTRAP_CACHE: dict[str, tuple[float, list[str], str]] = {}
# Vision-only prompts: caption alone is enough (skip second web search hop).
_SEARCH_VISION_LOCAL_RE = re.compile(
    r"(主色|颜色|色彩|什么色|图里|图中|图片|这张图|截图|画面|看见什么|看到什么|描述.*(图|画面)|图.*描述|"
    r"what\s+color|describe\s+(the\s+)?(image|picture)|what('?s|\s+is)\s+in\s+(the\s+)?(image|picture))",
    re.I,
)
_SEARCH_WEB_INTENT_RE = re.compile(
    r"(搜索|搜一下|查一下|联网|检索|最新|新闻|价格|官网|链接|出处|来源|谁发|什么时候|几月|多少钱|"
    r"wiki|wikipedia|百科|对比|评测|search|google|who\s+|when\s+|price|official)",
    re.I,
)
SEARCH_DONE_STATUS = {"finished_successfully", "finished_partial_completion", "stop"}
SEARCH_CONVERSATION_ID_RE = re.compile(r'"conversation_id"\s*:\s*"([^"]+)"')
SEARCH_URL_RE = re.compile(r"https?://[^\s\"'<>）)\]}]+")
EDITABLE_FILE_MODEL = "gpt-5-5-thinking"
EDITABLE_FILE_THINKING_EFFORT = "extended"
EDITABLE_FILE_TIMEOUT_SECS = 1200.0
EDITABLE_FILE_POLL_INTERVAL_SECS = 5.0
EDITABLE_FILE_CLIENT_VERSION = "prod-bede35f9dcd856d080e012478f0c1031faa2588e"
EDITABLE_FILE_CLIENT_BUILD_NUMBER = "6631702"
EDITABLE_FILE_PSD_OUTPUT_DIR = "data/files/psd"
EDITABLE_FILE_PPT_OUTPUT_DIR = "data/files/ppt"
EDITABLE_FILE_PPT_PROMPT = """我需要你根据用户的需求，来制作一个可以编辑的PPT，你可以使用Agent来做，你不要再继续询问用户问题，内容风格、版式、配色、内容结构和页面信息你可以自行补充并直接执行。整体的流程如下：
1. 用生图的方式，帮我生成一个精美的产品介绍ppt，5-6个页面
2. 帮我把以上涉及到的所有图像和形状素材拆分成单独png，每个素材单独一张图片，不要有遗漏，让我可以直接在ppt里拼接素材还原，不要文字
3. 利用以上所有图片和形状素材，帮我还原你第一次生成的展示ppt，我需要是可编辑的ppt格式，主要部分需要你单独还原插入，文字需要可以编辑
最后只需要给我生成一个PPT文件，以及生成中遇到的各种素材压缩包zip文件就行。"""
EDITABLE_FILE_PSD_PROMPT = "帮我生成这个图像，把这张海报分成若干图像，包括背景图，每个元素不要改位置，这样子我可以直接在 平时里无需拖动，底色为白色，不要伪透明底。再帮我将以上拆分的图像拼合成一个psd文件，去除白色底，不要改变每个图层的相应位置，保留每个元素所在图层的相应位置，保留每个元素的图层，最后只需要给我输出psd文件，以及每个图层的zip文件"
EDITABLE_ASSET_POINTER_RE = re.compile(r"(?:file-service|sediment)://([A-Za-z0-9_-]+)")
EDITABLE_ZIP_MIME_TYPES = {"application/zip", "application/x-zip-compressed"}
EDITABLE_PSD_MIME_TYPES = {"image/vnd.adobe.photoshop", "application/vnd.adobe.photoshop"}
EDITABLE_PPT_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
}
EDITABLE_PSD_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.(?:psd|zip))", re.IGNORECASE)
EDITABLE_PPT_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.(?:pptx?|zip))", re.IGNORECASE)
FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")
IMAGE_POLL_SETTLE_SECS = 2.0
CODEX_RESPONSES_INSTRUCTIONS = (
    "Use the image_generation tool to create exactly one image for the user's request. "
    "Return the generated image result."
)

# 内容政策违规错误关键词（上游拒绝生成图片的各种表述）
_CONTENT_POLICY_KEYWORDS = (
    # 明确的内容政策违规
    "内容政策", "防护限制", "违反", "moderation", "policy", "blocked",
    # 拒绝生成类
    "不能生成", "无法生成", "不能帮助", "无法帮助",
    # 敏感内容类
    "裸体", "裸露", "色情", "性内容", "未成年",
    # 通用拒绝
    "抱歉，我不能",
)


def _is_content_policy_error(error_msg: str) -> bool:
    """检查错误消息是否为内容政策违规。"""
    if not error_msg:
        return False
    msg_lower = error_msg.lower()
    return any(keyword in msg_lower for keyword in _CONTENT_POLICY_KEYWORDS)


_MISSING_REFERENCE_PATTERNS = (
    re.compile(r"请上传.*参考图", re.IGNORECASE),
    re.compile(r"请先上传.*(?:图|图片|参考)", re.IGNORECASE),
    re.compile(r"还没有.*(?:可用|任何).*(?:人物|参考).*(?:图|图片)", re.IGNORECASE),
    re.compile(r"please\s+upload.*reference", re.IGNORECASE),
    re.compile(r"upload.*reference\s+image", re.IGNORECASE),
    re.compile(r"no\s+(?:usable\s+)?reference\s+image", re.IGNORECASE),
    re.compile(r"don'?t\s+have.*(?:reference|image).*upload", re.IGNORECASE),
)

_INSTANT_LIMIT_PATTERNS = (
    re.compile(r"instant\s+limit", re.IGNORECASE),
    re.compile(r"image\s+creation\s+limit", re.IGNORECASE),
    re.compile(r"limit\s+resets", re.IGNORECASE),
)


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    text_parts: list[str] = []
    if isinstance(content, dict):
        msg_parts = content.get("parts") or []
        if isinstance(msg_parts, list):
            for part in msg_parts:
                if isinstance(part, str) and part.strip():
                    text_parts.append(part.strip())
        text_field = str(content.get("text") or "")
        if text_field.strip():
            text_parts.append(text_field.strip())
    elif isinstance(content, str) and content.strip():
        text_parts.append(content.strip())
    return "\n".join(text_parts)


def _classify_terminal_upstream_text(text: str) -> tuple[str, str] | None:
    """Classify assistant/task text into a terminal upstream error code."""
    if not text or not str(text).strip():
        return None
    clipped = str(text).strip()[:500]
    if _is_content_policy_error(clipped):
        return "content_policy_violation", clipped
    msg_lower = clipped.lower()
    if "image creation limit" in msg_lower or any(pattern.search(clipped) for pattern in _INSTANT_LIMIT_PATTERNS):
        return "image_instant_limit", clipped
    if any(pattern.search(clipped) for pattern in _MISSING_REFERENCE_PATTERNS):
        return "missing_reference_image", clipped
    return None


def _raise_terminal_upstream_block(code: str, message: str) -> None:
    clipped = str(message or "").strip()[:500]
    if code == "content_policy_violation":
        raise ImageContentPolicyError(clipped)
    raise ImageUpstreamTerminalError(clipped, code=code)


@dataclass
class EditableFileArtifact:
    attachment_id: str = ""
    file_id: str = ""
    name: str = ""
    mime_type: str = ""
    create_time: float = 0.0
    author_role: str = ""
    sandbox_path: str = ""
    message_id: str = ""


@dataclass
class EditableFileExportResult:
    conversation_id: str
    primary_path: Path
    zip_path: Path


class OpenAIBackendAPI:
    """ChatGPT Web 后端封装。

    说明：
    - 传入 `access_token` 时，聊天和模型列表都会走已登录链路
      例如 `/backend-api/sentinel/chat-requirements`、`/backend-api/conversation`
    - 不传 `access_token` 时，会走未登录链路
      例如 `/backend-anon/sentinel/chat-requirements`、`/backend-anon/conversation`
    - `stream_conversation()` 是底层统一流式入口
    - 协议兼容转换放在 `services.protocol`
    """

    def __init__(self, access_token: str = "") -> None:
        """初始化后端客户端。

        参数：
        - `access_token`：可选。传入后表示使用已登录链路；不传则使用未登录链路。
        """
        self.base_url = "https://chatgpt.com"
        self.client_version = DEFAULT_CLIENT_VERSION
        self.client_build_number = DEFAULT_CLIENT_BUILD_NUMBER
        self.access_token = access_token
        self.account = {}
        if self.access_token:
            ensure_ready = getattr(account_service, "ensure_account_identity_ready", None)
            if callable(ensure_ready):
                try:
                    self.account = ensure_ready(
                        self.access_token,
                        purpose="backend_session",
                    )
                except ValueError as exc:
                    message = str(exc)
                    # 账号不在池中时（单测/临时 token）降级为 get_account，不阻断 Session 构造。
                    if "account_missing" in message:
                        self.account = account_service.get_account(self.access_token) or {}
                    else:
                        logger.error(
                            {
                                "event": "account_identity_ready_failed",
                                "error": message[:240],
                            }
                        )
                        raise
            else:
                self.account = account_service.get_account(self.access_token) or {}
        self.account = self.account if isinstance(self.account, dict) else {}
        self.fp = self._build_fp()
        self._persist_fp_if_needed()
        self.user_agent = self.fp["user-agent"]
        self.device_id = self.fp["oai-device-id"]
        self.session_id = self.fp["oai-session-id"]
        self.pow_script_sources: list[str] = []
        self.pow_data_build = ""
        self._bootstrap_at = 0.0
        self.progress_callback: Callable[[object], None] | None = None
        self.cancel_event: threading.Event | None = None
        self._progress_started_at = time.time()
        self._progress_last_at = self._progress_started_at
        self._closed = False
        self._resource_session: requests.Session | None = None
        self._resource_session_lock = threading.Lock()
        self.session = requests.Session(**proxy_settings.build_session_kwargs(
            account=self.account,
            impersonate=self.fp["impersonate"],
            verify=True,
            upstream=True,
        ))
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": str(self.fp.get("accept-language") or "").strip()
            or "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        })

    def close(self) -> None:
        """幂等关闭 API/resource session，并停止流式请求 executor。"""
        if getattr(self, "_closed", False):
            return
        self._closed = True

        lock = getattr(self, "_resource_session_lock", None)
        if lock is not None:
            with lock:
                resource_session = getattr(self, "_resource_session", None)
                self._resource_session = None
        else:
            resource_session = getattr(self, "_resource_session", None)

        self._close_session(getattr(self, "session", None))
        self._close_session(resource_session)

    @staticmethod
    def _close_session(session: Any) -> None:
        if session is None:
            return
        executor = getattr(session, "_executor", None)
        try:
            session.close()
        except Exception:
            pass
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            try:
                session._executor = None
            except Exception:
                pass

    def _get_resource_session(self) -> requests.Session:
        """惰性创建同代理的资源 session，避免 API 凭据传播到跨域 URL。"""
        if getattr(self, "_closed", False):
            raise RuntimeError("backend session is closed")
        resource_session = getattr(self, "_resource_session", None)
        if resource_session is not None:
            return resource_session

        lock = getattr(self, "_resource_session_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._resource_session_lock = lock
        with lock:
            if getattr(self, "_closed", False):
                raise RuntimeError("backend session is closed")
            resource_session = getattr(self, "_resource_session", None)
            if resource_session is None:
                resource_session = requests.Session(**proxy_settings.build_session_kwargs(
                    account=self.account,
                    impersonate=self.fp["impersonate"],
                    verify=True,
                    resource=True,
                    upstream=True,
                ))
                resource_session.headers.clear()
                resource_session.headers.update({
                    "User-Agent": self.user_agent,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
                })
                self._resource_session = resource_session
            return resource_session

    def _resource_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造跨域资源请求头；不注入 bearer、OAI 标识或 clearance cookie。"""
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        }
        if extra:
            headers.update(extra)
        return headers

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "OpenAIBackendAPI":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def _build_fp(self) -> Dict[str, str]:
        from services.account_fingerprint import ensure_complete_fp

        fp, _ = ensure_complete_fp(self.account)
        return fp

    def _persist_fp_if_needed(self) -> None:
        """把 ensure 后的完整指纹写回账号，保证后续请求复用同一组字段。"""
        if not self.access_token or not isinstance(self.account, dict):
            return
        from services.account_fingerprint import normalize_fp

        existing = normalize_fp(self.account.get("fp"))
        ensured = normalize_fp(self.fp)
        if existing == ensured:
            return
        try:
            account_service.update_account(self.access_token, {"fp": dict(ensured)}, quiet=True)
            self.account["fp"] = dict(ensured)
        except Exception as exc:
            logger.warning({
                "event": "account_fp_persist_failed",
                "error_type": type(exc).__name__,
                "field_count": len(ensured),
            })

    def _accept_language(self) -> str:
        return str(self.fp.get("accept-language") or "").strip() or (
            "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7"
        )

    def _chat_timezone(self) -> str:
        """Authenticated chat/image tz follows sticky egress; anon keeps LA."""
        if not getattr(self, "access_token", ""):
            return "America/Los_Angeles"
        from services.humanlike_scheduler import resolve_account_tz_name

        pro = config.get_proactive_refresh_settings()
        account_value = getattr(self, "account", None)
        return resolve_account_tz_name(
            account_value if isinstance(account_value, dict) else {},
            timezone_from_egress=bool(pro.get("timezone_from_egress", True)),
            default_tz=str(pro.get("timezone") or "Asia/Singapore"),
        )

    def _oai_language(self) -> str:
        from services.protocol.chatgpt_web_request import oai_language_for_timezone

        return oai_language_for_timezone(self._chat_timezone(), self._accept_language())

    def _text_chat_persist_history(self) -> bool:
        account = self.account if isinstance(self.account, dict) else {}
        if bool(account.get("chat_persist_history")):
            return True
        return bool(getattr(config, "text_chat_persist_history", False))

    def _text_chat_reuse_conversation(self) -> bool:
        account = self.account if isinstance(self.account, dict) else {}
        if bool(account.get("chat_reuse_conversation")):
            return True
        return bool(getattr(config, "text_chat_reuse_conversation", False))

    def _api_headers(self) -> Dict[str, str]:
        """构造仅用于 chatgpt.com API 的完整浏览器请求头。"""
        headers = {
            "User-Agent": self.user_agent,
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "Accept-Language": self._accept_language(),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": self.fp.get("sec-ch-ua") or self.fp.get("sec_ch_ua") or "",
            "Sec-Ch-Ua-Arch": self.fp.get("sec-ch-ua-arch") or '"x86"',
            "Sec-Ch-Ua-Bitness": self.fp.get("sec-ch-ua-bitness") or '"64"',
            "Sec-Ch-Ua-Full-Version": self.fp.get("sec-ch-ua-full-version") or "",
            "Sec-Ch-Ua-Full-Version-List": self.fp.get("sec-ch-ua-full-version-list") or "",
            "Sec-Ch-Ua-Mobile": self.fp.get("sec-ch-ua-mobile") or "?0",
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": self.fp.get("sec-ch-ua-platform") or '"Windows"',
            "Sec-Ch-Ua-Platform-Version": self.fp.get("sec-ch-ua-platform-version") or '"15.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "OAI-Device-Id": self.device_id,
            "OAI-Session-Id": self.session_id,
            "OAI-Language": self._oai_language(),
            "OAI-Client-Version": self.client_version,
            "OAI-Client-Build-Number": self.client_build_number,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _me_light_headers(self, path: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """鉴权探针轻量头：实测比完整 Sec-CH/Target 簇更不易触发 CF 边缘 HTML 403。"""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "Accept-Language": self._accept_language(),
            "OAI-Device-Id": self.device_id,
            "OAI-Session-Id": self.session_id,
        }
        if extra:
            headers.update(extra)
        target_url = path if str(path).startswith("http") else self.base_url + path
        return proxy_settings.build_headers(
            headers=headers,
            target_url=target_url,
            account=self.account,
            upstream=True,
        )

    def _headers(self, path: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造请求头，并补上 web 端要求的 target path/route。"""
        headers = self._api_headers()
        headers["X-OpenAI-Target-Path"] = path
        headers["X-OpenAI-Target-Route"] = path
        if extra:
            headers.update(extra)
        target_url = path if str(path).startswith("http") else self.base_url + path
        return proxy_settings.build_headers(
            headers=headers,
            target_url=target_url,
            account=self.account,
            upstream=True,
        )

    @staticmethod
    def _extract_quota_and_restore_at(limits_progress: list[Any]) -> tuple[int, str | None, bool]:
        for item in limits_progress:
            if isinstance(item, dict) and item.get("feature_name") == "image_gen":
                restore_at = str(item.get("reset_after") or "") or None
                try:
                    remaining = int(item.get("remaining"))
                except (TypeError, ValueError):
                    return 0, restore_at, True
                if remaining < 0:
                    return 0, restore_at, True
                return max(0, remaining), restore_at, False
        return 0, None, True

    @classmethod
    def _looks_like_cf_edge_response(cls, status_code: int, body: str) -> bool:
        """判定 HTTP 响是否像 CF/边缘瞬时拦截（可重试，不等于账号失效）。"""
        status = int(status_code or 0)
        if status not in {403, 429, 502, 503, 520, 521, 522, 523, 524}:
            return False
        body_l = str(body or "").lower()
        if not body_l.strip():
            return status == 403
        return (
            "<html" in body_l
            or "cloudflare" in body_l
            or "cf-error" in body_l
            or "scale-appear" in body_l
            or "just a moment" in body_l
            or "error code: 101" in body_l
        )

    def _raise_on_error(self, response: Any, path: str) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        body = str(getattr(response, "text", "") or "")
        if status == 401:
            raise InvalidAccessTokenError(f"token invalidated ({path})")
        if self._looks_like_cf_edge_response(status, body):
            # 明确前缀，避免前端/运维误读成“必须重登”
            raise RuntimeError(f"cf_edge_block: {path} HTTP {status}")
        raise RuntimeError(f"{path} failed: HTTP {status}")

    def _get_me(self) -> Dict[str, Any]:
        path = "/backend-api/me"
        last_response: Any = None
        use_light = False
        for attempt in range(1, 4):
            headers = self._me_light_headers(path) if use_light else self._headers(path)
            response = self.session.get(self.base_url + path, headers=headers, timeout=20)
            last_response = response
            if response.status_code == 200:
                return response.json()
            if response.status_code == 401:
                self._raise_on_error(response, path)
            if not self._looks_like_cf_edge_response(response.status_code, response.text or ""):
                self._raise_on_error(response, path)
            # 全头触发 CF 后改轻量头重试（Olivia 类间歇 403）
            use_light = True
            if attempt < 3:
                time.sleep(0.35 * attempt)
        assert last_response is not None
        self._raise_on_error(last_response, path)
        raise RuntimeError(f"cf_edge_block: {path} HTTP 403")  # pragma: no cover

    def _get_conversation_init(self) -> Dict[str, Any]:
        path = "/backend-api/conversation/init"
        last_response: Any = None
        use_light = False
        payload = {
            "gizmo_id": None,
            "requested_default_model": None,
            "conversation_id": None,
            "timezone_offset_min": -480,
        }
        for attempt in range(1, 4):
            headers = (
                self._me_light_headers(path, {"Content-Type": "application/json"})
                if use_light
                else self._headers(path, {"Content-Type": "application/json"})
            )
            response = self.session.post(
                self.base_url + path,
                headers=headers,
                json=payload,
                timeout=20,
            )
            last_response = response
            if response.status_code == 200:
                return response.json()
            if response.status_code == 401:
                self._raise_on_error(response, path)
            if not self._looks_like_cf_edge_response(response.status_code, response.text or ""):
                self._raise_on_error(response, path)
            use_light = True
            if attempt < 3:
                time.sleep(0.35 * attempt)
        assert last_response is not None
        self._raise_on_error(last_response, path)
        raise RuntimeError(f"cf_edge_block: {path} HTTP 403")  # pragma: no cover

    def _get_default_account(self) -> Dict[str, Any]:
        path = "/backend-api/accounts/check/v4-2023-04-27"
        last_response: Any = None
        use_light = False
        for attempt in range(1, 4):
            headers = self._me_light_headers(path) if use_light else self._headers(path)
            response = self.session.get(
                self.base_url + path + "?timezone_offset_min=-480",
                headers=headers,
                timeout=20,
            )
            last_response = response
            if response.status_code == 200:
                payload = response.json()
                default_account = ((payload.get("accounts") or {}).get("default") or {}).get("account") or {}
                logger.debug({
                    "event": "backend_user_info_account_payload",
                    "plan_type": default_account.get("plan_type"),
                    "account_user_role": default_account.get("account_user_role"),
                    "account_id": default_account.get("account_id"),
                    "is_deactivated": default_account.get("is_deactivated"),
                    "has_active_subscription": (payload.get("accounts") or {}).get("default", {}).get("entitlement", {}).get("has_active_subscription"),
                    "subscription_plan": (payload.get("accounts") or {}).get("default", {}).get("entitlement", {}).get("subscription_plan"),
                })
                return default_account
            if response.status_code == 401:
                self._raise_on_error(response, path)
            if not self._looks_like_cf_edge_response(response.status_code, response.text or ""):
                self._raise_on_error(response, path)
            use_light = True
            if attempt < 3:
                time.sleep(0.35 * attempt)
        assert last_response is not None
        self._raise_on_error(last_response, path)
        raise RuntimeError(f"cf_edge_block: {path} HTTP 403")  # pragma: no cover

    def get_user_info(self) -> Dict[str, Any]:
        """获取当前 token 的账号信息。"""
        if not self.access_token:
            raise RuntimeError("access_token is required")
        # /me 是最小鉴权探针；失败时不再并发发起额外账号请求。
        me_payload = self._get_me()
        with ThreadPoolExecutor(max_workers=2) as executor:
            init_future = executor.submit(self._get_conversation_init)
            account_future = executor.submit(self._get_default_account)
            init_payload = init_future.result()
            default_account = account_future.result()

        plan_type = str(default_account.get("plan_type") or "free")

        limits_progress = init_payload.get("limits_progress")
        limits_progress = limits_progress if isinstance(limits_progress, list) else []
        quota, restore_at, image_quota_unknown = self._extract_quota_and_restore_at(limits_progress)
        result = {
            "email": me_payload.get("email"),
            "user_id": me_payload.get("id"),
            "type": plan_type,
            "quota": quota,
            "image_quota_unknown": image_quota_unknown,
            "limits_progress": limits_progress,
            "default_model_slug": init_payload.get("default_model_slug"),
            "restore_at": restore_at,
            "status": "正常" if image_quota_unknown and plan_type.lower() != "free" else ("限流" if quota == 0 else "正常"),
        }
        logger.debug({
            "event": "backend_user_info_result",
            "email": result.get("email"),
            "user_id": result.get("user_id"),
            "type": result.get("type"),
            "quota": result.get("quota"),
            "image_quota_unknown": result.get("image_quota_unknown"),
            "default_model_slug": result.get("default_model_slug"),
            "restore_at": result.get("restore_at"),
            "status": result.get("status"),
        })
        return result

    def _bootstrap_headers(self) -> Dict[str, str]:
        """构造首页预热请求头（经 clearance 合并，便于挂 cf_clearance）。"""
        headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": self.fp["sec-ch-ua"],
            "Sec-Ch-Ua-Mobile": self.fp["sec-ch-ua-mobile"],
            "Sec-Ch-Ua-Platform": self.fp["sec-ch-ua-platform"],
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        merged = proxy_settings.build_headers(
            headers=headers,
            target_url=self.base_url + "/",
            account=self.account,
            upstream=True,
        )
        return {str(k): str(v) for k, v in merged.items() if v is not None}

    def _build_requirements(self, data: Dict[str, Any], source_p: str = "") -> ChatRequirements:
        """把 sentinel 响应整理成后续对话需要的 token 集合。"""
        if (data.get("arkose") or {}).get("required"):
            raise RuntimeError("chat requirements requires arkose token, which is not implemented")

        proof_token = ""
        proof_info = data.get("proofofwork") or {}
        if proof_info.get("required"):
            proof_token = build_proof_token(
                proof_info.get("seed", ""),
                proof_info.get("difficulty", ""),
                self.user_agent,
                script_sources=self.pow_script_sources,
                data_build=self.pow_data_build,
            )

        turnstile_token = ""
        turnstile_info = data.get("turnstile") or {}
        if turnstile_info.get("required") and turnstile_info.get("dx"):
            turnstile_token = solve_turnstile_token(turnstile_info["dx"], source_p) or ""

        return ChatRequirements(
            token=data.get("token", ""),
            proof_token=proof_token,
            turnstile_token=turnstile_token,
            so_token=data.get("so_token", ""),
            raw_finalize=data,
        )

    def _conversation_headers(self, path: str, requirements: ChatRequirements) -> Dict[str, str]:
        """根据当前 requirements 构造对话 SSE 请求头。"""
        from services.protocol.chatgpt_web_request import build_chat_headers

        built = self._headers(path, build_chat_headers(requirements))
        try:
            from services.request_shape import header_shape

            logger.info(
                {
                    "event": "request_shape",
                    "purpose": "conversation",
                    "path": path,
                    **header_shape(built),
                }
            )
        except Exception:
            pass
        return built

    def _api_messages_to_conversation_messages(self, messages: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        """把标准 chat messages 转成 web conversation 所需的 messages。"""
        conversation_messages = []
        for item in messages:
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, str):
                conversation_messages.append({
                    "id": new_uuid(),
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": [content]},
                })
                continue
            if not isinstance(content, list):
                raise RuntimeError("only string or list message content is supported")
            text_parts: list[str] = []
            image_inputs: list[tuple[bytes, str]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "")
                if part_type == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part_type == "image":
                    data = part.get("data")
                    mime = str(part.get("mime") or "image/png")
                    if isinstance(data, (bytes, bytearray)):
                        image_inputs.append((bytes(data), mime))
            if not image_inputs:
                conversation_messages.append({
                    "id": new_uuid(),
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": ["".join(text_parts)]},
                })
                continue
            if not self.access_token:
                raise RuntimeError("authenticated upstream account required for image input")
            uploaded: list[Dict[str, Any]] = []
            for idx, (data, mime) in enumerate(image_inputs, start=1):
                ext_part = mime.split("/", 1)[1].split("+")[0] if "/" in mime else "png"
                extension = "jpg" if ext_part == "jpeg" else (ext_part or "png")
                b64 = base64.b64encode(data).decode("ascii")
                uploaded.append(self._upload_image(f"data:{mime};base64,{b64}", f"image_{idx}.{extension}"))
            parts: list[Any] = []
            for ref in uploaded:
                parts.append({
                    "content_type": "image_asset_pointer",
                    "asset_pointer": f"file-service://{ref['file_id']}",
                    "width": ref["width"],
                    "height": ref["height"],
                    "size_bytes": ref["file_size"],
                })
            text = "".join(text_parts)
            if text:
                parts.append(text)
            conversation_messages.append({
                "id": new_uuid(),
                "author": {"role": role},
                "content": {"content_type": "multimodal_text", "parts": parts},
                "metadata": {
                    "attachments": [{
                        "id": ref["file_id"],
                        "mimeType": ref["mime_type"],
                        "name": ref["file_name"],
                        "size": ref["file_size"],
                        "width": ref["width"],
                        "height": ref["height"],
                    } for ref in uploaded],
                },
            })
        return conversation_messages

    @staticmethod
    def _normalize_thinking_effort(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"", "none"}:
            return ""
        if normalized in {"low", "medium", "high"}:
            return normalized
        if normalized in {"xhigh", "extended"}:
            return "extended"
        return ""

    def _conversation_payload(
            self,
            messages: list[Dict[str, Any]],
            model: str,
            timezone: str,
            thinking_effort: str = "",
            *,
            history_and_training_disabled: bool | None = None,
            conversation_id: str = "",
            parent_message_id: str = "",
    ) -> Dict[str, Any]:
        """把标准 messages 构造成 web 对话请求体。"""
        from services.protocol.chatgpt_web_request import build_chat_body, timezone_offset_min

        persist = self._text_chat_persist_history()
        disabled = (
            bool(history_and_training_disabled)
            if history_and_training_disabled is not None
            else (not persist)
        )
        account = self.account if isinstance(self.account, dict) else {}
        email = str(account.get("email") or account.get("id") or "")[:48]
        return build_chat_body(
            messages,
            model,
            timezone=timezone,
            thinking_effort=thinking_effort,
            convert_messages=self._api_messages_to_conversation_messages,
            history_and_training_disabled=disabled,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            timezone_offset=timezone_offset_min(timezone),
            contextual_seed=f"{email}:{timezone}:{len(messages)}",
            contextual_jitter=True,
        )

    def _image_model_slug(self, model: str) -> str:
        """把标准图片模型名映射到底层 model slug。"""
        _, base_model = split_image_model(model)
        if not base_model:
            return "auto"
        if base_model == "gpt-image-2":
            return "auto"
        if base_model == CODEX_IMAGE_MODEL:
            return base_model
        return "auto"

    def _image_headers(
        self,
        path: str,
        requirements: ChatRequirements,
        conduit_token: str = "",
        accept: str = "*/*",
        *,
        spa_tool_path: bool = False,
    ) -> Dict[str, str]:
        """构造图片链路请求头。"""
        from services.protocol.chatgpt_web_request import (
            build_image_prepare_headers,
            build_image_start_headers,
        )

        if spa_tool_path and accept != "text/event-stream":
            # The proven auto-tool prepare request carries no Sentinel token.
            headers = {"Content-Type": "application/json", "Accept": accept or "*/*"}
        elif spa_tool_path or conduit_token or accept == "text/event-stream":
            # SPA 与 classic 均走 build_image_start_headers，保证含 X-Oai-Turn-Trace-Id。
            headers = build_image_start_headers(
                requirements,
                conduit_token,
                spa_tool_path=spa_tool_path,
            )
            if accept:
                headers["Accept"] = accept
        else:
            headers = build_image_prepare_headers(requirements)
            if accept:
                headers["Accept"] = accept
        if spa_tool_path:
            from services.protocol.chatgpt_web_request import oai_language_for_timezone

            # Match the proven curl_cffi envelope and avoid the large Sec-CH /
            # target-route header cluster that increases CF edge variance.
            built = {
                "User-Agent": self.user_agent,
                "Accept-Language": self._accept_language(),
                "OAI-Device-Id": self.device_id,
                "OAI-Session-Id": self.session_id,
                "OAI-Client-Version": PURE_HTTP_IMAGE_CLIENT_VERSION,
                "OAI-Client-Build-Number": PURE_HTTP_IMAGE_CLIENT_BUILD_NUMBER,
                # The accepted legacy image envelope derives this from timezone
                # instead of the account Accept-Language primary tag.
                "OAI-Language": oai_language_for_timezone(self._chat_timezone()),
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
                **headers,
            }
            if self.access_token:
                built["Authorization"] = f"Bearer {self.access_token}"
            built = proxy_settings.build_headers(
                headers=built,
                target_url=self.base_url + path,
                account=self.account,
                upstream=True,
            )
        else:
            built = self._headers(path, headers)
        try:
            from services.request_shape import header_shape

            logger.info(
                {
                    "event": "request_shape",
                    "purpose": "image",
                    "path": path,
                    **header_shape(built),
                }
            )
        except Exception:
            pass
        return built

    def _codex_responses_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _ensure_codex_source_account(self) -> None:
        account = account_service.get_account(self.access_token)
        source_type = str((account or {}).get("source_type") or "web").strip().lower()
        if source_type != "codex":
            raise RuntimeError("codex responses endpoint requires a codex source account")

    @staticmethod
    def _codex_image_input(prompt: str, images: list[str]) -> list[Dict[str, Any]]:
        content: list[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images:
            payload = image if image.startswith("data:image/") else f"data:image/png;base64,{image}"
            content.append({"type": "input_image", "image_url": payload})
        return [{"role": "user", "content": content}]

    @staticmethod
    def _codex_body_preview(body: Any, limit: int = 4000) -> str:
        if isinstance(body, (dict, list)):
            try:
                text = json.dumps(body, ensure_ascii=False)
            except Exception:
                text = repr(body)
        else:
            text = str(body or "")
        return text if len(text) <= limit else text[:limit] + "...[truncated]"

    @staticmethod
    def _codex_event_image_result_lengths(value: Any) -> list[int]:
        if isinstance(value, dict):
            lengths: list[int] = []
            if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
                lengths.append(len(value["result"]))
            for item in value.values():
                lengths.extend(OpenAIBackendAPI._codex_event_image_result_lengths(item))
            return lengths
        if isinstance(value, list):
            lengths: list[int] = []
            for item in value:
                lengths.extend(OpenAIBackendAPI._codex_event_image_result_lengths(item))
            return lengths
        return []

    @staticmethod
    def _codex_event_summary(event: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "type": str(event.get("type") or ""),
            "keys": list(event.keys())[:30],
        }
        for key in ("id", "status", "sequence_number", "response_id", "item_id", "output_index", "content_index"):
            value = event.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = value
        for key in ("response", "item", "output"):
            value = event.get(key)
            if isinstance(value, dict):
                summary[f"{key}_type"] = value.get("type")
                summary[f"{key}_status"] = value.get("status")
                summary[f"{key}_keys"] = list(value.keys())[:30]
            elif isinstance(value, list):
                summary[f"{key}_len"] = len(value)
                summary[f"{key}_types"] = [
                    item.get("type") for item in value[:10] if isinstance(item, dict)
                ]
        error = event.get("error")
        if isinstance(error, dict):
            summary["error"] = {
                key: error.get(key)
                for key in ("type", "code", "message")
                if error.get(key) is not None
            }
        delta = event.get("delta")
        if isinstance(delta, str):
            summary["delta_len"] = len(delta)
            summary["delta_preview"] = delta[:200]
        result_lengths = OpenAIBackendAPI._codex_event_image_result_lengths(event)
        if result_lengths:
            summary["image_result_lengths"] = result_lengths[:10]
        return summary

    def _log_codex_response_failure(
            self,
            path: str,
            status_code: int,
            headers: Any,
            payload: Dict[str, Any],
            body: Any,
    ) -> None:
        request_headers = self._codex_responses_headers()
        safe_request_headers = {
            key: value for key, value in request_headers.items() if key.lower() != "authorization"
        }
        response_headers = dict(headers.items()) if hasattr(headers, "items") else dict(headers or {})
        tool = ((payload.get("tools") or [{}])[0]) if isinstance(payload.get("tools"), list) else {}
        logger.warning({
            "event": "codex_responses_http_error",
            "path": path,
            "status_code": status_code,
            "request": {
                "model": payload.get("model"),
                "tool_model": tool.get("model"),
                "tool_action": tool.get("action"),
                "size": tool.get("size"),
                "quality": tool.get("quality"),
                "image_input_count": max(len((payload.get("input") or [{}])[0].get("content") or []) - 1, 0),
                "prompt_preview": self._codex_body_preview(
                    (((payload.get("input") or [{}])[0].get("content") or [{}])[0].get("text") or ""),
                    500,
                ),
                "headers": safe_request_headers,
            },
            "response": {
                "headers": response_headers,
                "body_preview": self._codex_body_preview(body),
            },
        })

    @staticmethod
    def _iter_codex_response_events(raw: Any) -> Iterator[Dict[str, Any]]:
        content_type = str(raw.headers.get("content-type") or "").lower()
        text = raw.read().decode("utf-8", "replace")
        status_code = getattr(raw, "status", None)
        parse_errors: list[str] = []
        events: list[Dict[str, Any]] = []
        if "application/json" in content_type:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    events.append(data)
            except Exception as exc:
                parse_errors.append(str(exc))
        else:
            lines: list[str] = []
            for line in text.splitlines() + [""]:
                if not line:
                    if lines:
                        payload_text = "\n".join(lines).strip()
                        if payload_text and payload_text != "[DONE]":
                            try:
                                data = json.loads(payload_text)
                            except Exception as exc:
                                parse_errors.append(str(exc))
                                data = None
                            if isinstance(data, dict):
                                events.append(data)
                        lines = []
                elif line.startswith("data:"):
                    lines.append(line[5:].lstrip())

        event_types: Dict[str, int] = {}
        image_result_lengths: list[int] = []
        for event in events:
            event_type = str(event.get("type") or "<missing>")
            event_types[event_type] = event_types.get(event_type, 0) + 1
            image_result_lengths.extend(OpenAIBackendAPI._codex_event_image_result_lengths(event))
        logger.info({
            "event": "codex_responses_response_debug",
            "status_code": status_code,
            "content_type": content_type,
            "response_text_len": len(text),
            "event_count": len(events),
            "event_types": event_types,
            "image_result_lengths": image_result_lengths[:10],
            "parse_error_count": len(parse_errors),
            "parse_errors": parse_errors[:5],
            "event_summaries": [OpenAIBackendAPI._codex_event_summary(event) for event in events[:30]],
            "event_previews": [
                OpenAIBackendAPI._codex_body_preview(event, 1500)
                for event in events[:10]
            ] if not image_result_lengths else [],
            "body_preview": text[:1000] if not events else "",
        })
        for event in events:
            yield event

    def iter_codex_image_response_events(
            self,
            prompt: str,
            images: list[str] | None = None,
            size: str | None = None,
            quality: str = "auto",
    ) -> Iterator[Dict[str, Any]]:
        if not self.access_token:
            raise RuntimeError("access_token is required for codex image endpoints")
        self._ensure_codex_source_account()
        path = "/backend-api/codex/responses"
        payload = {
            "model": CODEX_RESPONSES_MODEL,
            "instructions": CODEX_RESPONSES_INSTRUCTIONS,
            "store": False,
            "input": self._codex_image_input(prompt, images or []),
            "tools": [{
                "type": "image_generation",
                "model": "gpt-image-2",
                "action": "edit" if images else "generate",
                "size": str(size or "1024x1024"),
                "quality": str(quality or "auto"),
                "output_format": "png",
            }],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
        }
        request = urllib.request.Request(
            self.base_url + path,
            json.dumps(payload).encode(),
            self._codex_responses_headers(),
            method="POST",
        )
        account = account_service.get_account(self.access_token) or {}
        token_payload = account_service._decode_jwt_payload(self.access_token)
        auth_claim = token_payload.get("https://api.openai.com/auth")
        auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
        tool = payload["tools"][0]
        logger.info({
            "event": "codex_responses_request_debug",
            "url": self.base_url + path,
            "transport": "urllib.request",
            "timeout_secs": 1200,
            "account_email": str(account.get("email") or "").strip(),
            "source_type": str(account.get("source_type") or "").strip(),
            "account_type": str(account.get("type") or "").strip(),
            "token_claims": {
                "jti": token_payload.get("jti"),
                "iat": token_payload.get("iat"),
                "exp": token_payload.get("exp"),
                "client_id": token_payload.get("client_id"),
                "chatgpt_account_id": auth_claim.get("chatgpt_account_id"),
                "chatgpt_plan_type": auth_claim.get("chatgpt_plan_type"),
                "localhost": auth_claim.get("localhost"),
            },
            "request": {
                "model": payload.get("model"),
                "tool_model": tool.get("model"),
                "tool_action": tool.get("action"),
                "size": tool.get("size"),
                "quality": tool.get("quality"),
                "output_format": tool.get("output_format"),
                "stream": payload.get("stream"),
                "image_input_count": max(len((payload.get("input") or [{}])[0].get("content") or []) - 1, 0),
                "prompt_preview": self._codex_body_preview(
                    (((payload.get("input") or [{}])[0].get("content") or [{}])[0].get("text") or ""),
                    500,
                ),
            },
            "headers": {
                key: value for key, value in self._codex_responses_headers().items()
                if key.lower() != "authorization"
            },
        })
        try:
            with urllib.request.urlopen(request, timeout=1200) as raw:
                yield from self._iter_codex_response_events(raw)
        except urllib.error.HTTPError as error:
            body_text = error.read().decode("utf-8", "replace")
            body: Any = body_text
            try:
                body = json.loads(body_text)
            except Exception:
                pass
            self._log_codex_response_failure(path, error.code, error.headers, payload, body)
            retry_after_header = error.headers.get("Retry-After") if error.headers else None
            retry_after = int(retry_after_header) if str(retry_after_header or "").isdigit() else None
            raise UpstreamHTTPError(path, error.code, body, retry_after=retry_after) from error

    def _prepare_image_conversation(self, prompt: str, requirements: ChatRequirements, model: str) -> str:
        """为图片生成准备 conduit token。"""
        from services.protocol.chatgpt_web_request import (
            build_image_prepare_body,
            image_spa_tool_path_enabled,
            require_conduit_token,
            timezone_offset_min,
        )

        path = "/backend-api/f/conversation/prepare"
        tz = self._chat_timezone()
        account_value = getattr(self, "account", None)
        account = account_value if isinstance(account_value, dict) else {}
        seed = str(account.get("email") or account.get("id") or prompt)[:64]
        spa = image_spa_tool_path_enabled()
        payload = build_image_prepare_body(
            prompt,
            self._image_model_slug(model),
            timezone=tz,
            timezone_offset=timezone_offset_min(tz),
            contextual_seed=seed,
            spa_tool_path=spa,
        )
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, spa_tool_path=spa),
            json=payload,
            timeout=60,
        )
        ensure_ok(response, path)
        raw = ""
        try:
            raw = str((response.json() or {}).get("conduit_token") or "").strip()
        except Exception:
            raw = ""
        if spa:
            return raw
        return require_conduit_token(raw)

    def _decode_image_base64(self, image: str) -> bytes:
        """把 base64 图片字符串或本地路径解码成二进制。"""
        if (
                image
                and len(image) < 512
                and not image.startswith("data:")
                and "\n" not in image
                and "\r" not in image
        ):
            file_path = Path(os.path.expanduser(image))
            if file_path.exists() and file_path.is_file():
                return file_path.read_bytes()
        payload = image.split(",", 1)[1] if image.startswith("data:") and "," in image else image
        return base64.b64decode(payload)

    def _upload_image(self, image: str, file_name: str = "image.png") -> Dict[str, Any]:
        """上传一张 base64 图片，返回底层文件元数据。"""
        last_exc: BaseException | None = None
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                return self._upload_image_once(image, file_name)
            except UpstreamHTTPError as exc:
                last_exc = exc
                code = int(getattr(exc, "status_code", 0) or 0)
                body = str(getattr(exc, "body", "") or getattr(exc, "message", "") or "").lower()
                retryable = code in {500, 502, 503, 504, 408}
                if code == 503 and ("serverbusy" in body or "ingress is over" in body):
                    retryable = True
                if retryable and attempt < max_attempts:
                    time.sleep(min(30.0, 3.0 * (2 ** (attempt - 1))))
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                text = str(exc).lower()
                if attempt < 3 and ("timeout" in text or "timed out" in text):
                    time.sleep(2.0 * attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("image_upload_failed")

    def _upload_image_once(self, image: str, file_name: str = "image.png") -> Dict[str, Any]:
        """Single attempt to upload a base64 image."""
        data = self._decode_image_base64(image)
        if (
                image
                and len(image) < 512
                and not image.startswith("data:")
                and "\n" not in image
                and "\r" not in image
        ):
            candidate_path = Path(os.path.expanduser(image))
            if candidate_path.exists() and candidate_path.is_file():
                file_name = candidate_path.name
        image = Image.open(BytesIO(data))
        width, height = image.size
        mime_type = Image.MIME.get(image.format, "image/png")
        path = "/backend-api/files"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "application/json"}),
            json={"file_name": file_name, "file_size": len(data), "use_case": "multimodal", "width": width,
                  "height": height},
            timeout=60,
        )
        ensure_ok(response, path)
        upload_meta = response.json()
        response = self._get_resource_session().put(
            upload_meta["upload_url"],
            headers=self._resource_headers({
                "Content-Type": mime_type,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-version": "2020-04-08",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
                "Accept": "application/json, text/plain, */*",
            }),
            data=data,
            timeout=120,
        )
        ensure_ok(response, "image_upload")
        path = f"/backend-api/files/{upload_meta['file_id']}/uploaded"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "application/json"}),
            data="{}",
            timeout=60,
        )
        ensure_ok(response, path)
        return {
            "file_id": upload_meta["file_id"],
            "file_name": file_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }

    def _start_image_generation(self, prompt: str, requirements: ChatRequirements, conduit_token: str, model: str,
                                references: Optional[list[Dict[str, Any]]] = None) -> requests.Response:
        """启动图片生成或编辑的 SSE 请求。"""
        from services.protocol.chatgpt_web_request import (
            build_image_start_body,
            image_spa_tool_path_enabled,
            require_conduit_token,
            timezone_offset_min,
        )

        spa = image_spa_tool_path_enabled()
        if not spa:
            conduit_token = require_conduit_token(conduit_token)
        tz = self._chat_timezone()
        account = self.account if isinstance(self.account, dict) else {}
        seed = str(account.get("email") or account.get("id") or prompt)[:64]
        payload = build_image_start_body(
            prompt,
            self._image_model_slug(model),
            references=references or [],
            timezone=tz,
            timezone_offset=timezone_offset_min(tz),
            contextual_seed=seed,
            spa_tool_path=spa,
        )
        path = "/backend-api/f/conversation"
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(
                path,
                requirements,
                "" if spa else conduit_token,
                "text/event-stream",
                spa_tool_path=spa,
            ),
            json=payload,
            timeout=config.image_pre_conversation_timeout_secs,
            stream=True,
        )
        ensure_ok(response, path)
        return response

    def _get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """获取完整 conversation 详情。"""
        path = f"/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        return response.json()

    def _list_recent_conversations(self, limit: int = 5, timeout_secs: float = 10.0) -> list[Dict[str, Any]]:
        """列出最近的对话列表，按更新时间倒序。

        当 SSE 流太短导致 conversation_id 丢失时，可以通过此方法
        查找最近创建的对话来恢复 conversation_id。
        """
        path = f"/backend-api/conversations?offset=0&limit={limit}&order=updated&conversation_filter=all"
        try:
            response = self.session.get(
                self.base_url + path,
                headers=self._headers(path, {"Accept": "application/json"}),
                timeout=timeout_secs,
            )
            ensure_ok(response, path)
            data = response.json()
            return data.get("items") or data.get("conversations") or []
        except Exception as exc:
            logger.debug({"event": "list_conversations_failed", "error": str(exc)})
            return []

    @staticmethod
    def _conversation_timestamp(value: Any) -> float:
        """Normalize upstream epoch seconds/milliseconds or ISO-8601 timestamps."""
        if value in (None, ""):
            return 0.0
        try:
            numeric = float(value)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def find_conversation_by_prompt(self, prompt: str, started_at: float, timeout_secs: float = 10.0) -> str:
        """根据 prompt 和开始时间，从最近对话列表中查找匹配的 conversation_id。

        当 SSE 流太短导致 conversation_id 丢失时，使用此方法恢复。
        通过对比 prompt 关键词和时间戳来匹配最可能的对话。

        参数：
            prompt: 用户输入的 prompt 文本
            started_at: 请求开始的时间戳（epoch seconds）
            timeout_secs: 请求超时秒数

        返回：
            匹配的 conversation_id，如果未找到返回空字符串
        """
        items = self._list_recent_conversations(limit=10, timeout_secs=timeout_secs)
        if not items:
            return ""
        # 筛选在 started_at 之前或附近创建的对话（最多往前 5 分钟）
        # ChatGPT 的 updated_at 通常晚于实际请求时间
        prompt_lower = str(prompt or "").lower().strip()
        best_match = ""
        best_score = 0.0
        for item in items:
            # item 可能是完整的 conversation 对象或摘要
            conv_id = str(item.get("id") or item.get("conversation_id") or "")
            if not conv_id:
                continue
            # 检查时间范围：对话的 updated_at 应该在请求开始时间之后（或附近）
            updated_at = self._conversation_timestamp(item.get("update_time") or item.get("updated_at"))
            if updated_at and started_at and (updated_at < started_at - 30 or updated_at > started_at + 600):
                continue
            # 匹配 prompt 关键词
            title = str(item.get("title") or "").lower()
            # 计算匹配分数
            score = 0.0
            if prompt_lower and title:
                # 简单的关键词匹配
                prompt_words = set(prompt_lower.split())
                title_words = set(title.split())
                common = prompt_words & title_words
                if common:
                    score = len(common) / max(len(prompt_words), 1)
            # 图生图通常标题为 "Image" 开头
            if title.startswith("image"):
                score += 0.3
            if score > best_score:
                best_score = score
                best_match = conv_id
        if best_match and best_score > 0.1:
            logger.info({
                "event": "conversation_prompt_match_found",
                "conversation_id": best_match,
                "match_score": round(best_score, 2),
            })
            return best_match
        # 禁止“取最新对话”兜底：同账号并发时会串会话；无 prompt/时间匹配则放弃恢复。
        logger.info({
            "event": "conversation_prompt_match_missed",
            "started_at": started_at,
            "candidates": len(items),
            "best_score": round(best_score, 2),
        })
        return ""

    @staticmethod
    def _editable_prompt(fixed_prompt: str, user_prompt_text: str) -> str:
        extra = str(user_prompt_text or "").strip()
        return fixed_prompt if not extra else fixed_prompt + "\n\n以下是用户补充需求，请直接结合执行：\n" + extra

    def export_ppt_zip(
            self,
            base64_images: list[str] | None,
            prompt: str,
            output_dir: str | Path = EDITABLE_FILE_PPT_OUTPUT_DIR,
            timeout_secs: float = EDITABLE_FILE_TIMEOUT_SECS,
            poll_interval_secs: float = EDITABLE_FILE_POLL_INTERVAL_SECS,
    ) -> EditableFileExportResult:
        return self._export_editable_file_zip(
            base64_images or [],
            self._editable_prompt(EDITABLE_FILE_PPT_PROMPT, prompt),
            output_dir,
            primary_label="ppt",
            primary_suffixes=(".ppt", ".pptx"),
            primary_mime_types=EDITABLE_PPT_MIME_TYPES,
            primary_mime_keywords=("presentationml.presentation", "ms-powerpoint"),
            primary_default_extension=".pptx",
            export_file_re=EDITABLE_PPT_EXPORT_FILE_RE,
            timeout_secs=timeout_secs,
            poll_interval_secs=poll_interval_secs,
        )

    def export_psd_zip(
            self,
            base64_images: list[str],
            prompt: str,
            output_dir: str | Path = EDITABLE_FILE_PSD_OUTPUT_DIR,
            timeout_secs: float = EDITABLE_FILE_TIMEOUT_SECS,
            poll_interval_secs: float = EDITABLE_FILE_POLL_INTERVAL_SECS,
    ) -> EditableFileExportResult:
        if not base64_images:
            raise ValueError("base64_images is empty")
        return self._export_editable_file_zip(
            base64_images,
            self._editable_prompt(EDITABLE_FILE_PSD_PROMPT, prompt),
            output_dir,
            primary_label="psd",
            primary_suffixes=(".psd",),
            primary_mime_types=EDITABLE_PSD_MIME_TYPES,
            primary_mime_keywords=("photoshop",),
            primary_default_extension=".psd",
            export_file_re=EDITABLE_PSD_EXPORT_FILE_RE,
            timeout_secs=timeout_secs,
            poll_interval_secs=poll_interval_secs,
        )

    def _export_editable_file_zip(
            self,
            base64_images: list[str],
            prompt: str,
            output_dir: str | Path,
            *,
            primary_label: str,
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
            export_file_re: re.Pattern[str],
            timeout_secs: float,
            poll_interval_secs: float,
    ) -> EditableFileExportResult:
        if not self.access_token:
            raise RuntimeError("access_token is required for editable file export")
        self.client_version = EDITABLE_FILE_CLIENT_VERSION
        self.client_build_number = EDITABLE_FILE_CLIENT_BUILD_NUMBER
        output_path = Path(output_dir).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        uploaded = [self._upload_editable_base64_image(item, index) for index, item in enumerate(base64_images, start=1)]
        conduit_token = self._prepare_editable_conversation(prompt, [item["mime_type"] for item in uploaded])
        conversation_id = self._run_editable_conversation(prompt, uploaded, conduit_token)
        artifacts = self._wait_editable_output_artifacts(
            conversation_id,
            primary_label,
            primary_suffixes,
            primary_mime_types,
            primary_mime_keywords,
            export_file_re,
            timeout_secs,
            poll_interval_secs,
        )
        downloaded = [self._download_editable_artifact(
            conversation_id,
            item,
            output_path,
            primary_mime_types,
            primary_mime_keywords,
            primary_default_extension,
        ) for item in artifacts]
        primary_path = next((item for item in downloaded if item.suffix.lower() in primary_suffixes), None)
        zip_path = next((item for item in downloaded if item.suffix.lower() == ".zip"), None)
        if not primary_path or not zip_path:
            raise RuntimeError(f"download finished but did not get both {primary_label} and zip files: {downloaded}")
        return EditableFileExportResult(conversation_id=conversation_id, primary_path=primary_path, zip_path=zip_path)

    def _upload_editable_base64_image(self, base64_image: str, index: int) -> Dict[str, Any]:
        data, file_name, mime_type, width, height = self._decode_editable_base64_image(base64_image, index)
        path = "/backend-api/files"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json"}),
            json={
                "file_name": file_name,
                "file_size": len(data),
                "use_case": "multimodal",
                "timezone_offset_min": -480,
                "reset_rate_limits": False,
                "store_in_library": True,
                "library_persistence_mode": "opportunistic",
            },
            timeout=60,
        )
        ensure_ok(response, path)
        payload = response.json()
        upload_url = str(payload.get("upload_url") or "")
        file_id = str(payload.get("file_id") or "")
        if not upload_url or not file_id:
            raise RuntimeError(f"invalid upload response: {payload}")
        response = self._get_resource_session().put(
            upload_url,
            headers=self._resource_headers({
                "Content-Type": mime_type,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-version": "2020-04-08",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
                "Accept": "application/json, text/plain, */*",
            }),
            data=data,
            timeout=120,
        )
        ensure_ok(response, "image_upload")
        path = f"/backend-api/files/{file_id}/uploaded"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json"}),
            data="{}",
            timeout=60,
        )
        ensure_ok(response, path)
        return {
            "file_id": file_id,
            "library_file_id": str(payload.get("library_file_id") or ""),
            "file_name": file_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }

    def _decode_editable_base64_image(self, base64_image: str, index: int) -> tuple[bytes, str, str, int, int]:
        raw = str(base64_image or "").strip()
        if not raw:
            raise ValueError("base64 image is empty")
        mime_type = ""
        payload = raw
        match = re.match(r"^data:([^;]+);base64,(.*)$", raw, re.IGNORECASE | re.DOTALL)
        if match:
            mime_type = str(match.group(1) or "").strip().lower()
            payload = str(match.group(2) or "").strip()
        data = base64.b64decode(payload)
        image = Image.open(BytesIO(data))
        image.load()
        width, height = image.size
        mime_type = Image.MIME.get(image.format, mime_type or "image/png")
        extension = mimetypes.guess_extension(mime_type) or ".png"
        return data, f"image_{index}{extension}", mime_type, width, height

    def _prepare_editable_conversation(self, prompt: str, attachment_mime_types: list[str]) -> str:
        path = "/backend-api/f/conversation/prepare"
        payload: Dict[str, Any] = {
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": "client-created-root",
            "model": EDITABLE_FILE_MODEL,
            "client_prepare_state": "success",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": [],
            "partial_query": {"id": new_uuid(), "author": {"role": "user"}, "content": {"content_type": "text", "parts": [prompt]}},
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
            "thinking_effort": EDITABLE_FILE_THINKING_EFFORT,
        }
        if attachment_mime_types:
            payload["attachment_mime_types"] = attachment_mime_types
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json", "X-Conduit-Token": "no-token"}),
            json=payload,
            timeout=60,
        )
        ensure_ok(response, path)
        conduit_token = str(response.json().get("conduit_token") or "")
        if not conduit_token:
            raise RuntimeError(f"missing conduit_token: {response.text}")
        return conduit_token

    def _run_editable_conversation(self, prompt: str, uploaded: list[Dict[str, Any]], conduit_token: str) -> str:
        self._bootstrap()
        requirements = self._get_chat_requirements()
        message: Dict[str, Any] = {"id": new_uuid(), "author": {"role": "user"}, "create_time": time.time()}
        if uploaded:
            parts = [{
                "content_type": "image_asset_pointer",
                "asset_pointer": f"sediment://{item['file_id']}",
                "size_bytes": item["file_size"],
                "width": item["width"],
                "height": item["height"],
            } for item in uploaded]
            parts.append(prompt)
            message["content"] = {"content_type": "multimodal_text", "parts": parts}
            message["metadata"] = {
                "attachments": [{
                    "id": item["file_id"],
                    "size": item["file_size"],
                    "name": item["file_name"],
                    "mime_type": item["mime_type"],
                    "width": item["width"],
                    "height": item["height"],
                    "source": "library",
                    "library_file_id": item["library_file_id"],
                    "is_big_paste": False,
                } for item in uploaded],
                "developer_mode_connector_ids": [],
                "selected_sources": [],
                "selected_github_repos": [],
                "selected_all_github_repos": False,
                "serialization_metadata": {"custom_symbol_offsets": []},
            }
        else:
            message["content"] = {"content_type": "text", "parts": [prompt]}
        path = "/backend-api/f/conversation"
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json={
                "action": "next",
                "messages": [message],
                "parent_message_id": "client-created-root",
                "model": EDITABLE_FILE_MODEL,
                "client_prepare_state": "sent",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "client_contextual_info": {
                    "is_dark_mode": False,
                    "time_since_loaded": 401,
                    "page_height": 1138,
                    "page_width": 803,
                    "pixel_ratio": 2,
                    "screen_height": 1440,
                    "screen_width": 2560,
                    "app_name": "chatgpt.com",
                },
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
                "thinking_effort": EDITABLE_FILE_THINKING_EFFORT,
            },
            timeout=300,
            stream=True,
        )
        ensure_ok(response, path)
        conversation_id = ""
        try:
            for payload in iter_sse_payloads(response):
                if payload == "[DONE]":
                    break
                conversation_id = conversation_id or self._find_editable_value(payload, "conversation_id")
        finally:
            response.close()
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id

    def _wait_editable_output_artifacts(
            self,
            conversation_id: str,
            primary_label: str,
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            export_file_re: re.Pattern[str],
            timeout_secs: float,
            poll_interval_secs: float,
    ) -> list[EditableFileArtifact]:
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            try:
                conversation = self._get_editable_conversation_detail(conversation_id)
            except UpstreamHTTPError as exc:
                if exc.status_code in {404, 409, 423, 429, 500, 502, 503, 504}:
                    time.sleep(poll_interval_secs)
                    continue
                raise
            targeted = self._pick_editable_target_artifacts(
                self._extract_editable_artifacts(conversation, export_file_re),
                primary_suffixes,
                primary_mime_types,
                primary_mime_keywords,
            )
            if targeted:
                return targeted
            time.sleep(poll_interval_secs)
        raise RuntimeError(f"timed out waiting for {primary_label}/zip outputs")

    def _get_editable_conversation_detail(self, conversation_id: str) -> Dict[str, Any]:
        path = f"/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=self._editable_conversation_document_headers(path, conversation_id), timeout=60)
        ensure_ok(response, path)
        return response.json()

    def _editable_browser_headers(self, path: str, conversation_id: str) -> Dict[str, str]:
        headers = self._headers(path, {"Accept": "*/*"})
        headers["Referer"] = f"{self.base_url}/c/{conversation_id}"
        return headers

    def _editable_conversation_document_headers(self, path: str, conversation_id: str) -> Dict[str, str]:
        headers = self._editable_browser_headers(path, conversation_id)
        headers["X-OpenAI-Target-Route"] = "/backend-api/conversation/{conversation_id}"
        return headers

    def _extract_editable_artifacts(self, conversation: Dict[str, Any], export_file_re: re.Pattern[str]) -> list[EditableFileArtifact]:
        artifacts: dict[str, EditableFileArtifact] = {}
        for node in sorted((conversation.get("mapping") or {}).values(), key=lambda item: float(((item or {}).get("message") or {}).get("create_time") or 0.0)):
            message = (node or {}).get("message") or {}
            message_id = str(message.get("id") or "")
            author_role = str(((message.get("author") or {}).get("role") or "")).strip()
            if author_role not in {"assistant", "tool"}:
                continue
            create_time = float(message.get("create_time") or 0.0)
            message_text = self._editable_message_text(message)
            for artifact in self._extract_editable_message_artifacts(message, message_id, author_role, create_time, export_file_re):
                key = artifact.attachment_id or artifact.file_id or artifact.name or artifact.sandbox_path
                if key:
                    artifacts[key] = self._merge_editable_artifact(artifacts.get(key), artifact)
            for export_path in self._extract_editable_export_paths(message_text, export_file_re):
                inferred = EditableFileArtifact(name=Path(export_path).name, create_time=create_time, author_role=author_role, sandbox_path=export_path, message_id=message_id)
                artifacts[export_path] = self._merge_editable_artifact(artifacts.get(export_path), inferred)
        return sorted(artifacts.values(), key=lambda item: item.create_time)

    def _extract_editable_message_artifacts(
            self,
            message: Dict[str, Any],
            message_id: str,
            author_role: str,
            create_time: float,
            export_file_re: re.Pattern[str],
    ) -> list[EditableFileArtifact]:
        artifacts: list[EditableFileArtifact] = []
        for item in (message.get("metadata") or {}).get("attachments") or []:
            artifact = self._editable_artifact_from_dict(item, message_id, author_role, create_time, export_file_re)
            if artifact:
                artifacts.append(artifact)
        for obj in self._walk_search_dicts(message):
            artifact = self._editable_artifact_from_dict(obj, message_id, author_role, create_time, export_file_re)
            if artifact:
                artifacts.append(artifact)
        return artifacts

    def _editable_artifact_from_dict(
            self,
            payload: Dict[str, Any],
            message_id: str,
            author_role: str,
            create_time: float,
            export_file_re: re.Pattern[str],
    ) -> EditableFileArtifact | None:
        if not ({"id", "file_id", "asset_pointer", "name", "file_name", "filename", "mime_type", "mimeType"} & set(payload.keys())):
            return None
        attachment_id = self._match_editable_file_id(str(payload.get("id") or ""))
        file_id = self._match_editable_file_id(str(payload.get("file_id") or ""))
        name = self._sanitize_editable_filename(str(payload.get("name") or payload.get("file_name") or payload.get("filename") or payload.get("title") or "").strip())
        mime_type = self._clean_editable_mime_type(payload.get("mime_type") or payload.get("mimeType") or "")
        for asset_id in EDITABLE_ASSET_POINTER_RE.findall(str(payload.get("asset_pointer") or "")):
            attachment_id = attachment_id or asset_id
            file_id = file_id or asset_id
        if not attachment_id or not file_id:
            ids = self._extract_editable_file_ids(json.dumps(payload, ensure_ascii=False))
            attachment_id = attachment_id or (ids[0] if ids else "")
            file_id = file_id or (ids[0] if ids else "")
        if not attachment_id and not file_id:
            return None
        return EditableFileArtifact(
            attachment_id=attachment_id,
            file_id=file_id,
            name=name,
            mime_type=mime_type,
            create_time=create_time,
            author_role=author_role,
            sandbox_path=(self._extract_editable_export_paths(payload, export_file_re) or [""])[0],
            message_id=message_id,
        )

    def _pick_editable_target_artifacts(
            self,
            artifacts: list[EditableFileArtifact],
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
    ) -> list[EditableFileArtifact]:
        primary = next((item for item in reversed(artifacts) if self._looks_like_editable_primary(item, primary_suffixes, primary_mime_types, primary_mime_keywords)), None)
        zip_item = next((item for item in reversed(artifacts) if self._looks_like_editable_zip(item)), None)
        return [primary, zip_item] if primary and zip_item else []

    def _download_editable_artifact(
            self,
            conversation_id: str,
            artifact: EditableFileArtifact,
            output_dir: Path,
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
    ) -> Path:
        download_url = self._resolve_editable_download_url(conversation_id, artifact)
        if not download_url:
            raise RuntimeError(f"download url not found for artifact: {artifact}")
        response = self._get_resource_session().get(
            download_url,
            headers=self._resource_headers({"Accept": "*/*"}),
            timeout=300,
        )
        ensure_ok(response, "artifact_download")
        content_type = self._clean_editable_mime_type(response.headers.get("Content-Type") or artifact.mime_type)
        file_name = self._resolve_editable_output_name(artifact, response.url, response.headers.get("Content-Disposition"), content_type, primary_mime_types, primary_mime_keywords, primary_default_extension)
        target_path = self._unique_editable_path(output_dir / file_name)
        target_path.write_bytes(response.content)
        return target_path

    def _resolve_editable_download_url(self, conversation_id: str, artifact: EditableFileArtifact) -> str:
        ids: list[str] = []
        for item in (artifact.attachment_id, artifact.file_id):
            if item and item not in ids:
                ids.append(item)
        if artifact.sandbox_path and artifact.message_id:
            path = f"/backend-api/conversation/{conversation_id}/interpreter/download"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/conversation/{conversation_id}/interpreter/download"),
                params={"message_id": artifact.message_id, "sandbox_path": artifact.sandbox_path},
                timeout=60,
            )
            if 200 <= response.status_code < 300:
                url = self._download_url_from_response(response)
                if url:
                    return url
        for attachment_id in ids:
            path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"),
                timeout=60,
            )
            if 200 <= response.status_code < 300:
                url = self._download_url_from_response(response)
                if url:
                    return url
        for file_id in ids:
            path = f"/backend-api/files/download/{file_id}"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/files/download/{file_id}"),
                params={"post_id": "", "inline": "false"},
                timeout=60,
            )
            if 200 <= response.status_code < 300:
                url = self._download_url_from_response(response)
                if url:
                    return url
        for file_id in ids:
            path = f"/backend-api/files/{file_id}/download"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/files/download/{file_id}"),
                timeout=60,
            )
            if 200 <= response.status_code < 300:
                url = self._download_url_from_response(response)
                if url:
                    return url
        return ""

    def _editable_download_headers(self, path: str, conversation_id: str, route: str) -> Dict[str, str]:
        headers = self._editable_browser_headers(path, conversation_id)
        headers["X-OpenAI-Target-Route"] = route
        return headers

    @staticmethod
    def _download_url_from_response(response: Any) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        return str(payload.get("download_url") or payload.get("url") or "")

    def _resolve_editable_output_name(
            self,
            artifact: EditableFileArtifact,
            final_url: str,
            content_disposition: str | None,
            content_type: str,
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
    ) -> str:
        file_name = self._sanitize_editable_filename(artifact.name)
        if not file_name and artifact.sandbox_path:
            file_name = self._sanitize_editable_filename(Path(artifact.sandbox_path).name)
        if not file_name:
            file_name = self._sanitize_editable_filename(self._editable_filename_from_content_disposition(content_disposition or ""))
        if not file_name:
            file_name = self._sanitize_editable_filename(Path(urlparse(final_url).path).name)
        extension = self._editable_extension_from_mime_type(content_type, primary_mime_types, primary_mime_keywords, primary_default_extension)
        return f"artifact{extension}" if not file_name else (file_name if Path(file_name).suffix else file_name + extension)

    def _find_editable_value(self, payload: Any, key: str) -> str:
        if isinstance(payload, str):
            match = SEARCH_CONVERSATION_ID_RE.search(payload) if key == "conversation_id" else None
            if match:
                return match.group(1)
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return ""
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            return next((found for item in payload.values() if (found := self._find_editable_value(item, key))), "")
        if isinstance(payload, list):
            return next((found for item in payload if (found := self._find_editable_value(item, key))), "")
        return ""

    def _extract_editable_file_ids(self, text: str) -> list[str]:
        values: list[str] = []
        for item in EDITABLE_ASSET_POINTER_RE.findall(text):
            if item not in values:
                values.append(item)
        for item in FILE_ID_RE.findall(text):
            if item not in values:
                values.append(item)
        return values

    @staticmethod
    def _match_editable_file_id(value: str) -> str:
        match = FILE_ID_RE.search(value)
        return match.group(1) if match else ""

    @staticmethod
    def _clean_editable_mime_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        return text.split(";", 1)[0] if "/" in text else ""

    def _looks_like_editable_primary(
            self,
            artifact: EditableFileArtifact,
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
    ) -> bool:
        path, name, mime = artifact.sandbox_path.lower(), artifact.name.lower(), artifact.mime_type
        return name.endswith(primary_suffixes) or path.endswith(primary_suffixes) or mime in primary_mime_types or any(keyword in mime for keyword in primary_mime_keywords)

    @staticmethod
    def _looks_like_editable_zip(artifact: EditableFileArtifact) -> bool:
        path, name, mime = artifact.sandbox_path.lower(), artifact.name.lower(), artifact.mime_type
        return name.endswith(".zip") or path.endswith(".zip") or mime in EDITABLE_ZIP_MIME_TYPES or mime.endswith("/zip")

    @staticmethod
    def _editable_extension_from_mime_type(
            mime_type: str,
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
    ) -> str:
        if mime_type in primary_mime_types or any(keyword in mime_type for keyword in primary_mime_keywords):
            return primary_default_extension
        if mime_type in EDITABLE_ZIP_MIME_TYPES or mime_type.endswith("/zip"):
            return ".zip"
        return mimetypes.guess_extension(mime_type) or ""

    @staticmethod
    def _editable_filename_from_content_disposition(content_disposition: str) -> str:
        extended_match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
        if extended_match:
            return unquote(extended_match.group(1)).strip()
        plain_match = re.search(r'filename="([^"]+)"', content_disposition, re.IGNORECASE)
        return plain_match.group(1).strip() if plain_match else ""

    @staticmethod
    def _sanitize_editable_filename(value: str) -> str:
        return Path(str(value or "").strip()).name.replace("\x00", "").strip()

    @staticmethod
    def _unique_editable_path(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"failed to allocate output path for {path}")

    @staticmethod
    def _merge_editable_artifact(current: EditableFileArtifact | None, latest: EditableFileArtifact) -> EditableFileArtifact:
        if current is None:
            return latest
        return EditableFileArtifact(
            attachment_id=latest.attachment_id or current.attachment_id,
            file_id=latest.file_id or current.file_id,
            name=latest.name or current.name,
            mime_type=latest.mime_type or current.mime_type,
            create_time=max(current.create_time, latest.create_time),
            author_role=latest.author_role or current.author_role,
            sandbox_path=latest.sandbox_path or current.sandbox_path,
            message_id=latest.message_id or current.message_id,
        )

    @staticmethod
    def _editable_message_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content") or {}
        parts: list[str] = []
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                parts.append(content["text"])
            for part in content.get("parts") or []:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.extend(str(part.get(key) or "") for key in ("text", "asset_pointer", "model_set_context") if part.get(key))
        if isinstance(message.get("content"), str):
            parts.append(str(message["content"]))
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _extract_editable_export_paths(payload: Any, export_file_re: re.Pattern[str]) -> list[str]:
        if isinstance(payload, str):
            text = payload
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except Exception:
                text = str(payload)
        values: list[str] = []
        for item in export_file_re.findall(text):
            path = str(item or "").strip()
            if path and path not in values:
                values.append(path)
        return values

    @staticmethod
    def _is_vision_local_search_prompt(prompt: str) -> bool:
        text = str(prompt or "").strip()
        if not text:
            return False
        if _SEARCH_WEB_INTENT_RE.search(text):
            return False
        return bool(_SEARCH_VISION_LOCAL_RE.search(text))

    @staticmethod
    def _search_completion_chunk(
        model: str,
        *,
        completion_id: str,
        created: int,
        content: str = "",
        role: str | None = None,
        finish_reason: str | None = None,
        sources: list[Dict[str, str]] | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        delta: Dict[str, Any] = {}
        if role:
            delta["role"] = role
        if content:
            delta["content"] = content
        chunk: Dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if sources is not None:
            chunk["sources"] = sources
        if extra:
            chunk.update(extra)
        return chunk

    def search(
        self,
        prompt: str,
        model: str = SEARCH_MODEL,
        timeout_secs: float = SEARCH_TIMEOUT_SECS,
        poll_interval_secs: float = SEARCH_POLL_INTERVAL_SECS,
        images: list[str] | None = None,
    ) -> Dict[str, Any]:
        answer = ""
        sources: list[Dict[str, str]] = []
        meta: Dict[str, Any] = {}
        for chunk in self.iter_search(
            prompt,
            model=model,
            timeout_secs=timeout_secs,
            poll_interval_secs=poll_interval_secs,
            images=images,
        ):
            if not isinstance(chunk, dict):
                continue
            choice = (chunk.get("choices") or [{}])[0] if isinstance(chunk.get("choices"), list) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            piece = str(delta.get("content") or "")
            if piece:
                answer += piece
            if isinstance(chunk.get("sources"), list):
                sources = [item for item in chunk["sources"] if isinstance(item, dict)]
            for key in ("conversation_id", "assistant_message_id", "status", "_vision_fast_path"):
                if key in chunk:
                    meta[key] = chunk[key]
            if choice.get("finish_reason"):
                meta["status"] = str(choice.get("finish_reason") or meta.get("status") or "stop")
        return {
            "conversation_id": str(meta.get("conversation_id") or ""),
            "status": str(meta.get("status") or "stop"),
            "answer": answer,
            "sources": sources,
            "assistant_message_id": str(meta.get("assistant_message_id") or ""),
            "create_time": time.time(),
            **({"_vision_fast_path": True} if meta.get("_vision_fast_path") else {}),
        }

    def iter_search(
        self,
        prompt: str,
        model: str = SEARCH_MODEL,
        timeout_secs: float = SEARCH_TIMEOUT_SECS,
        poll_interval_secs: float = SEARCH_POLL_INTERVAL_SECS,
        images: list[str] | None = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield OpenAI-style chat.completion.chunk events as search text arrives."""
        if not self.access_token:
            raise RuntimeError("access_token is required for search")
        from services.protocol.conversation import sanitize_output_text

        completion_id = f"chatcmpl-{new_uuid().replace('-', '')}"
        created = int(time.time())
        emitted = ""
        role_sent = False

        def emit(full_text: str, *, finish: bool = False, sources: list | None = None, extra: Dict[str, Any] | None = None):
            nonlocal emitted, role_sent
            clean = sanitize_output_text(str(full_text or "")).strip("\n")
            # Prefer prefix growth; if upstream rewrites, emit the remainder best-effort.
            if clean.startswith(emitted):
                piece = clean[len(emitted):]
                emitted = clean
            elif not emitted:
                piece = clean
                emitted = clean
            else:
                piece = ""
            if piece or (finish and not role_sent):
                role = None
                if not role_sent:
                    role = "assistant"
                    role_sent = True
                yield self._search_completion_chunk(
                    model,
                    completion_id=completion_id,
                    created=created,
                    content=piece,
                    role=role,
                    finish_reason="stop" if finish else None,
                    sources=sources if finish else None,
                    extra=extra,
                )
            elif finish:
                yield self._search_completion_chunk(
                    model,
                    completion_id=completion_id,
                    created=created,
                    finish_reason="stop",
                    sources=sources or [],
                    extra=extra,
                )

        uploaded = self._upload_search_images(images or [])
        text_prompt = str(prompt or "").strip() or "请结合附件进行联网搜索。"
        if uploaded:
            self._ensure_bootstrap()
            vision_local = self._is_vision_local_search_prompt(text_prompt)
            caption = ""
            for partial in self._iter_caption_search_images(text_prompt, uploaded, model=model):
                caption = partial
                if vision_local:
                    yield from emit(partial)
            if caption and vision_local:
                yield from emit(
                    caption,
                    finish=True,
                    sources=[],
                    extra={"_vision_fast_path": True, "status": "stop"},
                )
                return
            if caption:
                text_prompt = f"{text_prompt}\n\n----- 图片内容理解 -----\n{caption}\n-----"
                uploaded = []
            else:
                logger.warning("search image caption failed; falling back to multimodal search")

        user_message = self._build_search_user_message(text_prompt, uploaded)
        self._ensure_bootstrap()
        # prepare (conduit) does not need sentinel; overlap with chat-requirements.
        with ThreadPoolExecutor(max_workers=2) as pool:
            prep_fut = pool.submit(
                self._prepare_search_conversation,
                text_prompt,
                model,
                user_message,
            )
            req_fut = pool.submit(self._get_chat_requirements)
            conduit_token = prep_fut.result(timeout=60)
            requirements = req_fut.result(timeout=60)

        conversation_id = ""
        saw_stream_text = False
        for partial in self._iter_search_conversation_sse(
            text_prompt,
            conduit_token,
            model,
            requirements=requirements,
            user_message=user_message,
        ):
            conversation_id = conversation_id or str(partial.get("conversation_id") or "")
            text = str(partial.get("text") or "")
            if text:
                saw_stream_text = True
                yield from emit(text)

        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")

        # Prefer sources via a short poll; if stream already had the answer, don't wait long.
        poll_every = min(float(poll_interval_secs or 1.0), 0.4)
        poll_budget = 8.0 if saw_stream_text else float(timeout_secs)
        deadline = time.time() + poll_budget
        last_result: Dict[str, Any] | None = None
        last_answer = ""
        stable_hits = 0
        while time.time() < deadline:
            try:
                last_result = self._extract_search_result(
                    conversation_id,
                    self._get_search_conversation(conversation_id),
                )
            except UpstreamHTTPError as exc:
                if exc.status_code not in {404, 409, 423, 429, 500, 502, 503, 504}:
                    raise
                if saw_stream_text and emitted:
                    break
                time.sleep(poll_every)
                continue
            answer = str((last_result or {}).get("answer") or "")
            if answer:
                yield from emit(answer)
                status = str((last_result or {}).get("status") or "")
                sources = list((last_result or {}).get("sources") or [])
                if status in SEARCH_DONE_STATUS or (saw_stream_text and sources):
                    yield from emit(
                        answer or emitted,
                        finish=True,
                        sources=sources,
                        extra={
                            "conversation_id": conversation_id,
                            "assistant_message_id": str((last_result or {}).get("assistant_message_id") or ""),
                            "status": status or "stop",
                        },
                    )
                    return
                stable_hits = stable_hits + 1 if answer == last_answer else 0
                last_answer = answer
                if stable_hits >= 1 and (len(answer) >= 24 or saw_stream_text):
                    yield from emit(
                        answer,
                        finish=True,
                        sources=sources,
                        extra={
                            "conversation_id": conversation_id,
                            "assistant_message_id": str((last_result or {}).get("assistant_message_id") or ""),
                            "status": status or "stop",
                        },
                    )
                    return
                if stable_hits >= 2:
                    yield from emit(
                        answer,
                        finish=True,
                        sources=sources,
                        extra={
                            "conversation_id": conversation_id,
                            "assistant_message_id": str((last_result or {}).get("assistant_message_id") or ""),
                            "status": status or "stop",
                        },
                    )
                    return
            elif saw_stream_text and emitted:
                # Answer already streamed; one empty poll is enough to try sources.
                time.sleep(poll_every)
                if last_result is not None:
                    break
            time.sleep(poll_every)

        if last_result and (last_result.get("answer") or emitted):
            yield from emit(
                str(last_result.get("answer") or emitted),
                finish=True,
                sources=list(last_result.get("sources") or []),
                extra={
                    "conversation_id": conversation_id,
                    "assistant_message_id": str(last_result.get("assistant_message_id") or ""),
                    "status": str(last_result.get("status") or "stop"),
                },
            )
            return
        if emitted:
            yield from emit(emitted, finish=True, sources=[])
            return
        raise RuntimeError(f"timed out waiting for search result: {conversation_id}")

    def _iter_caption_search_images(
        self,
        prompt: str,
        uploaded: list[Dict[str, Any]],
        model: str = SEARCH_MODEL,
    ) -> Iterator[str]:
        """Yield growing caption text from multimodal SSE (then short poll fallback)."""
        from services.protocol.conversation import iter_conversation_payloads, sanitize_output_text

        ask = f"用中文一句话描述图中关键内容（颜色/主体/文字）。问题：{prompt}"
        message = self._build_search_user_message(ask, uploaded)
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        metadata = {**metadata, "system_hints": []}
        message = {**message, "metadata": metadata, "create_time": time.time()}
        try:
            self._ensure_bootstrap()
            prepare_path = "/backend-api/f/conversation/prepare"
            with ThreadPoolExecutor(max_workers=2) as pool:
                prep_fut = pool.submit(
                    lambda: self.session.post(
                        self.base_url + prepare_path,
                        headers=self._headers(
                            prepare_path,
                            {"Accept": "*/*", "Content-Type": "application/json", "X-Conduit-Token": "no-token"},
                        ),
                        json={
                            "action": "next",
                            "fork_from_shared_post": False,
                            "parent_message_id": "client-created-root",
                            "model": model,
                            "client_prepare_state": "success",
                            "timezone_offset_min": -480,
                            "timezone": "Asia/Shanghai",
                            "conversation_mode": {"kind": "primary_assistant"},
                            "system_hints": [],
                            "partial_query": {
                                "id": message.get("id") or new_uuid(),
                                "author": {"role": "user"},
                                "content": message.get("content"),
                            },
                            "supports_buffering": True,
                            "supported_encodings": ["v1"],
                            "client_contextual_info": {"app_name": "chatgpt.com"},
                        },
                        timeout=45,
                    )
                )
                req_fut = pool.submit(self._get_chat_requirements)
                prepare_resp = prep_fut.result(timeout=60)
                requirements = req_fut.result(timeout=60)
            ensure_ok(prepare_resp, prepare_path)
            conduit_token = str(prepare_resp.json().get("conduit_token") or "")
            if not conduit_token:
                return
            path = "/backend-api/f/conversation"
            response = self.session.post(
                self.base_url + path,
                headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
                json={
                    "action": "next",
                    "messages": [message],
                    "parent_message_id": "client-created-root",
                    "model": model,
                    "client_prepare_state": "success",
                    "timezone_offset_min": -480,
                    "timezone": "Asia/Shanghai",
                    "conversation_mode": {"kind": "primary_assistant"},
                    "enable_message_followups": False,
                    "system_hints": [],
                    "supports_buffering": True,
                    "supported_encodings": ["v1"],
                    "force_use_search": False,
                    "client_contextual_info": {
                        "is_dark_mode": False,
                        "time_since_loaded": 36,
                        "page_height": 925,
                        "page_width": 886,
                        "pixel_ratio": 2,
                        "screen_height": 1440,
                        "screen_width": 2560,
                        "app_name": "chatgpt.com",
                    },
                },
                timeout=90,
                stream=True,
            )
            ensure_ok(response, path)
            conversation_id = ""
            streamed = ""
            try:
                for event in iter_conversation_payloads(iter_sse_payloads(response)):
                    conversation_id = conversation_id or str(event.get("conversation_id") or "")
                    if event.get("type") == "conversation.delta":
                        streamed = sanitize_output_text(str(event.get("text") or streamed)).strip()
                        if streamed:
                            yield streamed
                    if event.get("type") == "conversation.done":
                        break
            finally:
                response.close()
            streamed = sanitize_output_text(streamed).strip()
            if streamed:
                yield streamed
                return
            if not conversation_id:
                return
            deadline = time.time() + 8
            last = ""
            while time.time() < deadline:
                result = self._extract_search_result(conversation_id, self._get_search_conversation(conversation_id))
                answer = sanitize_output_text(str(result.get("answer") or "")).strip()
                if answer:
                    last = answer
                    yield answer
                    if str(result.get("status") or "") in SEARCH_DONE_STATUS or len(answer) >= 8:
                        return
                time.sleep(0.35)
            if last:
                yield last
        except Exception as exc:
            logger.warning({"event": "search_image_caption_error", "error": str(exc)})
            return

    def _caption_search_images(self, prompt: str, uploaded: list[Dict[str, Any]], model: str = SEARCH_MODEL) -> str:
        last = ""
        for partial in self._iter_caption_search_images(prompt, uploaded, model=model):
            last = partial
        return last
    def _upload_search_images(self, images: list[str]) -> list[Dict[str, Any]]:
        uploaded: list[Dict[str, Any]] = []
        for idx, image in enumerate(images, start=1):
            raw = str(image or "").strip()
            if not raw:
                continue
            if len(uploaded) >= 4:
                break
            mime = "image/png"
            if raw.startswith("data:") and ";base64," in raw:
                header = raw.split(";base64,", 1)[0]
                if header.startswith("data:") and "/" in header:
                    mime = header[5:]
            ext = "jpg" if "jpeg" in mime else (mime.split("/", 1)[-1] or "png")
            uploaded.append(self._upload_image(raw, f"search_{idx}.{ext}"))
        return uploaded

    @staticmethod
    def _build_search_user_message(prompt: str, uploaded: list[Dict[str, Any]]) -> Dict[str, Any]:
        text = str(prompt or "").strip() or "请结合附件进行联网搜索。"
        message_id = new_uuid()
        if not uploaded:
            return {
                "id": message_id,
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [text]},
                "metadata": {
                    "developer_mode_connector_ids": [],
                    "selected_github_repos": [],
                    "selected_all_github_repos": False,
                    "system_hints": ["search"],
                    "serialization_metadata": {"custom_symbol_offsets": []},
                },
            }
        parts: list[Any] = []
        for ref in uploaded:
            parts.append({
                "content_type": "image_asset_pointer",
                "asset_pointer": f"file-service://{ref['file_id']}",
                "width": ref["width"],
                "height": ref["height"],
                "size_bytes": ref["file_size"],
            })
        parts.append(text)
        return {
            "id": message_id,
            "author": {"role": "user"},
            "create_time": time.time(),
            "content": {"content_type": "multimodal_text", "parts": parts},
            "metadata": {
                "attachments": [{
                    "id": ref["file_id"],
                    "mimeType": ref["mime_type"],
                    "name": ref["file_name"],
                    "size": ref["file_size"],
                    "width": ref["width"],
                    "height": ref["height"],
                } for ref in uploaded],
                "developer_mode_connector_ids": [],
                "selected_github_repos": [],
                "selected_all_github_repos": False,
                "system_hints": ["search"],
                "serialization_metadata": {"custom_symbol_offsets": []},
            },
        }

    def _prepare_search_conversation(
        self,
        prompt: str,
        model: str,
        user_message: Dict[str, Any] | None = None,
    ) -> str:
        path = "/backend-api/f/conversation/prepare"
        message = user_message or self._build_search_user_message(prompt, [])
        partial = {
            "id": message.get("id") or new_uuid(),
            "author": {"role": "user"},
            "content": message.get("content") or {"content_type": "text", "parts": [prompt]},
        }
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json", "X-Conduit-Token": "no-token"}),
            json={
                "action": "next",
                "fork_from_shared_post": False,
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "system_hints": ["search"],
                "partial_query": partial,
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "client_contextual_info": {"app_name": "chatgpt.com"},
            },
            timeout=60,
        )
        ensure_ok(response, path)
        token = str(response.json().get("conduit_token") or "")
        if not token:
            raise RuntimeError("missing conduit_token")
        return token

    def _run_search_conversation(
        self,
        prompt: str,
        conduit_token: str,
        model: str,
        user_message: Dict[str, Any] | None = None,
    ) -> str:
        conversation_id = ""
        for partial in self._iter_search_conversation_sse(
            prompt, conduit_token, model, user_message=user_message
        ):
            conversation_id = conversation_id or str(partial.get("conversation_id") or "")
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id

    def _run_search_conversation_streamed(
        self,
        prompt: str,
        conduit_token: str,
        model: str,
        user_message: Dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        conversation_id = ""
        early_text = ""
        for partial in self._iter_search_conversation_sse(
            prompt, conduit_token, model, user_message=user_message
        ):
            conversation_id = conversation_id or str(partial.get("conversation_id") or "")
            if partial.get("text"):
                early_text = str(partial.get("text") or "")
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id, early_text

    def _iter_search_conversation_sse(
        self,
        prompt: str,
        conduit_token: str,
        model: str,
        *,
        requirements: ChatRequirements | None = None,
        user_message: Dict[str, Any] | None = None,
    ) -> Iterator[Dict[str, str]]:
        """Yield {conversation_id, text} as search SSE deltas arrive (do not buffer until DONE)."""
        from services.protocol.conversation import iter_conversation_payloads, sanitize_output_text

        if requirements is None:
            requirements = self._get_chat_requirements()
        path = "/backend-api/f/conversation"
        message = user_message or self._build_search_user_message(prompt, [])
        if "create_time" not in message:
            message = {**message, "create_time": time.time()}
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json={
                "action": "next",
                "messages": [message],
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "force_use_search": True,
                "client_reported_search_source": "conversation_composer_web_icon",
                "client_contextual_info": {
                    "is_dark_mode": False,
                    "time_since_loaded": 36,
                    "page_height": 925,
                    "page_width": 886,
                    "pixel_ratio": 2,
                    "screen_height": 1440,
                    "screen_width": 2560,
                    "app_name": "chatgpt.com",
                },
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
            },
            timeout=300,
            stream=True,
        )
        ensure_ok(response, path)
        conversation_id = ""
        try:
            for event in iter_conversation_payloads(iter_sse_payloads(response)):
                conversation_id = conversation_id or str(event.get("conversation_id") or "")
                if event.get("type") != "conversation.delta":
                    continue
                text = sanitize_output_text(str(event.get("text") or "")).strip()
                if not text:
                    continue
                low = text.lower()
                if low.startswith("search(") or 'search("' in low[:80] or "search('" in low[:80]:
                    continue
                yield {"conversation_id": conversation_id, "text": text}
        finally:
            response.close()
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")

    def _wait_search_result(self, conversation_id: str, timeout_secs: float, poll_interval_secs: float) -> Dict[str, Any]:
        deadline = time.time() + timeout_secs
        last_result: Dict[str, Any] | None = None
        last_answer = ""
        stable_hits = 0
        while time.time() < deadline:
            try:
                last_result = self._extract_search_result(conversation_id, self._get_search_conversation(conversation_id))
            except UpstreamHTTPError as exc:
                if exc.status_code not in {404, 409, 423, 429, 500, 502, 503, 504}:
                    raise
            if last_result and last_result.get("answer"):
                if last_result.get("status") in SEARCH_DONE_STATUS:
                    return last_result
                answer = str(last_result.get("answer") or "")
                stable_hits = stable_hits + 1 if answer == last_answer else 0
                last_answer = answer
                # One confirmed stable snapshot is enough when answer is already substantive.
                if stable_hits >= 1 and len(answer) >= 24:
                    return last_result
                if stable_hits >= 2:
                    return last_result
            time.sleep(poll_interval_secs)
        if last_result:
            return last_result
        raise RuntimeError(f"timed out waiting for search result: {conversation_id}")

    def _get_search_conversation(self, conversation_id: str) -> Dict[str, Any]:
        path = f"/backend-api/conversation/{conversation_id}"
        headers = self._headers(path, {"Accept": "*/*"})
        headers["Referer"] = f"{self.base_url}/c/{conversation_id}"
        headers["X-OpenAI-Target-Route"] = "/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=headers, timeout=60)
        ensure_ok(response, path)
        return response.json()

    def _extract_search_result(self, conversation_id: str, conversation: Dict[str, Any]) -> Dict[str, Any]:
        from services.protocol.conversation import sanitize_output_text

        messages = []
        for node in (conversation.get("mapping") or {}).values():
            message = (node or {}).get("message") or {}
            if ((message.get("author") or {}).get("role") or "") == "assistant":
                messages.append(message)

        def message_score(item: Dict[str, Any]) -> float:
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            content_type = str(content.get("content_type") or "").strip().lower()
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            finish_details = metadata.get("finish_details") if isinstance(metadata.get("finish_details"), dict) else {}
            finish_type = str(finish_details.get("type") or metadata.get("status") or "").strip().lower()
            text = sanitize_output_text(self._search_message_text(item))
            score = float(item.get("create_time") or 0.0)
            if content_type in {"text", "multimodal_text"}:
                score += 1_000_000_000_000.0
            if content_type == "code":
                score -= 1_000_000_000_000.0
            if text:
                score += 1_000_000_000.0 + float(len(text))
            if finish_type in {str(x).lower() for x in SEARCH_DONE_STATUS}:
                score += 100_000_000_000.0
            return score

        message = max(messages, key=message_score) if messages else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        finish_details = metadata.get("finish_details") if isinstance(metadata.get("finish_details"), dict) else {}
        answer = sanitize_output_text(self._search_message_text(message))
        sources = self._extract_search_sources(message)
        # Also harvest sources from sibling assistant nodes (citations often live elsewhere)
        for sibling in messages:
            for item in self._extract_search_sources(sibling):
                if item["url"] and all(existing["url"] != item["url"] for existing in sources):
                    sources.append(item)
        for url in SEARCH_URL_RE.findall(answer):
            url = self._clean_search_url(url)
            if url and all(item["url"] != url for item in sources):
                sources.append({"title": "", "url": url, "snippet": "", "source_type": ""})
        return {
            "conversation_id": conversation_id,
            # Only trust finish/status on the chosen assistant message (do not deep-walk nested "status")
            "status": str(finish_details.get("type") or metadata.get("status") or "").strip(),
            "answer": answer,
            "sources": sources,
            "assistant_message_id": str(message.get("id") or ""),
            "create_time": float(message.get("create_time") or 0.0),
        }

    def _extract_search_sources(self, payload: Any) -> list[Dict[str, str]]:
        sources: list[Dict[str, str]] = []
        for obj in self._walk_search_dicts(payload):
            metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            url = self._clean_search_url(obj.get("url") or obj.get("link") or obj.get("source_url") or metadata.get("url"))
            if url and all(item["url"] != url for item in sources):
                sources.append({
                    "title": str(obj.get("title") or obj.get("name") or obj.get("source") or "").strip(),
                    "url": url,
                    "snippet": str(obj.get("snippet") or obj.get("text") or obj.get("description") or "").strip(),
                    "source_type": str(obj.get("type") or obj.get("source_type") or "").strip(),
                })
        return sources

    def _search_message_text(self, message: Any) -> str:
        content = message.get("content") if isinstance(message, dict) else {}
        parts = []
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                parts.append(content["text"])
            for part in content.get("parts") or []:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.extend(str(part.get(key) or "") for key in ("text", "summary", "content") if part.get(key))
        elif isinstance(content, str):
            parts.append(content)
        return "\n".join(part.strip() for part in parts if str(part).strip()).strip()

    def _find_search_value(self, payload: Any, key: str) -> str:
        if isinstance(payload, str):
            match = SEARCH_CONVERSATION_ID_RE.search(payload) if key == "conversation_id" else None
            if match:
                return match.group(1)
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return ""
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            return next((found for item in payload.values() if (found := self._find_search_value(item, key))), "")
        if isinstance(payload, list):
            return next((found for item in payload if (found := self._find_search_value(item, key))), "")
        return ""

    def _walk_search_dicts(self, payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, dict):
            return [payload, *(item for value in payload.values() for item in self._walk_search_dicts(value))]
        if isinstance(payload, list):
            return [item for value in payload for item in self._walk_search_dicts(value)]
        return []

    def _clean_search_url(self, value: Any) -> str:
        return str(value or "").strip().rstrip(".,;，。；")

    @staticmethod
    def _add_unique(values: list[str], candidates: list[str]) -> None:
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)

    @classmethod
    def _extract_image_reference_ids(cls, payload: Any) -> tuple[list[str], list[str]]:
        file_ids: list[str] = []
        sediment_ids: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                # 只提取真正的图片文件 ID（file_00000000... 格式）和 file-service:// URI
                cls._add_unique(file_ids, FILE_SERVICE_ID_RE.findall(value))
                cls._add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(value))
                cls._add_unique(sediment_ids, SEDIMENT_ID_RE.findall(value))
                return
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return file_ids, sediment_ids

    @classmethod
    def _has_image_asset_pointer(cls, payload: Any) -> bool:
        if isinstance(payload, dict):
            if str(payload.get("content_type") or "") == "image_asset_pointer":
                return True
            asset_pointer = str(payload.get("asset_pointer") or "")
            if asset_pointer.startswith(("file-service://", "sediment://")):
                return True
            return any(cls._has_image_asset_pointer(item) for item in payload.values())
        if isinstance(payload, list):
            return any(cls._has_image_asset_pointer(item) for item in payload)
        return False

    def _extract_image_tool_records(self, data: Dict[str, Any]) -> list[Dict[str, Any]]:
        """从 conversation 明细里提取图片工具输出记录。"""
        mapping = data.get("mapping") or {}
        records = []
        for message_id, node in mapping.items():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            metadata = message.get("metadata") or {}
            content = message.get("content") or {}
            role = str(author.get("role") or "").strip().lower()
            if role not in {"tool", "assistant"}:
                continue
            is_image_gen = metadata.get("async_task_type") == "image_gen"
            has_asset_pointer = self._has_image_asset_pointer(content) or self._has_image_asset_pointer(metadata)
            if role == "assistant" and not (is_image_gen or has_asset_pointer):
                continue
            file_ids, sediment_ids = self._extract_image_reference_ids({"content": content, "metadata": metadata})
            if not is_image_gen and not has_asset_pointer and not file_ids and not sediment_ids:
                continue
            records.append(
                {"message_id": message_id, "create_time": message.get("create_time") or 0, "file_ids": file_ids,
                 "sediment_ids": sediment_ids})
        return sorted(records, key=lambda item: item["create_time"])

    @staticmethod
    def _conversation_has_image_gen_activity(data: Dict[str, Any]) -> bool:
        """True when conversation already has an image_gen async task in flight."""
        mapping = data.get("mapping") or {}
        for node in mapping.values():
            message = (node or {}).get("message") or {}
            metadata = message.get("metadata") or {}
            if str(metadata.get("async_task_type") or "").strip().lower() == "image_gen":
                return True
        return False

    @staticmethod
    def _find_terminal_upstream_block_in_conversation(data: Dict[str, Any]) -> tuple[str, str] | None:
        """Detect non-recoverable upstream terminal states in a conversation document."""
        title = str(data.get("title") or "").strip()
        if title and "image creation limit" in title.lower():
            return "image_instant_limit", title[:500]

        mapping = data.get("mapping") or {}
        for node in mapping.values():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            role = str(author.get("role") or "").strip().lower()
            if role not in {"assistant", "tool"}:
                continue
            msg_text = _extract_message_text(message)
            if not msg_text:
                continue
            hit = _classify_terminal_upstream_text(msg_text)
            if hit:
                return hit
        return None

    @staticmethod
    def _find_content_policy_error_in_conversation(data: Dict[str, Any]) -> str:
        """从对话文档中查找内容政策违规错误消息。

        上游拒绝生成图片时，错误消息会出现在 assistant 消息的文本中。
        本方法遍历所有 assistant/tool 消息，检查是否包含内容政策违规关键词，
        如果匹配则返回该消息文本（截断至 500 字符），否则返回空字符串。
        """
        hit = OpenAIBackendAPI._find_terminal_upstream_block_in_conversation(data)
        if hit and hit[0] == "content_policy_violation":
            return hit[1]
        return ""

    def _resolve_poll_initial_wait_secs(self, sse_image_gen_ms: float | None = None) -> float:
        base = float(config.image_poll_initial_wait_secs)
        try:
            early_ms = float(config.image_poll_early_sse_ms)
        except (TypeError, ValueError):
            early_ms = 5000.0
        try:
            early_wait = float(config.image_poll_early_sse_initial_wait_secs)
        except (TypeError, ValueError):
            early_wait = 25.0
        if sse_image_gen_ms is not None and float(sse_image_gen_ms) >= float(config.image_poll_after_slow_sse_ms):
            # SSE already waited for upstream image_gen; skip the long post-stream settle.
            return min(base, float(config.image_poll_after_slow_sse_initial_wait_secs))
        if sse_image_gen_ms is not None and float(sse_image_gen_ms) < early_ms:
            return max(base, early_wait)
        return base

    @staticmethod
    def _resolve_poll_max_upstream_gets() -> int | None:
        """Explicit conversation GET cap, or ``None`` to derive it from the wall.

        The hidden default of 24 made the 300s/360s wall budgets unreachable
        (audit 28 §B6 / fix A4-4); an operator who did configure the key keeps it.
        """
        if hasattr(config, "image_poll_max_upstream_gets_explicit"):
            explicit = config.image_poll_max_upstream_gets_explicit
            if explicit is None:
                return None
            try:
                return max(1, int(explicit))
            except (TypeError, ValueError):
                return None
        # Older config module without the explicit accessor: keep legacy behaviour.
        try:
            return max(1, int(config.image_poll_max_upstream_gets))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _resolve_poll_timeout_config_key(timeout_secs: float) -> tuple[str, str]:
        """Map a wall budget back to the mode and the config key that governs it.

        Per audit 28 §5 the top-level ``image_*_poll_timeout_secs`` keys are never
        read: the nested ``image_task_queue.*`` values win. Naming the top-level key
        in a timeout error sends operators to a knob that does nothing.
        """
        try:
            wall = float(timeout_secs)
        except (TypeError, ValueError):
            return "", "image_poll_timeout_secs"
        try:
            multi = float(config.image_multi_reference_poll_timeout_secs)
            edit = float(config.image_edit_poll_timeout_secs)
            generation = float(config.image_generation_poll_timeout_secs)
        except Exception:
            return "", "image_poll_timeout_secs"
        if wall >= multi:
            return "multi_reference", "image_task_queue.multi_reference_poll_timeout_secs"
        if wall >= edit:
            return "edit", "image_task_queue.edit_poll_timeout_secs"
        if wall >= generation:
            return "generation", "image_task_queue.generation_poll_timeout_secs"
        return "base", "image_poll_timeout_secs"

    def _poll_image_results(
            self,
            conversation_id: str,
            timeout_secs: float = 120.0,
            initial_file_ids: list[str] | None = None,
            initial_sediment_ids: list[str] | None = None,
            *,
            sse_image_gen_ms: float | None = None,
    ) -> tuple[list[str], list[str]]:
        """Poll the conversation document until image file ids appear or budget runs out.

        - Sleeps image_poll_initial_wait_secs first (default 10s, +jitter). ChatGPT
          image generation takes ~30s; polling immediately wastes requests and trips
          a transient 429 the upstream returns within ~200ms of the SSE stream
          closing (the conversation document is not yet committed).
        - Subsequent polls are image_poll_interval_secs apart (default 10s).
        - On upstream 429 / 5xx or network errors, backs off exponentially
          (capped at 16s, +jitter) honoring Retry-After when present.
        - All sleeps stay within timeout_secs; on exhaustion raises ImagePollTimeoutError.
        """
        def _raise_if_cancelled() -> None:
            cancel_event = getattr(self, "cancel_event", None)
            if cancel_event is not None and cancel_event.is_set():
                raise ImageStreamCancelledError("image poll cancelled")

        def _cancel_aware_sleep(seconds: float) -> None:
            sleep_for = max(0.0, float(seconds))
            if sleep_for <= 0:
                _raise_if_cancelled()
                return
            cancel_event = getattr(self, "cancel_event", None)
            if cancel_event is not None:
                if cancel_event.wait(timeout=sleep_for):
                    raise ImageStreamCancelledError("image poll cancelled")
                return
            time.sleep(sleep_for)

        _raise_if_cancelled()
        from services.image_poll_budget import ImagePollBudget

        interval = float(config.image_poll_interval_secs)
        poll_mode, poll_timeout_key = self._resolve_poll_timeout_config_key(timeout_secs)
        budget = ImagePollBudget.create(
            timeout_secs=timeout_secs,
            # None => derive the GET cap from the wall budget (audit 28 §B6 / fix
            # A4-4). An explicitly configured image_poll_max_upstream_gets still
            # wins verbatim so existing deployments keep their behaviour.
            max_conversation_gets=self._resolve_poll_max_upstream_gets(),
            max_tasks_gets=config.image_poll_max_tasks_gets,
            tasks_every_n_attempts=config.image_poll_tasks_every_n_attempts,
            poll_interval_secs=interval,
            mode=poll_mode,
            timeout_config_key=poll_timeout_key,
        )
        initial_wait = self._resolve_poll_initial_wait_secs(sse_image_gen_ms)
        file_ids: list[str] = []
        sediment_ids: list[str] = []
        self._add_unique(file_ids, initial_file_ids or [])
        self._add_unique(sediment_ids, initial_sediment_ids or [])
        has_initial_ids = bool(file_ids or sediment_ids)
        sediment_fast = bool(
            config.image_sediment_fast_poll_enabled
            and sediment_ids
            and not file_ids
        )
        settle_secs = float(config.image_settle_secs_sediment if sediment_fast else config.image_settle_secs)
        check_before_hit = False if sediment_fast else bool(config.image_check_before_hit_enabled)
        settle_enabled = bool(config.image_settle_enabled) and not (
            sediment_fast and float(config.image_settle_secs_sediment) <= 0
        )
        # 勿用 initial ids 预填 last_hit_key：否则「SSE 刚给出 file_id」会在首次 poll
        # 就被当成已确认，estuary 尚未就绪时下载会 404 File link not found。
        last_hit_key: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        logger.info({
            "event": "image_poll_start",
            "conversation_id": conversation_id,
            "timeout_secs": timeout_secs,
            "initial_wait_secs": initial_wait,
            "interval_secs": interval,
            "initial_file_ids": file_ids,
            "initial_sediment_ids": sediment_ids,
            "poll_budget": budget.snapshot(),
        })

        def _remaining() -> float:
            return budget.remaining_wall()

        if has_initial_ids and settle_enabled:
            settle_for = min(settle_secs, max(0.0, _remaining()))
            if settle_for > 0:
                _cancel_aware_sleep(settle_for)
        elif initial_wait > 0:
            jitter = random.uniform(0, min(2.0, initial_wait * 0.2))
            sleep_for = min(initial_wait + jitter, max(0.0, _remaining()))
            if sleep_for > 0:
                _cancel_aware_sleep(sleep_for)

        def _retry_sleep(
            reason: str,
            status_code: int | None,
            error: str | None,
            retry_after: int | None,
            *,
            base_secs: float | None = None,
        ) -> bool:
            # retry_after=0 means "retry immediately" — must not be coerced via falsy check.
            if base_secs is not None:
                base = float(base_secs)
            elif retry_after is not None:
                base = retry_after
            else:
                base = min(2 ** min(budget.attempt, 4), 16)
            backoff = base + random.uniform(0, 0.5)
            remaining = _remaining()
            if remaining <= 0:
                return False
            sleep_for = min(backoff, remaining)
            log_payload: Dict[str, Any] = {
                "event": "image_poll_retry",
                "conversation_id": conversation_id,
                "attempt": budget.attempt,
                "reason": reason,
                "sleep_secs": round(sleep_for, 2),
                "poll_budget": budget.snapshot(),
            }
            if status_code is not None:
                log_payload["status_code"] = status_code
            if error is not None:
                log_payload["error"] = error
            logger.warning(log_payload)
            _cancel_aware_sleep(sleep_for)
            return True

        last_task_error = ""
        cf_streak = 0
        upstream_429_streak = 0
        try:
            rate_limit_abort_streak = max(1, int(config.image_poll_429_abort_streak))
        except (TypeError, ValueError):
            rate_limit_abort_streak = 3
        skip_tasks_on_cf = False
        cf_abort_streak = max(1, int(config.image_poll_cf_abort_streak))

        def _note_poll_cf(source: str, exc: BaseException) -> None:
            nonlocal cf_streak, skip_tasks_on_cf
            cf_streak += 1
            if source == "tasks":
                skip_tasks_on_cf = True
            logger.warning({
                "event": "image_poll_cf_edge",
                "conversation_id": conversation_id,
                "attempt": budget.attempt,
                "source": source,
                "cf_streak": cf_streak,
                "cf_abort_streak": cf_abort_streak,
                "error": str(exc)[:240],
                "poll_budget": budget.snapshot(),
            })

        def _raise_poll_cf_abort(source: str) -> None:
            if cf_streak < cf_abort_streak:
                return
            abort = UpstreamHTTPError(
                f"image_poll/{source}",
                403,
                (
                    "cloudflare_or_edge_html_block: image poll aborted after "
                    f"{cf_streak} consecutive CF edge blocks (source={source})."
                ),
            )
            setattr(abort, "conversation_id", conversation_id or "")
            setattr(abort, "cf_abort", True)
            raise abort

        def _cf_retry_sleep(status_code: int | None, error: str | None) -> bool:
            return _retry_sleep(
                "cf_edge",
                status_code,
                error,
                None,
                base_secs=float(config.image_poll_cf_retry_backoff_secs),
            )

        def _note_upstream_429(source: str) -> None:
            nonlocal upstream_429_streak
            upstream_429_streak += 1
            logger.warning({
                "event": "image_poll_rate_limited",
                "conversation_id": conversation_id,
                "attempt": budget.attempt,
                "source": source,
                "streak": upstream_429_streak,
                "abort_streak": rate_limit_abort_streak,
                "poll_budget": budget.snapshot(),
            })
            if upstream_429_streak >= rate_limit_abort_streak:
                rate_exc = ImagePollRateLimitedError(
                    "image poll aborted after "
                    f"{upstream_429_streak} consecutive upstream 429 responses "
                    f"(source={source}, conversation={conversation_id})"
                )
                setattr(rate_exc, "conversation_id", conversation_id or "")
                setattr(rate_exc, "status_code", 429)
                raise rate_exc

        while budget.begin_attempt():
            _raise_if_cancelled()
            # tasks 降为低频终态诊断；conversation 文档是主轮询源
            last_task_error = ""
            if not skip_tasks_on_cf and budget.should_query_tasks():
                try:
                    tasks = self._query_backend_tasks(conversation_id=conversation_id, timeout_secs=5.0)
                    budget.record_tasks_get()
                    for task in tasks:
                        is_error, error_msg, metadata = self.check_task_error(task)
                        if is_error and error_msg:
                            last_task_error = error_msg
                            logger.info({
                                "event": "image_poll_task_error_not_blocking",
                                "conversation_id": conversation_id,
                                "attempt": budget.attempt,
                                "error_msg": error_msg,
                                "metadata": metadata,
                            })
                except Exception as exc:
                    budget.record_tasks_get()
                    error_text = str(exc)
                    if (
                        not file_ids
                        and not sediment_ids
                        and (
                            "status=401" in error_text
                            or "token_revoked" in error_text.lower()
                            or "invalidated oauth token" in error_text.lower()
                        )
                    ):
                        token_error = InvalidAccessTokenError(
                            f"token invalidated during image poll task check ({conversation_id})"
                        )
                        setattr(token_error, "conversation_id", conversation_id or "")
                        raise token_error from exc
                    if self._is_cf_edge_block(exc):
                        _note_poll_cf("tasks", exc)
                        if _cf_retry_sleep(getattr(exc, "status_code", None), str(exc)):
                            if cf_streak >= cf_abort_streak:
                                _raise_poll_cf_abort("tasks")
                            continue
                        _raise_poll_cf_abort("tasks")
                        break
                    elif isinstance(exc, UpstreamHTTPError) and int(exc.status_code or 0) == 429:
                        _note_upstream_429("tasks")
                        if _retry_sleep("upstream_status", 429, str(exc), getattr(exc, "retry_after", None)):
                            continue
                    else:
                        logger.warning({
                            "event": "image_poll_task_check_failed",
                            "conversation_id": conversation_id,
                            "attempt": budget.attempt,
                            "error": error_text,
                            "status_code": getattr(exc, "status_code", None),
                        })

            _raise_if_cancelled()
            budget.record_conversation_get()
            try:
                conversation = self._get_conversation(conversation_id)
                cf_streak = 0
                upstream_429_streak = 0
            except UpstreamHTTPError as exc:
                if exc.status_code == 401:
                    token_error = InvalidAccessTokenError(
                        f"token invalidated during image poll ({conversation_id})"
                    )
                    setattr(token_error, "conversation_id", conversation_id or "")
                    raise token_error from exc
                if self._is_cf_edge_block(exc) or (
                    int(exc.status_code or 0) == 403
                    and "cloudflare_or_edge_html_block" in str(exc).lower()
                ):
                    _note_poll_cf("conversation", exc)
                    if _cf_retry_sleep(exc.status_code, str(exc)):
                        if cf_streak >= cf_abort_streak:
                            _raise_poll_cf_abort("conversation")
                        continue
                    _raise_poll_cf_abort("conversation")
                    break
                if exc.status_code == 429:
                    _note_upstream_429("conversation")
                    if _retry_sleep("upstream_status", exc.status_code, str(exc), exc.retry_after):
                        continue
                    break
                if exc.status_code in (500, 502, 503, 504):
                    if _retry_sleep("upstream_status", exc.status_code, None, exc.retry_after):
                        continue
                    break
                raise
            except requests.exceptions.RequestException as exc:
                if _retry_sleep("network", None, str(exc), None):
                    continue
                break
            except Exception as exc:
                if self._is_cf_edge_block(exc):
                    _note_poll_cf("conversation", exc)
                    if _cf_retry_sleep(None, str(exc)):
                        if cf_streak >= cf_abort_streak:
                            _raise_poll_cf_abort("conversation")
                        continue
                    _raise_poll_cf_abort("conversation")
                    break
                raise

            _raise_if_cancelled()
            for record in self._extract_image_tool_records(conversation):
                for file_id in record["file_ids"]:
                    if file_id not in file_ids:
                        file_ids.append(file_id)
                for sediment_id in record["sediment_ids"]:
                    if sediment_id not in sediment_ids:
                        sediment_ids.append(sediment_id)

            # 检查对话文本中的不可恢复终态（内容政策 / Instant 限额 / 缺参考图等）。
            # 当上游拒绝生成图片或等待用户补参时，错误消息会出现在 assistant 文本中，
            # 而非 /backend-api/tasks/ 的 task error 结构中。
            if not file_ids and not sediment_ids and not self._conversation_has_image_gen_activity(conversation):
                terminal_block = self._find_terminal_upstream_block_in_conversation(conversation)
                if terminal_block:
                    code, terminal_msg = terminal_block
                    logger.warning({
                        "event": "image_poll_conversation_terminal_block",
                        "conversation_id": conversation_id,
                        "attempt": budget.attempt,
                        "terminal_code": code,
                        "error_msg": terminal_msg[:200],
                        "poll_budget": budget.snapshot(),
                    })
                    _raise_terminal_upstream_block(code, terminal_msg)
                if last_task_error:
                    task_block = _classify_terminal_upstream_text(last_task_error)
                    if task_block:
                        code, terminal_msg = task_block
                        logger.warning({
                            "event": "image_poll_task_terminal_block",
                            "conversation_id": conversation_id,
                            "attempt": budget.attempt,
                            "terminal_code": code,
                            "error_msg": terminal_msg[:200],
                            "poll_budget": budget.snapshot(),
                        })
                        _raise_terminal_upstream_block(code, terminal_msg)

            logger.debug({
                "event": "image_poll_check",
                "conversation_id": conversation_id,
                "attempt": budget.attempt,
                "file_ids": file_ids,
                "sediment_ids": sediment_ids,
                "poll_budget": budget.snapshot(),
            })
            if file_ids or sediment_ids:
                if not check_before_hit:
                    # 先check再hit 机制关闭：直接返回首次发现的 file_ids
                    logger.info({"event": "image_poll_hit_no_settle", "conversation_id": conversation_id,
                                 "file_ids": file_ids, "sediment_ids": sediment_ids,
                                 "sediment_fast": sediment_fast})
                    return file_ids, sediment_ids
                hit_key = (tuple(file_ids), tuple(sediment_ids))
                if last_hit_key == hit_key:
                    logger.info({"event": "image_poll_hit", "conversation_id": conversation_id, "file_ids": file_ids,
                                 "sediment_ids": sediment_ids, "sediment_fast": sediment_fast})
                    return file_ids, sediment_ids
                last_hit_key = hit_key
                if not settle_enabled:
                    # 二次确认机制关闭：直接返回首次发现的 file_ids
                    logger.info({"event": "image_poll_hit_settle_disabled", "conversation_id": conversation_id,
                                 "file_ids": file_ids, "sediment_ids": sediment_ids,
                                 "sediment_fast": sediment_fast})
                    return file_ids, sediment_ids
                logger.info({"event": "image_poll_hit_pending_settle", "conversation_id": conversation_id,
                             "file_ids": file_ids, "sediment_ids": sediment_ids,
                             "settle_secs": settle_secs, "sediment_fast": sediment_fast})
                wait = min(settle_secs, max(0.0, _remaining()))
                if wait > 0:
                    _cancel_aware_sleep(wait)
                    continue
                return file_ids, sediment_ids
            logger.debug({
                "event": "image_poll_wait",
                "conversation_id": conversation_id,
                "poll_budget": budget.snapshot(),
            })
            wait = min(interval, max(0.0, _remaining()))
            if wait > 0:
                _cancel_aware_sleep(wait)
        logger.info({
            "event": "image_poll_timeout",
            "conversation_id": conversation_id,
            "timeout_secs": timeout_secs,
            "elapsed_secs": round(budget.elapsed_wall(), 2),
            "exhausted_reason": budget.effective_exhausted_reason(),
            "poll_mode": poll_mode,
            "timeout_config_key": poll_timeout_key,
            "attempts_made": budget.attempt,
            # attempts_made == 0 means the initial_wait consumed the entire budget — no HTTP attempted.
            "initial_wait_exhausted_budget": budget.attempt == 0,
            "poll_budget": budget.snapshot(),
            "last_task_error": last_task_error if last_task_error else None,
        })
        # Real elapsed + real reason + the key that actually governs this mode
        # (audit 28 §B6 / fix A4-4). The old text always claimed the wall timeout and
        # pointed at image_poll_timeout_secs, which the queue modes never read.
        exc = ImagePollTimeoutError(budget.exhaustion_message())
        if last_task_error:
            setattr(exc, "task_error", last_task_error)
        setattr(exc, "conversation_id", conversation_id or "")
        setattr(exc, "poll_budget", budget.snapshot())
        raise exc

    def _get_file_download_url(self, file_id: str) -> str:
        """获取文件下载地址。"""
        path = f"/backend-api/files/{file_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _get_attachment_download_url(self, conversation_id: str, attachment_id: str) -> str:
        """通过 conversation 附件接口获取下载地址。"""
        path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _query_backend_tasks(
        self,
        conversation_id: str = "",
        task_id: str = "",
        timeout_secs: float = 30.0,
    ) -> list[Dict[str, Any]]:
        """查询 /backend-api/tasks/ 接口获取异步任务状态和错误信息。

        参数：
        - `conversation_id`：可选。按 conversation_id 过滤任务。
        - `task_id`：可选。按 task_id 过滤任务。
        - `timeout_secs`：请求超时秒数。

        返回：
        - 任务列表，每个任务包含 image_gen_message 等字段。
        """
        path = "/backend-api/tasks"
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "application/json"}),
            timeout=timeout_secs,
        )
        ensure_ok(response, path)
        data = response.json()
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            return []

        # 按 conversation_id 或 task_id 过滤
        if conversation_id:
            tasks = [
                t for t in tasks
                if isinstance(t, dict) and (
                    t.get("conversation_id") == conversation_id
                    or t.get("original_conversation_id") == conversation_id
                )
            ]
        if task_id:
            tasks = [t for t in tasks if isinstance(t, dict) and t.get("task_id") == task_id]
        return tasks

    def check_task_error(self, task: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
        """检查单个任务是否包含结构化错误。

        通过以下字段判断（不依赖文本匹配）：
        - image_gen_message.metadata.is_error == True
        - image_gen_message.author.role == "assistant" (而非 "tool")
        - image_gen_message.content.content_type == "text" (而非 "multimodal_text")

        返回：
        - (is_error, error_msg, metadata)
        """
        img_msg = task.get("image_gen_message") or {}
        if not img_msg:
            return False, "", {}

        metadata = img_msg.get("metadata") or {}
        content = img_msg.get("content") or {}
        author = img_msg.get("author") or {}

        is_error = metadata.get("is_error", False)
        is_text_only = content.get("content_type") == "text"
        is_assistant_role = author.get("role") == "assistant"

        # 提取错误文本
        error_msg = ""
        if is_error and is_text_only:
            parts = content.get("parts", [])
            error_msg = "".join(p for p in parts if isinstance(p, str))

        return is_error, error_msg, metadata

    def _resolve_image_urls(self, conversation_id: str, file_ids: list[str], sediment_ids: list[str]) -> list[str]:
        """把图片结果 id 解析成可下载 URL。"""
        urls = []
        skip_patterns = {"file_upload"}
        invalid_token_error: Exception | None = None
        for file_id in file_ids:
            if file_id in skip_patterns:
                logger.debug({
                    "event": "image_file_id_skipped",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
                continue
            try:
                url = self._get_file_download_url(file_id)
            except Exception as exc:
                if _is_invalid_access_token_error(exc):
                    invalid_token_error = exc
                logger.debug({
                    "event": "image_download_url_failed",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                    "error": repr(exc),
                })
                continue
            if url:
                if url not in urls:
                    urls.append(url)
            else:
                logger.debug({
                    "event": "image_download_url_empty",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
        if not conversation_id or not sediment_ids:
            logger.debug({
                "event": "image_urls_resolved",
                "conversation_id": conversation_id,
                "file_ids": file_ids,
                "sediment_ids": sediment_ids,
                "urls": urls,
            })
            if invalid_token_error and not urls:
                token_error = InvalidAccessTokenError(
                    f"token invalidated while resolving image download url ({conversation_id})"
                )
                setattr(token_error, "conversation_id", conversation_id or "")
                raise token_error from invalid_token_error
            return urls
        for sediment_id in sediment_ids:
            try:
                url = self._get_attachment_download_url(conversation_id, sediment_id)
            except Exception as exc:
                if _is_invalid_access_token_error(exc):
                    invalid_token_error = exc
                logger.debug({
                    "event": "image_download_url_failed",
                    "source": "sediment",
                    "conversation_id": conversation_id,
                    "id": sediment_id,
                    "error": repr(exc),
                })
                continue
            if url:
                if url not in urls:
                    urls.append(url)
            else:
                logger.debug({
                    "event": "image_download_url_empty",
                    "source": "sediment",
                    "conversation_id": conversation_id,
                    "id": sediment_id,
                })
        logger.debug({
            "event": "image_urls_resolved",
            "conversation_id": conversation_id,
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "urls": urls,
        })
        if invalid_token_error and not urls:
            token_error = InvalidAccessTokenError(
                f"token invalidated while resolving image download url ({conversation_id})"
            )
            setattr(token_error, "conversation_id", conversation_id or "")
            raise token_error from invalid_token_error
        return urls

    def resolve_conversation_image_urls(
            self,
            conversation_id: str,
            file_ids: list[str],
            sediment_ids: list[str],
            poll: bool = True,
            poll_timeout_secs: float | None = None,
            *,
            sse_image_gen_ms: float | None = None,
    ) -> list[str]:
        file_ids = [item for item in file_ids if item != "file_upload"]
        sediment_ids = list(sediment_ids)
        timeout = poll_timeout_secs if poll_timeout_secs is not None else config.image_poll_timeout_secs
        if sse_image_gen_ms is None:
            sse_image_gen_ms = getattr(self, "_last_image_sse_gen_ms", None)
        if (
            poll
            and conversation_id
            and sediment_ids
            and not file_ids
            and config.image_sediment_fast_poll_enabled
        ):
            settle = float(config.image_settle_secs_sediment)
            if settle > 0:
                time.sleep(min(settle, 2.0))
            try:
                urls = self._resolve_image_urls(conversation_id, file_ids, sediment_ids)
            except Exception:
                urls = []
            if urls:
                logger.info({
                    "event": "image_resolve_sediment_fast_direct",
                    "conversation_id": conversation_id,
                    "sediment_ids": sediment_ids,
                    "url_count": len(urls),
                })
                return urls
        # 当 check-before-hit 和 settle 均已关闭，且 SSE 已给出 file_ids 时，
        # 跳过轮询直接解析 URL，省去 initial_wait + 轮询耗时。
        if poll and conversation_id and (file_ids or sediment_ids):
            if not config.image_check_before_hit_enabled and not config.image_settle_enabled:
                logger.info({
                    "event": "image_resolve_skip_poll_direct_resolve",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                })
                return self._resolve_image_urls(conversation_id, file_ids, sediment_ids)
        if poll and conversation_id:
            logger.info({
                "event": "image_resolve_poll_needed",
                "conversation_id": conversation_id,
                "initial_file_ids": file_ids,
                "initial_sediment_ids": sediment_ids,
                "poll_timeout_secs": timeout,
            })
            try:
                polled_file_ids, polled_sediment_ids = self._poll_image_results(
                    conversation_id,
                    timeout,
                    file_ids,
                    sediment_ids,
                    sse_image_gen_ms=sse_image_gen_ms,
                )
            except ImagePollTimeoutError as exc:
                # 如果轮询超时且有 task error（如 moderation 拦截），抛出明确终态错误
                # 而非 ImagePollTimeoutError，让调用方能区分真正的超时和上游拒绝
                task_error = getattr(exc, "task_error", "")
                if not file_ids and not sediment_ids:
                    if task_error:
                        task_block = _classify_terminal_upstream_text(task_error)
                        if task_block:
                            code, terminal_msg = task_block
                            _raise_terminal_upstream_block(code, terminal_msg)
                        raise ImageContentPolicyError(task_error) from exc
                    raise
                logger.warning({
                    "event": "image_resolve_poll_partial_timeout",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                })
            except Exception as exc:
                # CF edge block is a stop signal, not a recoverable partial poll.
                # Continuing into attachment/file URL resolution immediately sends
                # another request through the same blocked egress and increases the
                # chance of extending the challenge window.
                if self._is_cf_edge_block(exc) or bool(getattr(exc, "cf_abort", False)):
                    raise
                if not file_ids and not sediment_ids:
                    raise
                logger.warning({
                    "event": "image_resolve_poll_partial_error",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                    "error": repr(exc),
                })
            else:
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
        return self._resolve_image_urls(conversation_id, file_ids, sediment_ids)

    def download_image_bytes(self, urls: list[str]) -> list[bytes]:
        """下载生图结果。estuary/chatgpt.com 必须带 Bearer（6aac185 前挂在 session 头上）。

        勿走无鉴权 resource session；S3/upload 仍用 `_resource_headers`。
        """
        images = []
        for url in urls:
            last_exc: BaseException | None = None
            for attempt in range(1, 4):
                try:
                    headers = {
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    }
                    if self.access_token:
                        headers["Authorization"] = f"Bearer {self.access_token}"
                    response = self.session.get(url, headers=headers, timeout=120)
                    ensure_ok(response, "image_download")
                    content = response.content
                    if content:
                        try:
                            from services.bandwidth_tracker import bandwidth_tracker

                            bandwidth_tracker.record_bytes(len(content))
                        except Exception:
                            pass
                    if content not in images:
                        images.append(content)
                    last_exc = None
                    break
                except UpstreamHTTPError as exc:
                    last_exc = exc
                    # file_id 刚出现时 estuary 偶发 404/403；短退避后重试
                    if int(getattr(exc, "status_code", 0) or 0) in {404, 403} and attempt < 3:
                        time.sleep(1.5 * attempt)
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
        return images

    def stream_conversation(
            self,
            messages: Optional[list[Dict[str, Any]]] = None,
            model: str = "auto",
            prompt: str = "",
            images: Optional[list[str]] = None,
            system_hints: Optional[list[str]] = None,
            thinking_effort: str = "",
            *,
            history_and_training_disabled: bool | None = None,
            conversation_id: str = "",
            parent_message_id: str = "",
    ) -> Iterator[str]:
        system_hints = system_hints or []
        if "picture_v2" in system_hints:
            yield from self._stream_picture_conversation(prompt, model, images or [])
            return

        normalized = messages or [{"role": "user", "content": prompt}]
        from services.request_phase import RequestPhaseTracker

        phase = RequestPhaseTracker(
            account_token=self.access_token or "",
            node_proxy=str((self.account or {}).get("proxy") or ""),
            purpose="text",
        )
        try:
            logger.info(phase.mark("preflight"))
            self._ensure_bootstrap()
            logger.info(phase.mark("auth"))
            requirements = self._get_chat_requirements()
            path, timezone = self._chat_target()
            account = self.account if isinstance(self.account, dict) else {}
            reuse = self._text_chat_reuse_conversation() and self._text_chat_persist_history()
            cid = str(conversation_id or "").strip()
            parent = str(parent_message_id or "").strip()
            if reuse and not cid:
                cid = str(account.get("text_conversation_id") or "").strip()
            if reuse and not parent:
                parent = str(account.get("text_parent_message_id") or "").strip()
            # Never continue an image conversation in the text path.
            image_cid = str(account.get("last_image_conversation_id") or "").strip()
            if cid and image_cid and cid == image_cid:
                cid = ""
                parent = ""
            logger.info(phase.mark("request_build"))
            payload = self._conversation_payload(
                normalized,
                model,
                timezone,
                thinking_effort=thinking_effort,
                history_and_training_disabled=history_and_training_disabled,
                conversation_id=cid,
                parent_message_id=parent,
            )
            # Extract plain prompt for prepare partial_query.
            prepare_prompt = str(prompt or "").strip()
            if not prepare_prompt:
                for item in reversed(normalized):
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content")
                    if isinstance(content, str) and content.strip():
                        prepare_prompt = content.strip()
                        break
            if self.access_token and path.startswith("/backend-api/f/conversation"):
                try:
                    self._prepare_text_conversation(
                        prepare_prompt or " ",
                        str(payload.get("model") or model or "auto"),
                        parent_message_id=str(payload.get("parent_message_id") or ""),
                        conversation_id=str(payload.get("conversation_id") or ""),
                    )
                except Exception as prep_exc:
                    logger.warning(
                        {
                            "event": "text_prepare_failed",
                            "error": str(prep_exc)[:240],
                        }
                    )
                    # Fall through: some environments still accept bare /f/conversation.
            try:
                from services.request_shape import body_shape

                logger.info(
                    {
                        "event": "request_shape",
                        "purpose": "conversation_body",
                        "path": path,
                        "timezone": timezone,
                        "history_disabled": bool(payload.get("history_and_training_disabled")),
                        "has_conversation_id": bool(payload.get("conversation_id")),
                        **body_shape(payload),
                    }
                )
            except Exception:
                pass
            logger.info(phase.mark("upstream_submit"))
            response = self._post_conversation_with_cf_retry(
                path,
                requirements,
                payload,
                timeout=config.image_pre_conversation_timeout_secs if "picture_v2" in system_hints else 300,
            )
            logger.info(phase.mark("sse_ready"))
            try:
                yield from iter_sse_payloads(response)
            finally:
                response.close()
                logger.info(phase.mark("cleanup"))
        except Exception as exc:
            logger.warning(phase.fail(error_type=type(exc).__name__))
            raise

    def _report_progress(self, step: str) -> None:
        """Report progress step to the callback if set."""
        now = time.time()
        mapped = {
            "uploading": "download",
            "bootstrapping": "node_connect",
            "getting_token": "auth",
            "preparing_conversation": "request_build",
            "starting_generation": "upstream_submit",
            "generating": "sse_ready",
            "polling": "poll",
            "downloading": "download",
            "done": "cleanup",
        }.get(str(step or ""), str(step or ""))
        try:
            tracker = getattr(self, "_phase_tracker", None)
            if tracker is not None:
                logger.info(tracker.mark(mapped))
            else:
                logger.info({
                    "event": "image_upstream_phase",
                    "phase": str(step or ""),
                    "elapsed_ms": int((now - self._progress_started_at) * 1000),
                    "since_last_ms": int((now - self._progress_last_at) * 1000),
                })
        except Exception:
            pass
        self._progress_last_at = now
        if self.progress_callback:
            try:
                self.progress_callback(step)
            except Exception:
                pass

    def _stream_picture_conversation(
            self,
            prompt: str,
            model: str,
            images: list[str],
    ) -> Iterator[str]:
        if not self.access_token:
            raise RuntimeError("access_token is required for image endpoints")
        from services.request_phase import RequestPhaseTracker

        self._phase_tracker = RequestPhaseTracker(
            account_token=self.access_token or "",
            node_proxy=str((self.account or {}).get("proxy") or ""),
            purpose="image",
        )
        try:
            self._report_progress("uploading")
            references = [self._upload_image(image, f"image_{idx}.png") for idx, image in enumerate(images, start=1)]
            response = self._open_image_sse_with_cf_retry(prompt, model, references)
            self._report_progress("generating")
            captured_conversation_id = ""
            sse_started_at = time.time()
            self._last_image_sse_gen_ms = None

            def conversation_ready(payload: str) -> bool:
                return bool(re.search(r'"conversation_id"\s*:\s*"[^"]+"', payload))

            def image_assets_ready(payload: str) -> bool:
                # Wait for concrete asset pointers before leaving SSE; tool_invoked alone is too early.
                if FILE_SERVICE_ID_RE.search(payload) or REAL_IMAGE_FILE_ID_RE.search(payload):
                    return True
                if SEDIMENT_ID_RE.search(payload):
                    return True
                return False

            try:
                # 仅对「拿到 conversation_id 之前」设墙钟；拿到后继续读流。
                # post_ready 是安全阀：上游 SSE 不结束时转入 poll，避免无限挂死。
                # 禁止再回到 post_ready=15s（会掐死免费号 tool 结果）。
                # complete_predicate：出现 file_id 立即转 poll，避免等 EOF（曾挂 ~90s）。
                post_ready = None
                try:
                    post_ready = config.image_sse_post_ready_timeout_secs
                except Exception:
                    post_ready = 75.0
                for payload in iter_sse_payloads_until_first_payload(
                    response,
                    config.image_pre_conversation_timeout_secs,
                    ready_predicate=conversation_ready,
                    cancel_event=self.cancel_event,
                    post_ready_timeout_secs=post_ready,
                    complete_predicate=image_assets_ready,
                ):
                    if self._last_image_sse_gen_ms is None:
                        low = payload.lower()
                        if (
                            SEDIMENT_ID_RE.search(payload)
                            or FILE_SERVICE_ID_RE.search(payload)
                            or REAL_IMAGE_FILE_ID_RE.search(payload)
                            or "image_gen" in low
                        ):
                            self._last_image_sse_gen_ms = (time.time() - sse_started_at) * 1000.0
                    if not captured_conversation_id:
                        match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
                        if match:
                            captured_conversation_id = match.group(1)
                            if self._phase_tracker is not None:
                                self._phase_tracker.conversation_id = captured_conversation_id
                                logger.info(self._phase_tracker.mark("conversation_started"))
                            logger.info({
                                "event": "image_sse_conversation_id_captured",
                                "conversation_id": captured_conversation_id,
                            })
                            if self.progress_callback:
                                try:
                                    self.progress_callback({
                                        "step": "conversation_id_captured",
                                        "conversation_id": captured_conversation_id,
                                        "access_token": self.access_token,
                                    })
                                except Exception:
                                    pass
                    yield payload
            finally:
                # If we already have conversation_id, defer close so soft post_ready /
                # complete_predicate handoff can poll while upstream may still finish.
                if captured_conversation_id:
                    def _deferred_close(resp: Any = response) -> None:
                        try:
                            time.sleep(2.0)
                        except Exception:
                            pass
                        try:
                            resp.close()
                        except Exception:
                            pass

                    threading.Thread(
                        target=_deferred_close,
                        name="image-sse-deferred-close",
                        daemon=True,
                    ).start()
                    logger.info({
                        "event": "image_sse_deferred_close",
                        "conversation_id": captured_conversation_id,
                    })
                else:
                    response.close()
                logger.info(self._phase_tracker.mark("cleanup"))
        except Exception as exc:
            logger.warning(self._phase_tracker.fail(error_type=type(exc).__name__))
            raise

    @staticmethod
    def _is_cf_edge_block(exc: BaseException) -> bool:
        """判定是否为 CF/边缘瞬时拦截（可重试）。"""
        if isinstance(exc, UpstreamHTTPError):
            return OpenAIBackendAPI._looks_like_cf_edge_response(int(exc.status_code), str(exc.body or ""))
        msg = str(exc).lower()
        return "cf_edge_block" in msg or "cloudflare_or_edge_html_block" in msg

    @staticmethod
    def _is_transient_transport_error(exc: BaseException) -> bool:
        """Retry TLS/socket failures without treating CF responses as transport noise."""
        if OpenAIBackendAPI._is_cf_edge_block(exc):
            return False
        text = str(exc or "").lower()
        return any(
            marker in text
            for marker in (
                "curl: (35)",
                "curl: (56)",
                "tls connect error",
                "recv failure",
                "connection was reset",
                "connection reset by peer",
                "failed to connect",
                "could not connect",
                "openssl_internal",
            )
        )

    def _rebuild_api_session_after_transport_error(self) -> None:
        """Recreate curl state after a TLS/socket failure without changing egress/TLS identity."""
        old_session = self.session
        cookies: dict[str, str] = {}
        try:
            cookies = dict(old_session.cookies.get_dict())
        except Exception:
            pass

        # A transport retry must stay on the same proxy and curl fingerprint.  In
        # particular, local Clash may require ``verify=False``; silently changing
        # it back to True turns a recoverable socket reset into repeated curl(35).
        session_kwargs = proxy_settings.build_session_kwargs(
            account=self.account,
            impersonate=str(getattr(old_session, "impersonate", "") or self.fp["impersonate"]),
            verify=getattr(old_session, "verify", True),
            upstream=True,
        )
        old_proxies = getattr(old_session, "proxies", None)
        if isinstance(old_proxies, dict) and old_proxies:
            session_kwargs.pop("proxy", None)
            session_kwargs["proxies"] = dict(old_proxies)
        old_timeout = getattr(old_session, "timeout", None)
        if old_timeout is not None:
            session_kwargs["timeout"] = old_timeout

        new_session = requests.Session(**session_kwargs)
        new_session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": self._accept_language(),
        })
        if cookies:
            try:
                new_session.cookies.update(cookies)
            except Exception:
                pass
        self.session = new_session
        try:
            old_session.close()
        except Exception:
            pass

    def _post_conversation_with_cf_retry(
        self,
        path: str,
        requirements: ChatRequirements,
        payload: Dict[str, Any],
        *,
        timeout: float,
        max_attempts: int = 3,
    ) -> Any:
        """POST /conversation；空 body/HTML 403 短暂重试并重建 requirements。"""
        last_exc: BaseException | None = None
        req = requirements
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(
                    self.base_url + path,
                    headers=self._conversation_headers(path, req),
                    json=payload,
                    timeout=timeout,
                    stream=True,
                )
                ensure_ok(response, path)
                return response
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_cf_edge_block(exc):
                    raise
                logger.warning(
                    {
                        "event": "conversation_cf_edge_retry",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "path": path,
                        "status_code": getattr(exc, "status_code", None),
                        "error": str(exc)[:240],
                    }
                )
                try:
                    if "response" in locals() and response is not None:
                        response.close()
                except Exception:
                    pass
                time.sleep(0.4 * attempt + random.uniform(0, 0.25))
                try:
                    self._ensure_bootstrap()
                    req = self._get_chat_requirements()
                except Exception as refresh_exc:
                    logger.warning(
                        {
                            "event": "conversation_cf_edge_refresh_failed",
                            "error": str(refresh_exc)[:240],
                        }
                    )
        assert last_exc is not None
        raise last_exc
    def _open_image_sse_with_cf_retry(
        self,
        prompt: str,
        model: str,
        references: list[Dict[str, Any]],
        *,
        max_attempts: int = 3,
    ) -> requests.Response:
        """bootstrap→requirements→prepare→start；仅重试 TLS/socket 瞬断。

        CF/边缘 403 仍立即抛出，由上层换号 failover，避免同号连打放大风控。
        """
        last_exc: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                self._report_progress("bootstrapping")
                # Image path: CF on homepage must fail-fast (no soft PoW fallback),
                # otherwise streams can idle until hard timeout (~300s).
                self._ensure_bootstrap(soft_fail=False)
                self._report_progress("getting_token")
                # Image CF/edge 403 is intentionally single-shot.  The generic
                # text helper retries CF responses internally, which would triple
                # pressure on the same account/egress before failover.  TLS/socket
                # errors still bubble to this outer loop and rebuild the session.
                requirements = self._get_chat_requirements_once()
                self._report_progress("preparing_conversation")
                conduit_token = self._prepare_image_conversation(prompt, requirements, model)
                self._report_progress("starting_generation")
                return self._start_image_generation(prompt, requirements, conduit_token, model, references)
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_transient_transport_error(exc):
                    raise
                logger.warning({
                    "event": "image_transport_retry",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "error": str(exc)[:240],
                })
                self._rebuild_api_session_after_transport_error()
                time.sleep(0.35 * attempt + random.uniform(0, 0.15))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _should_soft_fail_bootstrap(exc: BaseException) -> bool:
        """首页 HTML 被 CF/边缘拦时允许回退默认 PoW，避免整条生图/聊天硬失败。"""
        if isinstance(exc, UpstreamHTTPError):
            if int(exc.status_code) in {403, 429, 502, 503, 520, 521, 522, 523, 524}:
                return True
            body = str(exc.body or "").lower()
            if "<html" in body and (
                "cloudflare" in body
                or "cf-error" in body
                or "scale-appear" in body
                or "just a moment" in body
            ):
                return True
            return False
        # 传输层失败（超时/断连）同样回退，sentinel 仍可走默认脚本。
        transport_types: tuple[type, ...] = (TimeoutError, OSError, ConnectionError)
        try:
            from requests.exceptions import ConnectionError as ReqConnectionError
            from requests.exceptions import Timeout as ReqTimeout

            transport_types = (*transport_types, ReqTimeout, ReqConnectionError)
        except Exception:  # pragma: no cover
            pass
        return isinstance(exc, transport_types)

    def _apply_default_pow_scripts(self, *, reason: str, detail: str = "") -> None:
        self.pow_script_sources = [DEFAULT_POW_SCRIPT]
        if not self.pow_data_build:
            self.pow_data_build = ""
        logger.warning({
            "event": "bootstrap_soft_failed",
            "reason": reason,
            "detail": (detail or "")[:240],
            "fallback": DEFAULT_POW_SCRIPT,
        })

    def _bootstrap(self, *, soft_fail: bool = True) -> None:
        """预热首页并提取 PoW 脚本；CF/边缘拦 HTML 时默认软失败回退默认脚本。"""
        try:
            response = self.session.get(
                self.base_url + "/",
                headers=self._bootstrap_headers(),
                timeout=30,
            )
            ensure_ok(response, "bootstrap")
            self.pow_script_sources, self.pow_data_build = parse_pow_resources(response.text)
            if not self.pow_script_sources:
                self.pow_script_sources = [DEFAULT_POW_SCRIPT]
            self._remember_bootstrap_cache()
            return
        except Exception as exc:
            if not soft_fail or not self._should_soft_fail_bootstrap(exc):
                raise
            try:
                proxy_settings.invalidate_clearance(
                    target_url=self.base_url + "/",
                    account=self.account,
                    upstream=True,
                )
            except Exception:
                pass
            status = getattr(exc, "status_code", None)
            self._apply_default_pow_scripts(
                reason=f"upstream_{status}" if status is not None else type(exc).__name__,
                detail=str(exc),
            )
            self._remember_bootstrap_cache()

    def _bootstrap_cache_key(self) -> str:
        raw = str(self.access_token or self.device_id or "anon").encode("utf-8", "ignore")
        return hashlib.sha256(raw).hexdigest()[:24]

    def _remember_bootstrap_cache(self) -> None:
        self._bootstrap_at = time.time()
        if not self.pow_script_sources:
            return
        _SEARCH_BOOTSTRAP_CACHE[self._bootstrap_cache_key()] = (
            self._bootstrap_at,
            list(self.pow_script_sources),
            str(self.pow_data_build or ""),
        )

    def _ensure_bootstrap(
        self,
        max_age_secs: float = _SEARCH_BOOTSTRAP_TTL_SECS,
        *,
        soft_fail: bool = True,
    ) -> None:
        """Skip homepage fetch when PoW scripts were warmed recently for this token."""
        now = time.time()
        if self.pow_script_sources and self._bootstrap_at and (now - self._bootstrap_at) < max_age_secs:
            return
        key = self._bootstrap_cache_key()
        cached = _SEARCH_BOOTSTRAP_CACHE.get(key)
        if cached and (now - cached[0]) < max_age_secs and cached[1]:
            self.pow_script_sources = list(cached[1])
            self.pow_data_build = str(cached[2] or "")
            self._bootstrap_at = cached[0]
            return
        self._bootstrap(soft_fail=soft_fail)

    def _get_chat_requirements(self) -> ChatRequirements:
        """获取当前模式对话所需的 sentinel token（prepare + finalize 两步流程）。

        CF/边缘 403 时最多重试 2 次（短退避），避免对话 UI 长时间空挂。
        """
        try:
            from services.config import config as _cfg

            settings = _cfg.get_image_pipeline_settings()
            if bool(settings.get("pre_ticket_pool_enabled")) and self.access_token:
                from services.image_pipeline.pre_ticket_pool import pre_ticket_pool

                cached = pre_ticket_pool.get(self.access_token)
                if cached is not None and cached.requirements is not None:
                    return cached.requirements
        except Exception:
            pass

        last_exc: BaseException | None = None
        for attempt in range(1, 4):
            try:
                requirements = self._get_chat_requirements_once()
                try:
                    from services.config import config as _cfg

                    settings = _cfg.get_image_pipeline_settings()
                    if bool(settings.get("pre_ticket_pool_enabled")) and self.access_token:
                        from services.image_pipeline.pre_ticket_pool import PreTicketBundle, pre_ticket_pool

                        pre_ticket_pool.put(
                            self.access_token,
                            PreTicketBundle(requirements=requirements, turnstile_solved=bool(requirements.turnstile_token)),
                        )
                except Exception:
                    pass
                return requirements
            except UpstreamHTTPError as exc:
                last_exc = exc
                status = int(getattr(exc, "status_code", 0) or 0)
                body = str(getattr(exc, "body", "") or "")
                retryable = status in {403, 429, 502, 503, 520, 521, 522, 523, 524} or self._looks_like_cf_edge_response(
                    status, body
                )
                if not retryable or attempt >= 3:
                    raise
                time.sleep(0.35 * attempt + random.uniform(0, 0.2))
            except RuntimeError as exc:
                last_exc = exc
                msg = str(exc).lower()
                retryable = "cf_edge_block" in msg or "cloudflare" in msg or "403" in msg
                if not retryable or attempt >= 3:
                    raise
                time.sleep(0.35 * attempt + random.uniform(0, 0.2))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("chat_requirements failed after retries")

    def _get_chat_requirements_once(self) -> ChatRequirements:
        base = "/backend-api/sentinel/chat-requirements" if self.access_token else "/backend-anon/sentinel/chat-requirements"
        p_token = build_legacy_requirements_token(self.user_agent, self.pow_script_sources, self.pow_data_build)

        prepare_path = base + "/prepare"
        response = self.session.post(
            self.base_url + prepare_path,
            headers=self._headers(prepare_path, {"Content-Type": "application/json"}),
            json={"p": p_token},
            timeout=30,
        )
        ensure_ok(response, "chat_requirements_prepare")
        prepare_data = response.json()

        if (prepare_data.get("arkose") or {}).get("required"):
            raise RuntimeError("chat requirements requires arkose token, which is not implemented")

        proof_token = ""
        proof_info = prepare_data.get("proofofwork") or {}
        if proof_info.get("required"):
            proof_token = build_proof_token(
                proof_info.get("seed", ""),
                proof_info.get("difficulty", ""),
                self.user_agent,
                script_sources=self.pow_script_sources,
                data_build=self.pow_data_build,
            )

        turnstile_token = ""
        turnstile_info = prepare_data.get("turnstile") or {}
        turnstile_required = bool(turnstile_info.get("required"))
        if turnstile_required and turnstile_info.get("dx"):
            turnstile_token = solve_turnstile_token(turnstile_info["dx"], p_token) or ""
        if turnstile_required and not turnstile_token:
            raise RuntimeError(
                "chat_requirements_turnstile_required_but_unsolved: "
                "prepare demanded turnstile but local VM returned empty token"
            )

        # SPA HAR 2026-07-21: finalize body keys are proofofwork/turnstile (not proof_token/turnstile_token).
        finalize_path = base + "/finalize"
        finalize_body = {
            "prepare_token": prepare_data.get("prepare_token", ""),
            "proofofwork": proof_token,
            "turnstile": turnstile_token,
        }
        logger.info(
            {
                "event": "chat_requirements_finalize_shape",
                "turnstile_required": turnstile_required,
                "turnstile_solved_len": len(turnstile_token),
                "proof_solved_len": len(proof_token),
                "finalize_keys": sorted(finalize_body.keys()),
            }
        )
        response = self.session.post(
            self.base_url + finalize_path,
            headers=self._headers(finalize_path, {"Content-Type": "application/json"}),
            json=finalize_body,
            timeout=30,
        )
        ensure_ok(response, "chat_requirements_finalize")
        data = response.json()

        token = data.get("token", "")
        if not token:
            message = "missing auth chat requirements token" if self.access_token else "missing chat requirements token"
            raise RuntimeError(f"{message}: {data}")

        return ChatRequirements(
            token=token,
            proof_token=proof_token,
            turnstile_token=turnstile_token,
            so_token=data.get("so_token", ""),
            raw_finalize=data,
        )

    def _chat_target(self) -> tuple[str, str]:
        # SPA HAR 2026-07-21: authenticated text uses /f/conversation (+ prepare).
        if self.access_token:
            return "/backend-api/f/conversation", self._chat_timezone()
        return "/backend-anon/conversation", "America/Los_Angeles"

    def _prepare_text_conversation(
        self,
        prompt: str,
        model: str,
        *,
        parent_message_id: str = "",
        conversation_id: str = "",
    ) -> None:
        """SPA text path: POST /f/conversation/prepare (conduit optional; SPA often omits X-Conduit-Token)."""
        from services.image_pipeline.chat_prepare_cache import chat_prepare_cache
        from services.protocol.chatgpt_web_request import build_text_prepare_body, timezone_offset_min

        if self.access_token and chat_prepare_cache.get(
            self.access_token,
            conversation_id=conversation_id,
            model=model,
        ):
            return

        path = "/backend-api/f/conversation/prepare"
        tz = self._chat_timezone()
        account = self.account if isinstance(self.account, dict) else {}
        seed = str(account.get("email") or account.get("id") or prompt)[:64]
        payload = build_text_prepare_body(
            prompt,
            model or "auto",
            timezone=tz,
            timezone_offset=timezone_offset_min(tz),
            parent_message_id=parent_message_id,
            conversation_id=conversation_id,
            contextual_seed=seed,
        )
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "*/*"}),
            json=payload,
            timeout=60,
        )
        ensure_ok(response, path)
        # SPA returns conduit_token but does not require X-Conduit-Token on text SSE.
        try:
            data = response.json() if response.text else {}
            logger.info(
                {
                    "event": "text_prepare_ok",
                    "has_conduit": bool((data or {}).get("conduit_token")),
                    "status": (data or {}).get("status"),
                }
            )
        except Exception:
            pass
        if self.access_token:
            chat_prepare_cache.put(
                self.access_token,
                conversation_id=conversation_id,
                model=model,
            )

    def delete_text_conversation(self, conversation_id: str) -> bool:
        cid = str(conversation_id or "").strip()
        if not cid or not self.access_token:
            return False
        path = f"/backend-api/conversation/{cid}"
        try:
            response = self.session.patch(
                self.base_url + path,
                headers=self._editable_conversation_document_headers(path, cid),
                json={"is_visible": False},
                timeout=30,
            )
            if int(getattr(response, "status_code", 0) or 0) in {200, 204}:
                return True
        except Exception:
            pass
        try:
            response = self.session.delete(
                self.base_url + path,
                headers=self._editable_conversation_document_headers(path, cid),
                timeout=30,
            )
            return int(getattr(response, "status_code", 0) or 0) in {200, 204, 404}
        except Exception:
            return False


    def list_models(self) -> Dict[str, Any]:
        """返回当前模式下可用模型，格式对齐 OpenAI `/v1/models`。"""
        self._bootstrap()
        path = "/backend-api/models?history_and_training_disabled=false" if self.access_token else (
            "/backend-anon/models?iim=false&is_gizmo=false"
        )
        route = "/backend-api/models" if self.access_token else "/backend-anon/models"
        context = "auth_models" if self.access_token else "anon_models"
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(route),
            timeout=30,
        )
        ensure_ok(response, context)
        data = []
        seen = set()
        for item in response.json().get("models", []):
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug", "")).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            data.append({
                "id": slug,
                "object": "model",
                "created": int(item.get("created") or 0),
                "owned_by": str(item.get("owned_by") or "chatgpt"),
                "permission": [],
                "root": slug,
                "parent": None,
            })
        data.sort(key=lambda item: item["id"])
        return {"object": "list", "data": data}
