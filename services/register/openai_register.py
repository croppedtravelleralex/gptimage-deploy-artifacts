from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from services.account_service import account_service
from services.proxy_service import ClearanceBundle, proxy_settings
from services.register import mail_provider
from utils.helper import anonymize_token

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
register_post_verify_diagnostics_file = base_dir.parents[1] / "data" / "register_post_verify_diagnostics.jsonl"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


from utils.pkce import generate_pkce as _generate_pkce  # noqa: F401


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_SENSITIVE_RESPONSE_FIELD_RE = re.compile(
    r'("(?:access_token|refresh_token|id_token|token|code|authorization)"\s*:\s*")[^"]+(")',
    re.IGNORECASE,
)


def _redact_response_preview(value: object, limit: int = 800) -> str:
    text = str(value or "")[:limit]
    text = _SENSITIVE_RESPONSE_FIELD_RE.sub(r"\1<redacted>\2", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text, flags=re.IGNORECASE)
    return text


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={_redact_response_preview(json.dumps(data, ensure_ascii=False), limit)}")
    else:
        parts.append(f"body={_redact_response_preview(getattr(resp, 'text', '') or '', limit)}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def _mail_config() -> dict:
    profile = proxy_settings.get_profile(proxy=config["proxy"], resource=True, upstream=True)
    # 邮箱/资源请求不直连：未配置 resource_proxy_url 时仍沿用注册代理；
    # 配了 resource_proxy_url 时才与 OpenAI 注册流拆分出口。
    return {**config["mail"], "proxy": profile.proxy_url}


def is_transient_register_error(error: Exception | str) -> bool:
    text = str(error or "").lower()
    transient_markers = (
        "connect tunnel failed",
        "response 502",
        "response 503",
        "response 504",
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (56)",
        "failed to connect",
        "could not connect to server",
        "connection refused",
        "actively refused",
        "tls connect error",
        "openssl_internal",
        "operation timed out",
        "timed out",
        "connection closed abruptly",
        "connection reset by peer",
        "connection aborted",
        "remote disconnected",
        "proxy",
        "network",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "too many requests",
        "rate limited",
        "http 429",
    )
    return any(marker in text for marker in transient_markers)


def _is_invalid_refresh_error(error: object) -> bool:
    text = str(error or "").lower()
    invalid_markers = (
        "token invalidated",
        "invalid access token",
        "invalid_access_token",
        "refresh_token_invalidated",
        "app_session_terminated",
        "account_deactivated",
        "http 401",
        "unauthorized",
        "session has ended",
    )
    return any(marker in text for marker in invalid_markers)


def _refresh_errors_text(refresh_result: dict) -> str:
    errors = refresh_result.get("errors") if isinstance(refresh_result, dict) else []
    if not isinstance(errors, list):
        return str(errors or "")
    return "; ".join(
        str(item.get("error") if isinstance(item, dict) else item)
        for item in errors
        if str(item.get("error") if isinstance(item, dict) else item).strip()
    )


def _refresh_result_safe_message(refresh_result: dict[str, Any]) -> str:
    if not isinstance(refresh_result, dict):
        return "refresh_result_unavailable"
    errors = refresh_result.get("errors") if isinstance(refresh_result.get("errors"), list) else []
    error_text = _refresh_errors_text(refresh_result)
    if error_text:
        return error_text[:500]
    return (
        "post_register_verification_no_refresh_success"
        f"(refreshed={int(refresh_result.get('refreshed') or 0)},"
        f"relogined={int(refresh_result.get('relogined') or 0)},"
        f"errors={len(errors)})"
    )


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _diagnostic_safe_account(result: dict[str, Any]) -> dict[str, Any]:
    access_token = str(result.get("access_token") or "").strip()
    refresh_token = str(result.get("refresh_token") or "").strip()
    id_token = str(result.get("id_token") or "").strip()
    payload = _jwt_payload(access_token)
    return {
        "email_domain": str(result.get("email") or "").partition("@")[2].lower(),
        "access_token": anonymize_token(access_token),
        "refresh_token": anonymize_token(refresh_token) if refresh_token else "",
        "id_token": anonymize_token(id_token) if id_token else "",
        "has_refresh_token": bool(refresh_token),
        "has_id_token": bool(id_token),
        "jwt_aud": payload.get("aud"),
        "jwt_client_id": payload.get("azp") or payload.get("client_id"),
        "jwt_scope": payload.get("scope"),
        "jwt_iat": payload.get("iat"),
        "jwt_exp": payload.get("exp"),
        "source_type": result.get("source_type"),
        "created_at": result.get("created_at"),
    }


def _record_post_register_diagnostic(
    *,
    index: int,
    result: dict[str, Any],
    refresh_result: dict[str, Any],
    verification_failed: str,
    elapsed_seconds: float,
) -> None:
    payload = {
        "time": datetime.now(timezone.utc).isoformat(),
        "index": index,
        "verification_failed": verification_failed,
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "account": _diagnostic_safe_account(result),
        "refresh_summary": {
            "refreshed": int(refresh_result.get("refreshed") or 0) if isinstance(refresh_result, dict) else 0,
            "relogined": int(refresh_result.get("relogined") or 0) if isinstance(refresh_result, dict) else 0,
            "errors": [
                {
                    "token": str((item or {}).get("token") or "") if isinstance(item, dict) else "",
                    "error": str((item or {}).get("error") if isinstance(item, dict) else item)[:500],
                }
                for item in (refresh_result.get("errors") if isinstance(refresh_result, dict) and isinstance(refresh_result.get("errors"), list) else [])
            ],
        },
    }
    try:
        register_post_verify_diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
        with register_post_verify_diagnostics_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    final_url = str(getattr(resp, "url", "") or "").lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox(_mail_config(), username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code(_mail_config(), mailbox)


from utils.sentinel import SentinelTokenGenerator, build_sentinel_token as _build_sentinel_token_tuple  # noqa: F401


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    sentinel_val, _oai_sc_val = _build_sentinel_token_tuple(session, device_id, flow, user_agent=user_agent, sec_ch_ua=sec_ch_ua)
    return sentinel_val


def create_session(proxy: str = "") -> Any:
    kwargs = proxy_settings.build_session_kwargs(
        proxy=proxy,
        upstream=True,
        impersonate="chrome",
        verify=False,
    )
    return requests.Session(**kwargs)


def _apply_clearance_to_session(session: requests.Session, bundle: ClearanceBundle | None) -> None:
    if bundle is None:
        return
    if bundle.user_agent:
        session.headers["User-Agent"] = bundle.user_agent
        session.headers["user-agent"] = bundle.user_agent
    for name, value in bundle.cookies.items():
        try:
            session.cookies.set(name, value, domain=f".{bundle.target_host or 'openai.com'}")
            session.cookies.set(name, value, domain=bundle.target_host or "auth.openai.com")
        except Exception:
            continue


def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
) -> dict[str, str]:
    merged = proxy_settings.build_headers(
        headers=headers,
        target_url=target_url,
        proxy=proxy,
        upstream=True,
    )
    normalized = {str(key): str(value) for key, value in merged.items()}
    if user_agent_override:
        ua_key = next((key for key in normalized if key.lower() == "user-agent"), "user-agent")
        normalized[ua_key] = user_agent_override
    return normalized


def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def request_platform_oauth_token(session: requests.Session, code: str, code_verifier: str) -> dict | None:
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": user_agent,
    }
    try:
        resp = session.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
            },
            verify=False,
            timeout=60,
        )
    except Exception as exc:
        raise RuntimeError(f"token换取请求异常: {_redact_response_preview(exc, 500)}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"token换取失败: HTTP {resp.status_code}, {_response_debug_detail(resp, 500)}")
    data = _response_json(resp)
    if not data.get("access_token"):
        raise RuntimeError(f"token换取响应缺少 access_token: {_response_debug_detail(resp, 500)}")
    return data


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.session = create_session(self.proxy)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers["oai-session-id"] = self.session_id
        headers.update(_make_trace_headers())
        return headers

    def _account_fingerprint(self) -> dict[str, str]:
        actual_user_agent = self.clearance_user_agent or user_agent
        return {
            "user-agent": actual_user_agent,
            "impersonate": "chrome",
            "oai-device-id": self.device_id,
            "oai-session-id": self.session_id,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-arch": '"x86_64"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": '"145.0.0.0"',
            "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
        }

    def _refresh_cloudflare_clearance(self, target_url: str, index: int) -> ClearanceBundle | None:
        self.clearance_failure_reason = ""
        profile = proxy_settings.get_profile(proxy=self.proxy, upstream=True)
        if not profile.clearance_enabled:
            self.clearance_failure_reason = (
                "可尝试使用 FlareSolverr 清障方式，注意需要 Docker 部署 flaresolverr、privoxy、warp-proxy 等相关容器"
            )
            step(index, f"检测到 Cloudflare 拦截，{self.clearance_failure_reason}", "yellow")
            return None
        step(index, "检测到 Cloudflare 拦截，尝试刷新 clearance", "yellow")
        bundle = proxy_settings.refresh_clearance(
            target_url=target_url,
            proxy=self.proxy,
            force=True,
            upstream=True,
        )
        if bundle is not None:
            _apply_clearance_to_session(self.session, bundle)
            self.clearance_user_agent = bundle.user_agent or self.clearance_user_agent
            step(index, "Cloudflare clearance 刷新完成，重试当前请求", "yellow")
        else:
            self.clearance_failure_reason = "clearance 刷新未返回可用 Cookie，请检查 FlareSolverr URL、代理和出口 IP"
            step(index, f"Cloudflare clearance 刷新失败：{self.clearance_failure_reason}", "yellow")
        return bundle

    def _platform_authorize(self, email: str, index: int) -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            # 注册流程显式声明 signup：throwaway 域名 OpenAI 会自动当新账号走注册，
            # 但 @outlook.com/@hotmail.com 这类真实消费邮箱会被 login_or_signup 路由到登录分支，
            # 后续 user/register 落在错误的 auth step 上报 invalid_auth_step。
            "screen_hint": "signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            retry_headers = _headers_with_clearance(self._navigate_headers(f"{platform_base}/"), target_url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=retry_headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        landed = _authorize_landed_page(resp)
        # 仅打日志，不据此中断：authorize 落地页无法可靠区分注册/登录，
        # 真正的判定交给 user/register（失败会 dump 完整响应）。
        step(index, f"platform authorize 完成[{landed or '?'}] url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        url = f"{auth_base}/api/accounts/email-otp/send"
        headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        callback_params = extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        step(index, "创建账号资料完成")

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier)
        if not tokens:
            raise RuntimeError("token换取失败: empty token response")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            password = _random_password()
            first_name, last_name = _random_name()
            self._platform_authorize(email, index)
            self._register_user(email, password, index)
            self._send_otp(index)
            step(index, "开始等待注册验证码")
            code = wait_for_code(mailbox)
            if not code:
                raise RuntimeError("等待注册验证码超时")
            step(index, f"收到注册验证码: {code}")
            self._validate_otp(code, index)
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            tokens = self._exchange_registered_tokens(index)
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "source_type": "web",
            "fp": self._account_fingerprint(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int) -> dict:
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_account_items([result])
        step(index, "开始注册后验号")
        refresh_result = account_service.refresh_accounts([access_token], defer_invalid_removal=False)
        errors_text = _refresh_errors_text(refresh_result)
        if refresh_result.get("errors") or int(refresh_result.get("refreshed") or 0) <= 0:
            account_service.delete_accounts([access_token], include_items=False)
            if _is_invalid_refresh_error(errors_text):
                _record_post_register_diagnostic(
                    index=index,
                    result=result,
                    refresh_result=refresh_result,
                    verification_failed="invalid",
                    elapsed_seconds=cost,
                )
                with stats_lock:
                    stats["done"] += 1
                    stats["fail"] += 1
                step(index, f"注册后立即验号失败，已删除本地账号，不计成功: {errors_text or 'refresh_failed'}", "red")
                return {
                    "ok": False,
                    "verification_failed": "invalid",
                    "index": index,
                    "error": errors_text or "post_register_verification_failed",
                }
            _record_post_register_diagnostic(
                index=index,
                result=result,
                refresh_result=refresh_result,
                verification_failed="transient",
                elapsed_seconds=cost,
            )
            with stats_lock:
                stats["done"] += 1
                stats["fail"] += 1
            safe_message = _refresh_result_safe_message(refresh_result)
            step(index, f"注册后验号遇到网络/代理瞬断，计入失败并已删除本地临时账号，不污染号池: {safe_message}", "yellow")
            return {
                "ok": False,
                "transient": True,
                "verification_failed": "transient",
                "index": index,
                "error": safe_message,
            }
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        _record_post_register_diagnostic(
            index=index,
            result=result,
            refresh_result=refresh_result,
            verification_failed="ok",
            elapsed_seconds=cost,
        )
        try:
            from services.panda_staging_service import panda_staging_service

            panda_staging_service.stage_account(result, source="register_worker")
        except Exception:
            pass
        log(
            f'{result["email"]} 注册并验号成功，已进入本地 1h/3h/6h 成熟度探活队列，'
            f"本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s",
            "green",
        )
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        if is_transient_register_error(e):
            with stats_lock:
                stats["done"] += 1
                stats["fail"] += 1
            log(f"任务{index} 遇到网络/代理瞬断，本次计入失败，耗时{cost:.1f}s，原因: {e}", "yellow")
            return {"ok": False, "transient": True, "index": index, "error": str(e)}
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
