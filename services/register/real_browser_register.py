from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import socket
import socketserver
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote, urlencode, urlparse, urlsplit, urlunsplit


OUTLOOK_DOMAINS = ("@outlook.com", "@hotmail.com", "@live.com")
OUTLOOK_CLOCK_TOLERANCE_SEC = 45
OUTLOOK_IN_USE_STALE_SECONDS = 3600
HOST_REGISTER_PROXY = "http://127.0.0.1:40080"
CONTAINER_REGISTER_PROXY = "http://privoxy:8118"
LOCAL_PROXY_CONTAINERS = (
    "chatgpt2api-flaresolverr",
    "chatgpt2api-privoxy",
    "chatgpt2api-warp-proxy",
)


def normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def mask_email(value: object) -> str:
    email = normalize_email(value)
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:2]}***{local[-1:]}@{domain}"


def is_registration_transition_path(value: object) -> bool:
    path = urlparse(str(value or "")).path
    return any(
        marker in path
        for marker in (
            "create-account/password",
            "email-verification",
            "about-you",
            "/log-in/",
            "/api/auth/callback/openai",
        )
    )


def is_signup_password_input(input_type: object, name: object) -> bool:
    return str(input_type or "").strip().lower() == "password" or str(name or "").strip().lower() == "new-password"


def generate_openai_account_password(length: int = 20) -> str:
    size = max(18, int(length or 20))
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*_-+="),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    chars = required + [secrets.choice(alphabet) for _ in range(size - len(required))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def birthday_segment_value(label: object, data_type: object, index: int) -> str:
    signature = f"{label or ''} {data_type or ''}".strip().lower()
    if "month" in signature:
        return "01"
    if "day" in signature:
        return "01"
    if "year" in signature:
        return "1990"
    return ("01", "01", "1990")[max(0, min(2, int(index)))]


def is_retryable_about_you_error(visible_text: object, button_texts: list[object] | tuple[object, ...]) -> bool:
    text = " ".join(str(visible_text or "").strip().lower().split())
    has_timeout = "operation timed out" in text or "oops, an error occurred" in text
    has_retry = any("try again" in str(item or "").strip().lower() for item in button_texts)
    return has_timeout and has_retry


def about_you_submit_method(attempt: int) -> str:
    return ("native", "javascript", "enter")[max(0, min(2, int(attempt) - 1))]


def is_transient_registration_error(error: object) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    markers = (
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (56)",
        "connect tunnel failed",
        "connection reset",
        "connection refused",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "_http_429",
        "_http_500",
        "_http_502",
        "_http_503",
        "_http_504",
    )
    return any(marker in text for marker in markers)


def should_reconnect_registration_warp(error: object) -> bool:
    text = str(error or "").strip().lower()
    markers = (
        "connect tunnel failed",
        "proxyerror",
        "curl: (56)",
        "response 503",
        "connection reset by peer",
    )
    return any(marker in text for marker in markers)


def should_retry_browser_session(error: object) -> bool:
    text = str(error or "").strip().lower()
    return (
        "staleelementreferenceexception@/about-you" in text
        or "staleelementreferenceexception@/auth/error" in text
        or "about_you_submit_stuck@/about-you" in text
    )


def is_registered_unusable_login_error(error: object) -> bool:
    text = str(error or "").strip().lower()
    return "account_deactivated" in text or "deleted or deactivated" in text


def summarize_registration_network_events(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a secret-free trace of registration requests relevant to diagnosis."""

    result: list[dict[str, Any]] = []
    request_urls: dict[str, str] = {}
    interesting = ("/api/accounts/", "/email-otp/", "/create_account", "/create-account")
    for entry in entries:
        try:
            envelope = json.loads(str(entry.get("message") or "{}"))
            message = envelope.get("message") if isinstance(envelope, dict) else None
            if not isinstance(message, dict):
                continue
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method == "Network.requestWillBeSent":
                request = params.get("request") if isinstance(params.get("request"), dict) else {}
                url = str(request.get("url") or "")
                request_id = str(params.get("requestId") or "")
                if request_id and url:
                    request_urls[request_id] = url
                event = "request"
                status = None
            elif method == "Network.responseReceived":
                response = params.get("response") if isinstance(params.get("response"), dict) else {}
                url = str(response.get("url") or "")
                request_id = str(params.get("requestId") or "")
                if request_id and url:
                    request_urls[request_id] = url
                event = "response"
                status = response.get("status")
            elif method == "Network.loadingFailed":
                request_id = str(params.get("requestId") or "")
                url = str(params.get("url") or request_urls.get(request_id) or "")
                event = "failed"
                status = None
            else:
                continue
            path = urlparse(url).path
            if not path or not any(marker in path for marker in interesting):
                continue
            item: dict[str, Any] = {"event": event, "path": path}
            if status is not None:
                item["status"] = int(status)
            if event == "failed":
                error_text = str(params.get("errorText") or "").strip()
                if error_text:
                    item["error"] = error_text[:120]
            result.append(item)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return result[-20:]


def build_attached_chromium_args(*, profile: str, proxy_url: str, debug_port: int) -> list[str]:
    """Launch Chromium as a normal process, then attach ChromeDriver over CDP."""

    return [
        "/usr/bin/chromium",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
        "--no-first-run",
        "--no-default-browser-check",
        "--incognito",
        f"--user-data-dir={profile}",
        f"--proxy-server={proxy_url}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={int(debug_port)}",
        "about:blank",
    ]


@dataclass(frozen=True)
class ProxySpec:
    url: str
    endpoint: str
    hash: str
    container_url: str = ""


@dataclass(frozen=True)
class OutlookJob:
    credential: dict[str, str]
    proxy: ProxySpec


@dataclass(frozen=True)
class OutlookPlan:
    jobs: list[OutlookJob]
    skipped_registered: int
    skipped_state: int
    skipped_no_proxy: int

    @property
    def skipped(self) -> int:
        return self.skipped_registered + self.skipped_state + self.skipped_no_proxy


def proxy_endpoint_key(value: object) -> str:
    raw = str(getattr(value, "url", value) or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return ""
    port = int(parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
    return f"{host}:{port}"


def is_local_only_proxy_url(value: object) -> bool:
    raw = str(getattr(value, "url", value) or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    return str(parsed.hostname or "").strip().lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
        "0.0.0.0",
    }


def proxy_hash(value: object) -> str:
    return hashlib.sha256(proxy_endpoint_key(value).encode("utf-8")).hexdigest()[:12]


def dedicated_proxy_node_id(email: object) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()[:12]


def build_dedicated_privoxy_config(warp_container: str) -> str:
    return "\n".join(
        (
            "listen-address 0.0.0.0:8118",
            "toggle 1",
            "enable-remote-toggle 0",
            "enable-remote-http-toggle 0",
            "enable-edit-actions 0",
            "enforce-blocks 0",
            "buffer-limit 4096",
            "keep-alive-timeout 15",
            "socket-timeout 90",
            "max-client-connections 128",
            f"forward-socks5t / {warp_container}:1080 .",
            "",
        )
    )


def parse_proxy_line(value: str) -> ProxySpec:
    line = str(value or "").strip()
    if not line:
        raise ValueError("empty proxy")
    if "://" in line:
        parsed = urlsplit(line)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("unsupported proxy URL")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username or password else ""
        url = urlunsplit((parsed.scheme, f"{auth}{parsed.hostname}:{parsed.port}", "", "", ""))
    else:
        parts = line.split(":", 3)
        if len(parts) != 4:
            raise ValueError("proxy must be URL or host:port:user:password")
        host, port_text, username, password = [part.strip() for part in parts]
        port = int(port_text)
        if not host or not username or not password or not (1 <= port <= 65535):
            raise ValueError("invalid proxy fields")
        url = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    endpoint = proxy_endpoint_key(url)
    if not endpoint:
        raise ValueError("proxy endpoint missing")
    return ProxySpec(url=url, endpoint=endpoint, hash=proxy_hash(url))


def parse_proxy_pool(value: object) -> list[ProxySpec]:
    seen: set[str] = set()
    result: list[ProxySpec] = []
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec = parse_proxy_line(line)
        if spec.endpoint in seen:
            continue
        seen.add(spec.endpoint)
        result.append(spec)
    return result


def enabled_outlook_credentials(config: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    from services.register.mail_provider import parse_outlook_credentials

    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    provider_settings: dict[str, Any] = {
        "mode": "auto",
        "imap_host": "outlook.live.com",
        "message_limit": 10,
    }
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("type") != "outlook_token" or not provider.get("enable", True):
            continue
        provider_settings.update(
            {
                "mode": str(provider.get("mode") or "auto"),
                "imap_host": str(provider.get("imap_host") or "outlook.live.com"),
                "message_limit": max(1, int(provider.get("message_limit") or 10)),
            }
        )
        for credential in parse_outlook_credentials(str(provider.get("mailboxes") or "")):
            email = normalize_email(credential.get("email"))
            if email and email not in seen:
                seen.add(email)
                credentials.append(credential)
    return credentials, provider_settings


def uses_real_browser_outlook(config: dict[str, Any]) -> bool:
    """仅当启用的邮件 provider 全是 outlook_token 时走真实浏览器。

    若同时启用 yumail / tempmail 等，改走协议注册引擎，避免 Outlook 独占整单任务。
    """
    credentials, _ = enabled_outlook_credentials(config)
    if not credentials:
        return False
    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    enabled = [
        item
        for item in providers
        if isinstance(item, dict) and item.get("enable", True)
    ]
    if not enabled:
        return True
    return all(str(item.get("type") or "").strip() == "outlook_token" for item in enabled)


def plan_outlook_jobs(
    credentials: Iterable[dict[str, str]],
    proxies: Iterable[ProxySpec],
    *,
    existing_emails: set[str],
    used_proxy_endpoints: set[str] | dict[str, int] | None = None,
    outlook_state: dict[str, Any] | None = None,
    dedicated_runtime: bool = False,
    max_accounts_per_proxy: int | None = None,
) -> OutlookPlan:
    state = outlook_state or {}
    try:
        from services.config import config as _cfg

        capacity = max(1, int(max_accounts_per_proxy or getattr(_cfg, "proxy_binding_max_accounts", 5) or 5))
    except Exception:
        capacity = max(1, int(max_accounts_per_proxy or 5))

    # used: endpoint -> current active account count (legacy set = fully saturated)
    usage: dict[str, int] = {}
    if isinstance(used_proxy_endpoints, dict):
        usage = {str(k): max(0, int(v or 0)) for k, v in used_proxy_endpoints.items()}
    elif used_proxy_endpoints:
        usage = {str(ep): capacity for ep in used_proxy_endpoints}

    available_proxies = [proxy for proxy in proxies if int(usage.get(proxy.endpoint, 0)) < capacity]
    jobs: list[OutlookJob] = []
    skipped_registered = 0
    skipped_state = 0
    skipped_no_proxy = 0

    def credential_priority(raw: dict[str, str]) -> int:
        record = state.get(normalize_email(raw.get("email")))
        current_state = str(record.get("state") or "") if isinstance(record, dict) else ""
        return 1 if current_state == "failed" else 0

    ordered_credentials = list(credentials)
    ordered_credentials.sort(key=credential_priority)
    for raw in ordered_credentials:
        credential = dict(raw)
        email = normalize_email(credential.get("email"))
        if not email or not email.endswith(OUTLOOK_DOMAINS):
            skipped_state += 1
            continue
        if email in existing_emails:
            skipped_registered += 1
            continue
        record = state.get(email) if isinstance(state, dict) else None
        current_state = str((record or {}).get("state") or "") if isinstance(record, dict) else ""
        state_blocks = current_state in {"used", "token_invalid"}
        if current_state == "in_use" and isinstance(record, dict):
            try:
                updated_at = datetime.fromisoformat(str(record.get("updated_at") or ""))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                state_blocks = (
                    datetime.now(timezone.utc) - updated_at
                ).total_seconds() < OUTLOOK_IN_USE_STALE_SECONDS
            except (TypeError, ValueError):
                state_blocks = False
        if state_blocks:
            skipped_state += 1
            continue
        if dedicated_runtime:
            placeholder = ProxySpec(
                url=HOST_REGISTER_PROXY,
                endpoint=f"dedicated:{dedicated_proxy_node_id(email)}",
                hash=dedicated_proxy_node_id(email),
            )
            jobs.append(OutlookJob(credential=credential, proxy=placeholder))
            continue
        chosen: ProxySpec | None = None
        # 优先填不满容量的 endpoint（同 IP 最多 capacity 个号）
        ranked = sorted(available_proxies, key=lambda item: (int(usage.get(item.endpoint, 0)), item.endpoint))
        for proxy in ranked:
            if int(usage.get(proxy.endpoint, 0)) < capacity:
                chosen = proxy
                break
        if chosen is None:
            skipped_no_proxy += 1
            continue
        jobs.append(OutlookJob(credential=credential, proxy=chosen))
        usage[chosen.endpoint] = int(usage.get(chosen.endpoint, 0)) + 1
    return OutlookPlan(
        jobs=jobs,
        skipped_registered=skipped_registered,
        skipped_state=skipped_state,
        skipped_no_proxy=skipped_no_proxy,
    )


class BrowserRuntimeError(RuntimeError):
    def __init__(self, code: str, *, stage: str = "browser", captcha: bool = False):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.captcha = captcha


class WslBrowserRunner:
    def __init__(
        self,
        *,
        distro: str = "HermesUbuntu",
        container: str = "chatgpt2api-flaresolverr",
        command_timeout: int = 120,
    ) -> None:
        self.distro = distro
        self.container = container
        self.command_timeout = command_timeout

    def _run(
        self,
        args: list[str],
        *,
        input_data: bytes | None = None,
        timeout: int | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["wsl", "-d", self.distro, "--", *args]
        completed = subprocess.run(
            command,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or self.command_timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="ignore").strip()[:300]
            raise BrowserRuntimeError(f"runtime_command_failed:{detail or completed.returncode}", stage="runtime")
        return completed

    def _docker(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return self._run(["docker", *args], **kwargs)

    def preflight(self) -> dict[str, Any]:
        if os.name != "nt":
            raise BrowserRuntimeError("real_browser_requires_windows_wsl", stage="runtime")
        for container in LOCAL_PROXY_CONTAINERS:
            result = self._docker(
                ["inspect", "-f", "{{.State.Running}}", container],
                timeout=20,
            )
            if result.stdout.decode().strip().lower() != "true":
                raise BrowserRuntimeError(f"proxy_container_not_running:{container}", stage="runtime")
        display = self._detect_display()
        trace = self._docker(
            [
                "exec",
                self.container,
                "curl",
                "-fsS",
                "--max-time",
                "20",
                "-x",
                CONTAINER_REGISTER_PROXY,
                "https://www.cloudflare.com/cdn-cgi/trace",
            ],
            timeout=30,
        ).stdout.decode("utf-8", errors="ignore")
        if "warp=on" not in trace:
            raise BrowserRuntimeError("container_privoxy_warp_probe_failed", stage="runtime")
        return {
            "container": self.container,
            "display": display,
            "browser_proxy": CONTAINER_REGISTER_PROXY,
            "warp": True,
        }

    def _detect_display(self) -> str:
        result = self._docker(["exec", self.container, "ps", "-ef"], timeout=20)
        match = re.search(r"\bXvfb\s+:(\d+)", result.stdout.decode("utf-8", errors="ignore"))
        if not match:
            raise BrowserRuntimeError("xvfb_display_not_found", stage="runtime")
        return f":{match.group(1)}"

    def reconnect_warp(self) -> dict[str, Any]:
        container = "chatgpt2api-warp-proxy"
        self._docker(
            ["exec", container, "warp-cli", "--accept-tos", "disconnect"],
            timeout=20,
            check=False,
        )
        time.sleep(2)
        self._docker(
            ["exec", container, "warp-cli", "--accept-tos", "connect"],
            timeout=20,
            check=False,
        )
        status = ""
        for _ in range(30):
            result = self._docker(
                ["exec", container, "warp-cli", "--accept-tos", "status"],
                timeout=20,
                check=False,
            )
            status = result.stdout.decode("utf-8", errors="ignore")
            if "Status update: Connected" in status:
                break
            time.sleep(2)
        if "Status update: Connected" not in status:
            raise BrowserRuntimeError("warp_reconnect_timeout", stage="runtime")
        trace = self._docker(
            [
                "exec",
                self.container,
                "curl",
                "-fsS",
                "--max-time",
                "20",
                "-x",
                CONTAINER_REGISTER_PROXY,
                "https://www.cloudflare.com/cdn-cgi/trace",
            ],
            timeout=30,
        ).stdout.decode("utf-8", errors="ignore")
        if "warp=on" not in trace:
            raise BrowserRuntimeError("warp_reconnect_probe_failed", stage="runtime")
        return {"connected": True, "healthy": "Network: healthy" in status}

    def _write_file(self, path: str, data: bytes) -> None:
        self._docker(
            [
                "exec",
                "-i",
                "-u",
                "flaresolverr",
                self.container,
                "sh",
                "-c",
                f"umask 077; cat > {path}",
            ],
            input_data=data,
            timeout=30,
        )

    def _read_file(self, path: str) -> str:
        result = self._docker(
            ["exec", self.container, "sh", "-c", f"cat {path} 2>/dev/null || true"],
            timeout=20,
            check=False,
        )
        return result.stdout.decode("utf-8", errors="ignore").strip()

    def _file_exists(self, path: str) -> bool:
        result = self._docker(
            ["exec", self.container, "test", "-f", path],
            timeout=20,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _browser_failure(result: dict[str, Any]) -> BrowserRuntimeError:
        raw = str(result.get("error_code") or "").strip()
        if not raw or raw.lower().startswith("message:"):
            raw = str(result.get("error") or result.get("stage") or "browser_failed").strip()
        path = str(result.get("url_path") or "").strip()
        markers = result.get("markers") if isinstance(result.get("markers"), dict) else {}
        marker_names = [str(key) for key, value in markers.items() if value]
        code = raw
        if path:
            code += f"@{path}"
        if marker_names:
            code += f"[{','.join(marker_names)}]"
        visible_text = " ".join(str(result.get("visible_text") or "").split())[:160]
        if visible_text:
            code += f" view={visible_text}"
        input_meta = result.get("input_meta")
        if isinstance(input_meta, list) and input_meta:
            code += f" inputs={json.dumps(input_meta[:6], ensure_ascii=False, separators=(',', ':'))}"
        button_meta = result.get("button_meta")
        if isinstance(button_meta, list) and button_meta:
            code += f" buttons={json.dumps(button_meta[:4], ensure_ascii=False, separators=(',', ':'))}"
        birthday_meta = result.get("birthday_meta")
        if isinstance(birthday_meta, list) and birthday_meta:
            code += f" birthday={json.dumps(birthday_meta[:6], ensure_ascii=False, separators=(',', ':'))}"
        network_events = result.get("network_events")
        if isinstance(network_events, list) and network_events:
            code += f" network={json.dumps(network_events[-10:], ensure_ascii=False, separators=(',', ':'))}"
        return BrowserRuntimeError(
            code,
            stage=str(result.get("stage") or "browser"),
            captcha=bool(result.get("captcha") or markers.get("captcha")),
        )

    def _cleanup(self, base: str) -> None:
        self._docker(
            [
                "exec",
                "-u",
                "flaresolverr",
                self.container,
                "sh",
                "-c",
                f"if [ -f {base}/browser.pid ]; then pid=$(cat {base}/browser.pid); "
                f"pkill -TERM -P $pid 2>/dev/null || true; kill -TERM $pid 2>/dev/null || true; fi; rm -rf {base}",
            ],
            timeout=30,
            check=False,
        )

    def run(
        self,
        credential: dict[str, str],
        proxy: ProxySpec,
        otp_resolver: Callable[[datetime], str],
        *,
        account_password: str,
        request_timeout: int = 180,
        result_timeout: int = 240,
    ) -> dict[str, Any]:
        display = self._detect_display()
        job_id = uuid.uuid4().hex
        base = f"/tmp/gptimage-register/{job_id}"
        credential_line = "----".join(
            str(credential.get(key) or "")
            for key in ("email", "password", "client_id", "refresh_token")
        )
        self._docker(["exec", "-u", "flaresolverr", self.container, "mkdir", "-p", base], timeout=20)
        try:
            self._write_file(f"{base}/worker.py", Path(__file__).read_bytes())
            self._write_file(f"{base}/credential.secret.txt", credential_line.encode("utf-8"))
            self._write_file(f"{base}/openai-password.secret.txt", str(account_password).encode("utf-8"))
            browser_proxy_url = str(proxy.container_url or proxy.url).strip()
            self._write_file(f"{base}/proxy.secret.txt", browser_proxy_url.encode("utf-8"))
            self._docker(
                [
                    "exec",
                    "-d",
                    "-e",
                    f"DISPLAY={display}",
                    "-e",
                    f"GPTIMAGE_BROWSER_BASE={base}",
                    "-u",
                    "flaresolverr",
                    self.container,
                    "sh",
                    "-c",
                    f"cd {base} && echo $$ > browser.pid && exec python3 worker.py --browser-worker >browser.out 2>browser.err",
                ],
                timeout=30,
            )
            deadline = time.monotonic() + max(30, request_timeout)
            while time.monotonic() < deadline:
                if self._file_exists(f"{base}/otp-request.json"):
                    break
                early = self._read_file(f"{base}/browser.out")
                if early:
                    result = json.loads(early)
                    if result.get("ok"):
                        return result
                    raise self._browser_failure(result)
                time.sleep(1)
            else:
                raise BrowserRuntimeError("otp_request_timeout", stage="browser")

            request = json.loads(self._read_file(f"{base}/otp-request.json"))
            boundary = datetime.fromisoformat(str(request["not_before"]))
            code = str(otp_resolver(boundary) or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                raise BrowserRuntimeError("otp_not_received", stage="mail")
            self._write_file(f"{base}/otp.secret.txt", code.encode("ascii"))

            deadline = time.monotonic() + max(60, result_timeout)
            while time.monotonic() < deadline:
                text = self._read_file(f"{base}/browser.out")
                if text:
                    result = json.loads(text)
                    if result.get("ok"):
                        return result
                    raise self._browser_failure(result)
                time.sleep(1)
            raise BrowserRuntimeError("browser_result_timeout", stage="browser")
        finally:
            self._cleanup(base)


class DedicatedWarpProxyManager:
    """Provision one persistent WARP + Privoxy pair for each Outlook account."""

    def __init__(
        self,
        runner: WslBrowserRunner,
        *,
        network: str = "gptimage_default",
        port_start: int = 41000,
        port_end: int = 41999,
    ) -> None:
        self.runner = runner
        self.network = network
        self.port_start = port_start
        self.port_end = port_end

    def _exists(self, name: str) -> bool:
        return self.runner._docker(["inspect", name], timeout=20, check=False).returncode == 0

    def _start_existing(self, name: str) -> None:
        self.runner._docker(["start", name], timeout=30, check=False)

    def _mapped_port(self, proxy_name: str) -> int:
        result = self.runner._docker(
            ["port", proxy_name, "8118/tcp"],
            timeout=20,
            check=False,
        )
        match = re.search(r":(\d+)\s*$", result.stdout.decode("utf-8", errors="ignore"))
        return int(match.group(1)) if match else 0

    def _allocate_port(self) -> int:
        used: set[int] = set()
        result = self.runner._docker(["ps", "-a", "--format", "{{.Ports}}"], timeout=20, check=False)
        for value in re.findall(r"127\.0\.0\.1:(\d+)->8118/tcp", result.stdout.decode("utf-8", errors="ignore")):
            used.add(int(value))
        for port in range(self.port_start, self.port_end + 1):
            if port in used:
                continue
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    continue
            except OSError:
                return port
        raise BrowserRuntimeError("dedicated_proxy_port_pool_exhausted", stage="runtime")

    def _create_warp(self, name: str) -> None:
        self.runner._docker(
            [
                "run",
                "-d",
                "--name",
                name,
                "--network",
                self.network,
                "--restart",
                "unless-stopped",
                "--cap-add",
                "NET_ADMIN",
                "--cap-add",
                "SYS_MODULE",
                "--sysctl",
                "net.ipv6.conf.all.disable_ipv6=0",
                "--sysctl",
                "net.ipv4.conf.all.src_valid_mark=1",
                "caomingjun/warp:latest",
            ],
            timeout=60,
        )

    def _create_privoxy(self, name: str, warp_name: str, port: int) -> None:
        encoded = base64.b64encode(build_dedicated_privoxy_config(warp_name).encode("ascii")).decode("ascii")
        command = f"echo {encoded} | base64 -d > /tmp/privoxy.conf && exec privoxy --no-daemon /tmp/privoxy.conf"
        self.runner._docker(
            [
                "run",
                "-d",
                "--name",
                name,
                "--network",
                self.network,
                "--restart",
                "unless-stopped",
                "-p",
                f"127.0.0.1:{port}:8118",
                "vimagick/privoxy:latest",
                "sh",
                "-c",
                command,
            ],
            timeout=60,
        )

    def ensure(
        self,
        email: object,
        *,
        disallowed_ip_hashes: set[str] | None = None,
        max_ip_attempts: int = 3,
    ) -> tuple[ProxySpec, str]:
        node_id = dedicated_proxy_node_id(email)
        warp_name = f"gptimage-warp-{node_id}"
        proxy_name = f"gptimage-privoxy-{node_id}"
        if not self._exists(warp_name):
            self._create_warp(warp_name)
        else:
            self._start_existing(warp_name)
        if not self._exists(proxy_name):
            port = self._allocate_port()
            self._create_privoxy(proxy_name, warp_name, port)
        else:
            self._start_existing(proxy_name)
            port = self._mapped_port(proxy_name)
        if not port:
            raise BrowserRuntimeError("dedicated_proxy_port_missing", stage="runtime")

        container_url = f"http://{proxy_name}:8118"
        blocked = {str(item or "").strip() for item in (disallowed_ip_hashes or set()) if str(item or "").strip()}
        attempts = max(1, int(max_ip_attempts or 1))
        for ip_attempt in range(attempts):
            trace = ""
            for _ in range(30):
                result = self.runner._docker(
                    [
                        "exec",
                        self.runner.container,
                        "curl",
                        "-fsS",
                        "--max-time",
                        "15",
                        "-x",
                        container_url,
                        "https://www.cloudflare.com/cdn-cgi/trace",
                    ],
                    timeout=25,
                    check=False,
                )
                trace = result.stdout.decode("utf-8", errors="ignore")
                if result.returncode == 0 and "warp=on" in trace:
                    break
                time.sleep(3)
            else:
                raise BrowserRuntimeError("dedicated_warp_proxy_not_ready", stage="runtime")
            ip_match = re.search(r"^ip=(.+)$", trace, re.MULTILINE)
            raw_ip = str(ip_match.group(1) if ip_match else "").strip()
            if not raw_ip:
                raise BrowserRuntimeError("dedicated_warp_ip_missing", stage="runtime")
            ip_hash = hashlib.sha256(raw_ip.encode()).hexdigest()[:12]
            if ip_hash not in blocked:
                url = f"http://127.0.0.1:{port}"
                return ProxySpec(
                    url=url,
                    endpoint=f"127.0.0.1:{port}",
                    hash=proxy_hash(url),
                    container_url=container_url,
                ), ip_hash
            if ip_attempt + 1 >= attempts:
                raise BrowserRuntimeError("dedicated_warp_ip_collision", stage="runtime")
            self.runner._docker(["restart", warp_name], timeout=45, check=False)
            time.sleep(3)
        raise BrowserRuntimeError("dedicated_warp_ip_collision", stage="runtime")

    def reconnect(
        self,
        email: object,
        *,
        disallowed_ip_hashes: set[str] | None = None,
    ) -> tuple[ProxySpec, str]:
        node_id = dedicated_proxy_node_id(email)
        self.runner._docker(["restart", f"gptimage-warp-{node_id}"], timeout=45, check=False)
        return self.ensure(email, disallowed_ip_hashes=disallowed_ip_hashes)

    def remove(self, email: object) -> None:
        node_id = dedicated_proxy_node_id(email)
        for name in (f"gptimage-privoxy-{node_id}", f"gptimage-warp-{node_id}"):
            self.runner._docker(["rm", "-f", name], timeout=45, check=False)


class RealBrowserOutlookRegistrar:
    def __init__(
        self,
        account_service: Any,
        *,
        log: Callable[[str, str], None] | None = None,
        host_register_proxy: str = HOST_REGISTER_PROXY,
        container_register_proxy: str = CONTAINER_REGISTER_PROXY,
    ) -> None:
        self.account_service = account_service
        self.log = log or (lambda _text, _level="": None)
        self.host_register_proxy = str(host_register_proxy or HOST_REGISTER_PROXY).strip()
        self.browser_proxy = parse_proxy_line(container_register_proxy or CONTAINER_REGISTER_PROXY)
        self.browser = WslBrowserRunner()
        self.dedicated_proxies = DedicatedWarpProxyManager(self.browser)

    def preflight(self) -> dict[str, Any]:
        parsed = urlparse(self.host_register_proxy)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            raise BrowserRuntimeError("host_register_proxy_invalid", stage="runtime")
        try:
            with socket.create_connection((str(parsed.hostname), int(parsed.port)), timeout=3):
                pass
        except OSError as exc:
            raise BrowserRuntimeError("host_privoxy_40080_unavailable", stage="runtime") from exc
        result = self.browser.preflight()
        result["host_proxy"] = self.host_register_proxy
        return result

    def _mail_config(
        self,
        credential: dict[str, str],
        proxy: ProxySpec,
        mail: dict[str, Any],
        provider: dict[str, Any],
        registration_proxy: str = "",
    ) -> dict[str, Any]:
        email = normalize_email(credential.get("email"))
        domain = email.split("@", 1)[-1] if "@" in email else ""
        imap_host = str(provider.get("imap_host") or "").strip()
        if not imap_host:
            imap_host = "outlook.live.com" if domain in {"outlook.com", "hotmail.com", "live.com"} else "outlook.office365.com"
        return {
            "request_timeout": max(10, int(mail.get("request_timeout") or 30)),
            "wait_timeout": max(30, int(mail.get("wait_timeout") or 180)),
            "wait_interval": max(1, int(mail.get("wait_interval") or 2)),
            "user_agent": self.account_service._OAUTH_USER_AGENT,
            "api_use_register_proxy": True,
            "proxy": str(registration_proxy or self.host_register_proxy).strip(),
            "providers": [
                {
                    "type": "outlook_token",
                    "enable": True,
                    "label": "LocalRealBrowserRegister",
                    "mode": str(provider.get("mode") or "auto"),
                    "imap_host": imap_host,
                    "message_limit": max(5, int(provider.get("message_limit") or 10)),
                    "mailboxes": [credential],
                }
            ],
        }

    def register_one(
        self,
        job: OutlookJob,
        *,
        mail: dict[str, Any],
        provider: dict[str, Any],
    ) -> dict[str, Any]:
        from scripts.recover_panda_outlook_accounts import (
            build_mailbox,
            build_staged_account,
            build_verified_updates,
            login_with_chatgpt_email_otp,
            preflight_mailbox_access,
            prime_mailbox_messages,
            sanitize_error,
        )
        from services.register import mail_provider

        credential = job.credential
        proxy = job.proxy
        dedicated_proxy = proxy
        dedicated_ip_hash = ""
        email = normalize_email(credential.get("email"))
        used_ip_hashes = {
            str(item.get("proxy_egress_hash") or "").strip()
            for item in self.account_service.list_accounts()
            if normalize_email(item.get("email")) != email
            and str(item.get("proxy_egress_hash") or "").strip()
        }
        state_mailbox = {"provider": "outlook_token", "address": email}
        mail_provider._set_outlook_token_state(email, "in_use")
        token = ""

        def cleanup_dedicated_runtime() -> None:
            try:
                self.dedicated_proxies.remove(email)
            except Exception:
                pass

        try:
            dedicated_proxy, dedicated_ip_hash = self.dedicated_proxies.ensure(
                email,
                disallowed_ip_hashes=used_ip_hashes,
            )
            self.log(
                f"独享 WARP 节点就绪：{mask_email(email)}，节点={dedicated_proxy.hash}，出口={dedicated_ip_hash}；注册与首次登录走稳定 40080",
                "green",
            )
            mail_config = self._mail_config(
                credential,
                proxy,
                mail,
                provider,
                registration_proxy=self.host_register_proxy,
            )
            prime_boundary = datetime.now(timezone.utc)
            browser_mailbox = build_mailbox(credential, prime_boundary)
            try:
                preflight = preflight_mailbox_access(mail_config, browser_mailbox)
                if browser_mailbox.get("_outlook_imap_host"):
                    mail_config["providers"][0]["imap_host"] = browser_mailbox["_outlook_imap_host"]
                prime_mailbox_messages(mail_config, browser_mailbox)
            except Exception as exc:
                mail_provider._release_outlook_token_state(email)
                cleanup_dedicated_runtime()
                return {
                    "ok": False,
                    "stage": "mail_preflight",
                    "error": sanitize_error(f"{type(exc).__name__}: {exc}"),
                    "transient": True,
                    "email": mask_email(email),
                    "proxy_hash": dedicated_proxy.hash,
                }

            def resolve_browser_otp(boundary: datetime) -> str:
                browser_mailbox["_code_not_before"] = boundary - timedelta(seconds=OUTLOOK_CLOCK_TOLERANCE_SEC)
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        return str(mail_provider.wait_for_code(mail_config, browser_mailbox) or "")
                    except Exception as exc:
                        last_error = exc
                        if attempt >= 3 or not is_transient_registration_error(exc):
                            raise
                        self.log(
                            f"稳定注册链/邮箱读信瞬断，第{attempt}次重试：{mask_email(email)}",
                            "yellow",
                        )
                        time.sleep(attempt * 2)
                raise last_error or RuntimeError("otp_not_received")

            account_password = generate_openai_account_password()
            browser_result: dict[str, Any] = {}
            for browser_attempt in range(1, 4):
                try:
                    browser_result = self.browser.run(
                        credential,
                        self.browser_proxy,
                        resolve_browser_otp,
                        account_password=account_password,
                        request_timeout=int(mail_config["wait_timeout"]) + 60,
                    )
                    break
                except BrowserRuntimeError as browser_error:
                    if browser_attempt >= 3 or not should_retry_browser_session(browser_error):
                        raise
                    self.log(
                        f"注册页面重渲染，重开浏览器会话第{browser_attempt + 1}次：{mask_email(email)}",
                        "yellow",
                    )
                    time.sleep(browser_attempt * 2)
            if not browser_result.get("ok"):
                raise BrowserRuntimeError("browser_registration_failed")
            self.log(
                f"浏览器注册阶段完成：{mask_email(email)}，状态={browser_result.get('stage')}，开始通过稳定 40080 首次登录",
                "green",
            )

            login_result: dict[str, Any] = {"ok": False, "stage": "not_started"}
            for login_attempt in range(1, 4):
                login_mailbox = build_mailbox(credential, datetime.now(timezone.utc))
                login_result = login_with_chatgpt_email_otp(
                    account_service=self.account_service,
                    email=email,
                    mail_config=mail_config,
                    mailbox=login_mailbox,
                    wait_for_code=mail_provider.wait_for_code,
                    prime_mailbox=prime_mailbox_messages,
                    proxy=self.host_register_proxy,
                )
                if login_result.get("ok"):
                    break
                login_error = f"{login_result.get('stage')}: {login_result.get('error')}"
                if login_attempt >= 3 or not is_transient_registration_error(login_error):
                    break
                if should_reconnect_registration_warp(login_error):
                    try:
                        self.browser.reconnect_warp()
                        self.log(
                            f"稳定注册链 WARP 已重连，准备第{login_attempt + 1}次首次登录：{mask_email(email)}",
                            "yellow",
                        )
                    except BrowserRuntimeError as reconnect_error:
                        self.log(
                            f"稳定注册链 WARP 重连未完成，继续按瞬断重试：{sanitize_error(reconnect_error, limit=120)}",
                            "yellow",
                        )
                self.log(
                    f"稳定注册链/首次登录瞬断，第{login_attempt}次重试：{mask_email(email)}",
                    "yellow",
                )
                time.sleep(login_attempt * 2)
            if not login_result.get("ok"):
                login_error = sanitize_error(
                    f"{login_result.get('stage')}: {login_result.get('error')}"
                )
                if is_registered_unusable_login_error(login_error):
                    mail_provider._set_outlook_token_state(
                        email,
                        "used",
                        "registered account unavailable: account_deactivated",
                    )
                    cleanup_dedicated_runtime()
                    return {
                        "ok": False,
                        "skipped": True,
                        "stage": "registered_skip",
                        "error": "account_deactivated",
                        "email": mask_email(email),
                        "proxy_hash": dedicated_proxy.hash,
                    }
                raise BrowserRuntimeError(
                    f"login_failed:{login_error}",
                    stage="login",
                )
            token = str(login_result.get("access_token") or "").strip()
            if not token:
                raise BrowserRuntimeError("login_returned_no_token", stage="login")

            self.log(
                f"首次登录完成：{mask_email(email)}，切换到独享 WARP 节点并执行后端验号",
                "green",
            )
            now = datetime.now(timezone.utc).isoformat()
            staged = build_staged_account(
                {"email": email, "password": account_password},
                login_result,
                dedicated_proxy.url,
                now,
            )
            staged["source_detail"] = "local_real_browser_register_split_proxy"
            staged["proxy_scope"] = "local_dedicated_warp"
            staged["proxy_egress_hash"] = dedicated_ip_hash
            staged["registration_proxy_scope"] = "shared_stable_warp"
            staged["registration_proxy_endpoint"] = "127.0.0.1:40080"
            staged["lifecycle_ip_mode"] = "split_registration_dedicated_runtime"
            staged["panda_sync_state"] = "local_proxy_only"
            staged["panda_sync_last_error"] = "local dedicated proxy is not reachable from Panda"
            self.account_service.add_account_items([staged], include_items=False)
            try:
                refresh = self.account_service.refresh_accounts([token], defer_invalid_removal=True, include_items=False)
                if refresh.get("errors") or int(refresh.get("refreshed") or 0) <= 0:
                    errors = refresh.get("errors") if isinstance(refresh.get("errors"), list) else []
                    first_error = errors[0] if errors else {}
                    detail = sanitize_error(
                        first_error.get("error") if isinstance(first_error, dict) else first_error,
                        limit=180,
                    )
                    raise BrowserRuntimeError(
                        f"backend_verification_failed:{detail or 'no_refresh'}",
                        stage="verify",
                    )
                committed = self.account_service.resolve_access_token(token) or token
                verified = self.account_service.get_account(committed)
                if not isinstance(verified, dict) or normalize_email(verified.get("email")) != email:
                    raise BrowserRuntimeError("verified_account_mismatch", stage="verify")
                updates = build_verified_updates(datetime.now(timezone.utc).isoformat())
                updates.update(
                    {
                        "proxy": dedicated_proxy.url,
                        "proxy_scope": "local_dedicated_warp",
                        "proxy_egress_hash": dedicated_ip_hash,
                        "registration_proxy_scope": "shared_stable_warp",
                        "registration_proxy_endpoint": "127.0.0.1:40080",
                        "lifecycle_ip_mode": "split_registration_dedicated_runtime",
                        "source_detail": "local_real_browser_register_split_proxy",
                        "panda_sync_state": "local_proxy_only",
                        "panda_sync_last_error": "local dedicated proxy is not reachable from Panda",
                    }
                )
                self.account_service.update_account(committed, updates, quiet=True)
                token = committed
            except Exception:
                self.account_service.delete_accounts(
                    [self.account_service.resolve_access_token(token) or token],
                    include_items=False,
                )
                raise
            mail_provider.mark_mailbox_result(state_mailbox, success=True)
            return {
                "ok": True,
                "result": {"access_token": token},
                "email": mask_email(email),
                "proxy_hash": dedicated_proxy.hash,
                "runtime_ip_hash": dedicated_ip_hash,
                "registration_proxy_hash": self.browser_proxy.hash,
                "lifecycle_ip_mode": "split_registration_dedicated_runtime",
                "browser_stage": browser_result.get("stage"),
                "mailbox_preflight": preflight,
                "local_proxy_only": True,
            }
        except BrowserRuntimeError as exc:
            cleanup_dedicated_runtime()
            mail_provider.mark_mailbox_result(state_mailbox, success=False, error=exc)
            return {
                "ok": False,
                "stage": exc.stage,
                "error": exc.code,
                "captcha": exc.captcha,
                "email": mask_email(email),
                "proxy_hash": dedicated_proxy.hash,
            }
        except Exception as exc:
            cleanup_dedicated_runtime()
            if token:
                try:
                    self.account_service.delete_accounts(
                        [self.account_service.resolve_access_token(token) or token],
                        include_items=False,
                    )
                except Exception:
                    pass
            mail_provider.mark_mailbox_result(state_mailbox, success=False, error=exc)
            return {
                "ok": False,
                "stage": "register",
                "error": sanitize_error(f"{type(exc).__name__}: {exc}"),
                "email": mask_email(email),
                "proxy_hash": dedicated_proxy.hash,
            }


def _browser_worker() -> int:
    import shutil as _shutil

    import requests
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait

    base = Path(os.environ.get("GPTIMAGE_BROWSER_BASE", "/tmp/gptimage-register"))
    credential_line = (base / "credential.secret.txt").read_text(encoding="utf-8-sig").strip()
    proxy_url = (base / "proxy.secret.txt").read_text(encoding="utf-8-sig").strip()
    account_password = (base / "openai-password.secret.txt").read_text(encoding="utf-8-sig").strip()
    email, _mail_password, _client_id, _refresh_token = credential_line.split("----", 3)
    parsed = urlparse(proxy_url)
    upstream_host = parsed.hostname
    upstream_port = parsed.port
    upstream_user = unquote(parsed.username or "")
    upstream_password = unquote(parsed.password or "")

    class ProxyBridgeHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            request_line = self.rfile.readline(65536)
            if not request_line:
                return
            headers: list[bytes] = []
            while True:
                line = self.rfile.readline(65536)
                if line in (b"\r\n", b"\n", b""):
                    break
                headers.append(line)
            upstream = socket.create_connection((str(upstream_host), int(upstream_port or 80)), timeout=20)
            try:
                auth = base64.b64encode(f"{upstream_user}:{upstream_password}".encode()).decode()
                upstream.sendall(request_line)
                for line in headers:
                    if not line.lower().startswith(b"proxy-authorization:"):
                        upstream.sendall(line)
                upstream.sendall(f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
                if request_line.upper().startswith(b"CONNECT "):
                    response = b""
                    while b"\r\n\r\n" not in response:
                        chunk = upstream.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                    self.connection.sendall(response)
                    if b" 200 " not in response.split(b"\r\n", 1)[0]:
                        return
                sockets = [self.connection, upstream]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 60)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
            finally:
                upstream.close()

    class ThreadingProxyBridge(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    def signup_name(value: str) -> str:
        local = value.split("@", 1)[0]
        local = re.sub(r"\d+", " ", local)
        local = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", local)
        words = re.findall(r"[A-Za-z]{2,}", local)
        return " ".join(word.capitalize() for word in words[:3]) or "Alex Morgan"

    def wait_external_otp(boundary: datetime, timeout: int = 300) -> str:
        request_path = base / "otp-request.json"
        otp_path = base / "otp.secret.txt"
        otp_path.unlink(missing_ok=True)
        request_path.write_text(
            json.dumps({"not_before": boundary.isoformat(), "email": mask_email(email)}),
            encoding="utf-8",
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if otp_path.exists():
                code = otp_path.read_text(encoding="utf-8").strip()
                otp_path.unlink(missing_ok=True)
                request_path.unlink(missing_ok=True)
                if re.fullmatch(r"\d{6}", code):
                    return code
            time.sleep(1)
        raise RuntimeError("external_otp_timeout")

    def otp_step_finished(browser: Any) -> bool:
        if "email-verification" not in browser.current_url:
            return True
        for item in browser.find_elements(By.TAG_NAME, "input"):
            if not item.is_displayed():
                continue
            signature = " ".join(
                str(item.get_attribute(name) or "")
                for name in ("name", "placeholder", "aria-label", "inputmode")
            ).lower()
            if "code" in signature or "numeric" in signature or "decimal" in signature:
                return False
        return True

    bridge = ThreadingProxyBridge(("127.0.0.1", 0), ProxyBridgeHandler)
    bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    bridge_thread.start()
    profile = Path(tempfile.mkdtemp(prefix="chrome-profile-"))
    debug_socket = socket.socket()
    debug_socket.bind(("127.0.0.1", 0))
    debug_port = int(debug_socket.getsockname()[1])
    debug_socket.close()
    chromium = subprocess.Popen(
        build_attached_chromium_args(
            profile=str(profile),
            proxy_url=f"http://127.0.0.1:{bridge.server_address[1]}",
            debug_port=debug_port,
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", debug_port), timeout=1):
                break
        except OSError:
            if chromium.poll() is not None:
                raise RuntimeError("chromium_exited_before_cdp_ready")
            time.sleep(0.25)
    else:
        raise RuntimeError("chromium_cdp_ready_timeout")
    options = Options()
    options.debugger_address = f"127.0.0.1:{debug_port}"
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = webdriver.Chrome(service=Service("/app/chromedriver"), options=options)
    summary: dict[str, Any] = {"email": mask_email(email), "proxy_configured": True}
    summary["webdriver_flag"] = bool(driver.execute_script("return navigator.webdriver"))
    finish_clicked = False
    try:
        wait = WebDriverWait(driver, 45)
        driver.set_page_load_timeout(120)
        device_id = str(uuid.uuid4())
        authorize_url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(
            {
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "audience": "https://api.openai.com/v1",
                "redirect_uri": "https://chatgpt.com/api/auth/callback/openai",
                "response_type": "code",
                "scope": "openid email profile offline_access model.request model.read organization.read organization.write",
                "prompt": "login",
                "screen_hint": "signup",
                "login_hint": email,
                "device_id": device_id,
                "ext-oai-did": device_id,
                "ext-passkey-client-capabilities": "11111",
                "auth_session_logging_id": str(uuid.uuid4()),
                "state": str(uuid.uuid4()),
            }
        )
        boundary = datetime.now(timezone.utc)
        try:
            driver.get(authorize_url)
        except TimeoutException:
            if not is_registration_transition_path(driver.current_url):
                raise
        wait.until(
            lambda browser: is_registration_transition_path(browser.current_url)
        )
        path = urlparse(driver.current_url).path
        if path == "/api/auth/callback/openai":
            summary.update({"ok": True, "stage": "existing_account_or_callback", "url_path": path})
        else:
            source = driver.page_source.lower()
            if "verify you are human" in source or "captcha" in source or "turnstile" in source:
                summary.update({"ok": False, "stage": "captcha", "captcha": True, "url_path": path})
            else:
                if path.rstrip("/") == "/create-account/password":
                    visible_inputs = [item for item in driver.find_elements(By.TAG_NAME, "input") if item.is_displayed()]
                    password_input = next(
                        (
                            item
                            for item in visible_inputs
                            if is_signup_password_input(
                                item.get_attribute("type"),
                                item.get_attribute("name"),
                            )
                        ),
                        None,
                    )
                    if password_input is None:
                        raise RuntimeError("signup_password_input_missing")
                    password_input.click()
                    password_input.clear()
                    password_input.send_keys(account_password)
                    buttons = [
                        button
                        for button in driver.find_elements(By.TAG_NAME, "button")
                        if button.is_displayed() and "continue" in button.text.lower()
                    ]
                    if not buttons:
                        raise RuntimeError("signup_password_continue_missing")
                    boundary = datetime.now(timezone.utc)
                    buttons[-1].click()
                    WebDriverWait(driver, 45).until(
                        lambda browser: urlparse(browser.current_url).path.rstrip("/")
                        != "/create-account/password"
                    )
                    path = urlparse(driver.current_url).path
                if "email-verification" in path:
                    code = wait_external_otp(boundary)
                    visible_inputs = [item for item in driver.find_elements(By.TAG_NAME, "input") if item.is_displayed()]
                    otp_inputs = [
                        item
                        for item in visible_inputs
                        if (item.get_attribute("inputmode") or "").lower() in ("numeric", "decimal")
                        or "code"
                        in (
                            (item.get_attribute("name") or "")
                            + (item.get_attribute("placeholder") or "")
                            + (item.get_attribute("aria-label") or "")
                        ).lower()
                    ]
                    if len(otp_inputs) >= 6 and all((item.get_attribute("maxlength") or "") == "1" for item in otp_inputs[:6]):
                        for item, digit in zip(otp_inputs[:6], code):
                            item.send_keys(digit)
                        submit_input = otp_inputs[-1]
                    elif otp_inputs:
                        otp_inputs[0].click()
                        otp_inputs[0].clear()
                        otp_inputs[0].send_keys(code)
                        submit_input = otp_inputs[0]
                    elif visible_inputs:
                        visible_inputs[0].click()
                        visible_inputs[0].clear()
                        visible_inputs[0].send_keys(code)
                        submit_input = visible_inputs[0]
                    else:
                        raise RuntimeError("otp_input_missing")
                    time.sleep(1)
                    submitted = False
                    for submit_attempt in range(1, 4):
                        buttons = [
                            button
                            for button in driver.find_elements(By.TAG_NAME, "button")
                            if button.is_displayed() and "continue" in button.text.lower()
                        ]
                        if submit_attempt == 1 and buttons:
                            buttons[-1].click()
                        elif submit_attempt == 2 and buttons:
                            driver.execute_script("arguments[0].click()", buttons[-1])
                        else:
                            submit_input.send_keys(Keys.ENTER)
                        try:
                            WebDriverWait(driver, 20).until(otp_step_finished)
                            submitted = True
                            break
                        except Exception:
                            visible = driver.find_element(By.TAG_NAME, "body").text.lower()
                            if any(marker in visible for marker in ("incorrect", "invalid code", "expired", "wrong code")):
                                raise RuntimeError("wrong_email_otp_code")
                            if submit_attempt < 3:
                                time.sleep(2)
                    if not submitted:
                        raise RuntimeError("otp_submit_stuck")

                path = urlparse(driver.current_url).path
                if path.rstrip("/") == "/about-you":
                    def about_you_outcome(browser: Any) -> str | bool:
                        if urlparse(browser.current_url).path.rstrip("/") != "/about-you":
                            return "complete"
                        body_text = browser.find_element(By.TAG_NAME, "body").text
                        button_texts = [
                            button.text
                            for button in browser.find_elements(By.TAG_NAME, "button")
                            if button.is_displayed()
                        ]
                        if is_retryable_about_you_error(body_text, button_texts):
                            return "retry"
                        return False

                    for about_you_attempt in range(1, 4):
                        inputs = [item for item in driver.find_elements(By.TAG_NAME, "input") if item.is_displayed()]
                        name_input = next(
                            (item for item in inputs if (item.get_attribute("name") or "").lower() == "name"),
                            None,
                        )
                        age_input = next(
                            (item for item in inputs if (item.get_attribute("name") or "").lower() == "age"),
                            None,
                        )
                        if name_input is None:
                            raise RuntimeError("about_you_name_missing")
                        name_input.click()
                        name_input.send_keys(Keys.CONTROL, "a")
                        name_input.send_keys(signup_name(email))
                        if age_input is not None:
                            age_input.send_keys(Keys.CONTROL, "a")
                            age_input.send_keys("30")
                        else:
                            raw_segments = driver.find_elements(
                                By.CSS_SELECTOR,
                                '[role="spinbutton"], [contenteditable="true"]',
                            )
                            birthday_segments = []
                            seen_segments: set[str] = set()
                            for item in raw_segments:
                                if not item.is_displayed():
                                    continue
                                key = str(item.id)
                                if key in seen_segments:
                                    continue
                                seen_segments.add(key)
                                birthday_segments.append(item)
                            if len(birthday_segments) < 3:
                                raise RuntimeError("about_you_birthday_fields_missing")
                            for index, item in enumerate(birthday_segments[:3]):
                                value = birthday_segment_value(
                                    item.get_attribute("aria-label"),
                                    item.get_attribute("data-type")
                                    or item.get_attribute("data-segment"),
                                    index,
                                )
                                item.click()
                                item.send_keys(Keys.CONTROL, "a")
                                item.send_keys(value)
                        finish = [
                            button
                            for button in driver.find_elements(By.TAG_NAME, "button")
                            if button.is_displayed() and "finish creating account" in button.text.lower()
                        ]
                        if not finish:
                            raise RuntimeError("finish_account_button_missing")
                        finish_clicked = True
                        submit_method = about_you_submit_method(about_you_attempt)
                        try:
                            if submit_method == "native":
                                finish[-1].click()
                            elif submit_method == "javascript":
                                driver.execute_script("arguments[0].click()", finish[-1])
                            else:
                                finish[-1].send_keys(Keys.ENTER)
                        except Exception:
                            if about_you_attempt < 3:
                                continue
                            raise
                        try:
                            outcome = WebDriverWait(driver, 35).until(about_you_outcome)
                        except TimeoutException:
                            if about_you_attempt < 3:
                                continue
                            raise RuntimeError("about_you_submit_stuck")
                        if outcome == "complete":
                            break
                        if about_you_attempt >= 3:
                            raise RuntimeError("about_you_retry_exhausted")
                        retry_buttons = [
                            button
                            for button in driver.find_elements(By.TAG_NAME, "button")
                            if button.is_displayed() and "try again" in button.text.lower()
                        ]
                        if not retry_buttons:
                            raise RuntimeError("about_you_retry_button_missing")
                        driver.execute_script("arguments[0].click()", retry_buttons[-1])
                        WebDriverWait(driver, 45).until(
                            lambda browser: urlparse(browser.current_url).path.rstrip("/") != "/about-you"
                            or any(
                                item.is_displayed() and (item.get_attribute("name") or "").lower() == "name"
                                for item in browser.find_elements(By.TAG_NAME, "input")
                            )
                        )
                        if urlparse(driver.current_url).path.rstrip("/") != "/about-you":
                            break
                    final_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    if "registration_disallowed" in final_text or "registration is currently unavailable" in final_text:
                        raise RuntimeError("registration_disallowed")
                    summary.update(
                        {
                            "ok": True,
                            "stage": "account_submitted",
                            "url_path": urlparse(driver.current_url).path,
                            "about_you_submitted": True,
                        }
                    )
                else:
                    summary.update(
                        {
                            "ok": True,
                            "stage": "existing_account_or_callback",
                            "url_path": path,
                            "about_you_submitted": False,
                        }
                    )
    except Exception as exc:
        path = urlparse(driver.current_url).path
        if finish_clicked and path == "/api/auth/callback/openai":
            summary.update(
                {
                    "ok": True,
                    "stage": "account_submitted",
                    "url_path": path,
                    "about_you_submitted": True,
                }
            )
        else:
            source = driver.page_source.lower()
            visible_text = driver.find_element(By.TAG_NAME, "body").text[:300].replace(email, mask_email(email))
            visible_inputs = [item for item in driver.find_elements(By.TAG_NAME, "input") if item.is_displayed()]
            visible_buttons = [item for item in driver.find_elements(By.TAG_NAME, "button") if item.is_displayed()]
            birthday_controls = [
                item
                for item in driver.find_elements(
                    By.CSS_SELECTOR,
                    '[role="spinbutton"], [contenteditable="true"]',
                )
                if item.is_displayed()
            ]
            try:
                network_events = summarize_registration_network_events(driver.get_log("performance"))
            except Exception:
                network_events = []
            summary.update(
                {
                    "ok": False,
                    "stage": "browser",
                    "error": type(exc).__name__,
                    "error_code": str(exc)[:120],
                    "url_path": path,
                    "title": driver.title,
                    "visible_text": visible_text,
                    "visible_inputs": len(visible_inputs),
                    "input_meta": [
                        {
                            "type": str(item.get_attribute("type") or ""),
                            "name": str(item.get_attribute("name") or ""),
                            "inputmode": str(item.get_attribute("inputmode") or ""),
                            "maxlength": str(item.get_attribute("maxlength") or ""),
                            "value_len": len(str(item.get_attribute("value") or "")),
                        }
                        for item in visible_inputs[:6]
                    ],
                    "button_meta": [
                        {
                            "text": item.text[:50],
                            "enabled": item.is_enabled(),
                            "aria_disabled": str(item.get_attribute("aria-disabled") or ""),
                        }
                        for item in visible_buttons[:4]
                    ],
                    "birthday_meta": [
                        {
                            "role": str(item.get_attribute("role") or ""),
                            "aria_label": str(item.get_attribute("aria-label") or ""),
                            "data_type": str(item.get_attribute("data-type") or item.get_attribute("data-segment") or ""),
                            "contenteditable": str(item.get_attribute("contenteditable") or ""),
                        }
                        for item in birthday_controls[:6]
                    ],
                    "network_events": network_events,
                    "markers": {
                        "cloudflare": "cloudflare" in source or "just a moment" in driver.title.lower(),
                        "proxy_error": "err_proxy" in source or "proxy" in driver.title.lower(),
                        "connection_error": "err_connection" in source,
                        "captcha": "captcha" in source or "turnstile" in source,
                    },
                }
            )
    finally:
        try:
            driver.quit()
        finally:
            if chromium.poll() is None:
                chromium.terminate()
                try:
                    chromium.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    chromium.kill()
            bridge.shutdown()
            bridge.server_close()
            _shutil.rmtree(profile, ignore_errors=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-worker", action="store_true")
    args = parser.parse_args()
    if args.browser_worker:
        return _browser_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
