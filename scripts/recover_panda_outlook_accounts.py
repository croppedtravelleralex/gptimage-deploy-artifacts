from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

OUTLOOK_ROOTS = {"outlook.com", "hotmail.com", "live.com"}
ABNORMAL_STATUSES = {"异常"}
REJECTED_RECEIVE_STATES = {"rejected", "tainted"}
FAILURE_FIELDS = (
    "last_refresh_error",
    "last_token_refresh_error",
    "last_quota_refresh_error",
    "panda_verify_last_error",
)
INVALID_ERROR_MARKERS = (
    "token invalidated",
    "refresh_token_invalidated",
    "invalid access token",
    "invalid_access_token",
    "session has ended",
    "unauthorized",
    "http 401",
    "oauth_refresh_http_401",
    "account_deactivated",
)
TERMINAL_RECOVERY_REASONS = {"account_deactivated"}
TERMINAL_RECOVERY_MESSAGES = {
    "account_deactivated": "OpenAI 账号已删除或停用，无法自动恢复；请先通过 OpenAI Help Center 确认账号状态",
}
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OUTLOOK_RECEIVED_AT_CLOCK_TOLERANCE_SEC = 45


def outlook_code_not_before(
    now: datetime | None = None,
    *,
    tolerate_clock_skew: bool,
) -> datetime:
    value = now or datetime.now(timezone.utc)
    if tolerate_clock_skew:
        value -= timedelta(seconds=OUTLOOK_RECEIVED_AT_CLOCK_TOLERANCE_SEC)
    return value
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{16,}(?:\.[A-Za-z0-9_-]{8,}){1,2}")
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|authorization|password|client_secret)\b"
    r"(\s*[:=]\s*)([^\s,;}'\"]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
PROXY_AUTH_PATTERN = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")


@dataclass(frozen=True)
class ProxySpec:
    url: str
    label: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def mask_email(value: object) -> str:
    email = normalize_email(value)
    local, separator, domain = email.partition("@")
    if not separator:
        return "<unknown>"
    if len(local) <= 3:
        masked_local = f"{local[:1]}***"
    else:
        masked_local = f"{local[:2]}***{local[-1:]}"
    return f"{masked_local}@{domain}"


def token_hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def email_root(value: object) -> str:
    domain = normalize_email(value).rsplit("@", 1)[-1]
    parts = [part for part in domain.split(".") if part]
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def is_outlook_account(account: dict[str, Any]) -> bool:
    return email_root(account.get("email")) in OUTLOOK_ROOTS


def is_terminal_recovery_account(account: dict[str, Any]) -> bool:
    state = str(account.get("outlook_recovery_state") or "").strip().lower()
    reason = str(account.get("outlook_recovery_terminal_reason") or "").strip().lower()
    return state == "terminal" or reason in TERMINAL_RECOVERY_REASONS


def has_failure_evidence(account: dict[str, Any]) -> bool:
    if str(account.get("status") or "").strip() in ABNORMAL_STATUSES:
        return True
    if str(account.get("panda_receive_state") or "").strip().lower() in REJECTED_RECEIVE_STATES:
        return True
    if int(account.get("invalid_count") or 0) > 0:
        return True
    failure_kind = str(account.get("quota_refresh_failure_kind") or "").strip().lower()
    if failure_kind == "invalid":
        return True
    for field in FAILURE_FIELDS:
        error = str(account.get(field) or "").strip().lower()
        if error and any(marker in error for marker in INVALID_ERROR_MARKERS):
            return True
    return False


def select_recovery_targets(
    accounts: Iterable[dict[str, Any]],
    *,
    credential_emails: set[str],
    target_emails: set[str] | None = None,
    limit: int = 0,
    allow_account_password: bool = False,
) -> list[dict[str, Any]]:
    normalized_credentials = {normalize_email(item) for item in credential_emails if normalize_email(item)}
    explicit_targets = {normalize_email(item) for item in (target_emails or set()) if normalize_email(item)}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_account in accounts:
        account = dict(raw_account or {})
        email = normalize_email(account.get("email"))
        token = str(account.get("access_token") or "").strip()
        if not email or not token or email in seen or not is_outlook_account(account):
            continue
        if is_terminal_recovery_account(account):
            continue
        has_password = bool(str(account.get("password") or "").strip())
        in_credentials = email in normalized_credentials
        if not in_credentials and not (allow_account_password and has_password):
            continue
        if explicit_targets:
            if email not in explicit_targets:
                continue
        elif not has_failure_evidence(account):
            continue
        account["email"] = email
        selected.append(account)
        seen.add(email)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


def parse_proxy_line(value: str) -> ProxySpec:
    line = str(value or "").strip()
    if not line:
        raise ValueError("empty proxy line")

    if "://" in line:
        parsed = urlsplit(line)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("unsupported proxy URL")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        auth = ""
        if username or password:
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        host = parsed.hostname
        netloc = f"{auth}{host}:{parsed.port}"
        return ProxySpec(url=urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)), label=f"{host}:{parsed.port}")

    parts = line.split(":", 3)
    if len(parts) != 4:
        raise ValueError("proxy must be host:port:user:password or a HTTP URL")
    host, port_text, username, password = [part.strip() for part in parts]
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("proxy port must be an integer") from exc
    if not host or not username or not password or not (1 <= port <= 65535):
        raise ValueError("invalid Webshare proxy fields")
    url = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return ProxySpec(url=url, label=f"{host}:{port}")


def proxy_label_from_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.hostname and parsed.port:
            return f"{parsed.hostname}:{parsed.port}"
    except ValueError:
        pass
    return "<configured>"


def load_proxy_specs(path: Path | None) -> list[ProxySpec]:
    if path is None:
        return []
    specs: list[ProxySpec] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig", errors="ignore").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            spec = parse_proxy_line(line)
        except ValueError as exc:
            raise ValueError(f"invalid proxy at line {line_number}: {exc}") from exc
        if spec.url in seen:
            continue
        seen.add(spec.url)
        specs.append(spec)
    return specs


def sanitize_error(value: object, limit: int = 320) -> str:
    text = str(value or "")
    text = PROXY_AUTH_PATTERN.sub(r"\1<redacted>@", text)
    text = BEARER_PATTERN.sub("Bearer <redacted>", text)
    text = JWT_PATTERN.sub("<redacted>", text)
    text = SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    text = EMAIL_PATTERN.sub("<redacted-email>", text)
    return text[: max(1, int(limit))]


def build_staged_account(
    old_account: dict[str, Any],
    login_result: dict[str, Any],
    assigned_proxy: str,
    now: str,
) -> dict[str, Any]:
    item = {
        "email": normalize_email(old_account.get("email")),
        "password": str(old_account.get("password") or ""),
        "access_token": str(login_result.get("access_token") or "").strip(),
        "refresh_token": str(login_result.get("refresh_token") or "").strip(),
        "id_token": str(login_result.get("id_token") or "").strip(),
        "chatgpt_session_token": str(login_result.get("chatgpt_session_token") or "").strip(),
        "chatgpt_session_expires": login_result.get("chatgpt_session_expires"),
        "expires_at": login_result.get("expires_at"),
        "source_type": "web",
        "source_detail": "panda_outlook_manual_recovery",
        "created_at": now,
        "updated_at": now,
        "proxy": assigned_proxy,
        # 新 token 在 Panda Webshare /backend-api/me 验证前保持隔离。
        "status": "异常",
        "quota": 0,
        "image_quota_unknown": False,
        "panda_receive_state": "incoming",
        "panda_sync_state": "incoming",
        "panda_imported_at": now,
        "panda_verified_at": None,
        "panda_ready_at": None,
        "panda_rejected_at": None,
        "invalid_count": 0,
        "last_invalid_at": None,
        "last_refresh_error": None,
        "last_refresh_error_at": None,
        "last_token_refresh_error": None,
        "last_token_refresh_error_at": None,
        "last_quota_refresh_error": None,
        "last_quota_refresh_error_at": None,
        "quota_refresh_failure_kind": None,
        "quota_refresh_fail_count": 0,
        "quota_refresh_quarantined_at": None,
        "panda_verify_last_error": None,
        "panda_probe_last_error": None,
    }
    login_fp = login_result.get("fp")
    if isinstance(login_fp, dict) and login_fp:
        item["fp"] = {str(k).lower(): str(v) for k, v in login_fp.items() if str(v or "").strip()}
    else:
        from services.account_fingerprint import build_aligned_chrome_fp

        item["fp"] = build_aligned_chrome_fp()
    return item


def build_verified_updates(now: str) -> dict[str, Any]:
    return {
        "source_detail": "panda_outlook_manual_recovery",
        "updated_at": now,
        "invalid_count": 0,
        "last_invalid_at": None,
        "last_refresh_error": None,
        "last_refresh_error_at": None,
        "last_token_refresh_error": None,
        "last_token_refresh_error_at": None,
        "last_quota_refresh_error": None,
        "last_quota_refresh_error_at": None,
        "quota_refresh_failure_kind": None,
        "quota_refresh_fail_count": 0,
        "quota_refresh_quarantined_at": None,
        "panda_verify_last_error": None,
        "panda_probe_last_error": None,
        "panda_receive_state": "verified_ready",
        "panda_sync_state": "ready",
        "panda_verified_at": now,
        "panda_ready_at": now,
        "panda_probe_last_at": now,
        "panda_rejected_at": None,
    }


def mark_terminal_account(
    account_service: Any,
    access_token: str,
    reason: str,
    *,
    now: str | None = None,
) -> dict[str, Any] | None:
    normalized_reason = str(reason or "").strip().lower()
    if normalized_reason not in TERMINAL_RECOVERY_REASONS:
        return None
    timestamp = now or utc_now_iso()
    message = "OpenAI 账号已删除或停用，无法自动恢复"
    return account_service.update_account(
        access_token,
        {
            "status": "禁用",
            "quota": 0,
            "image_quota_unknown": False,
            "panda_receive_state": "rejected",
            "panda_rejected_at": timestamp,
            "panda_verify_last_error": normalized_reason,
            "last_refresh_error": normalized_reason,
            "last_refresh_error_at": timestamp,
            "quota_refresh_failure_kind": "invalid",
            "outlook_recovery_state": "terminal",
            "outlook_recovery_terminal_reason": normalized_reason,
            "outlook_recovery_terminal_at": timestamp,
            "outlook_recovery_last_attempt_at": timestamp,
            "outlook_recovery_last_error": message,
            "updated_at": timestamp,
        },
        quiet=True,
    )


def read_target_emails(path: Path | None, inline_values: Sequence[str]) -> set[str]:
    values = list(inline_values)
    if path is not None:
        values.extend(path.read_text(encoding="utf-8-sig", errors="ignore").splitlines())
    return {normalize_email(value) for value in values if normalize_email(value)}


def create_sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source))
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def create_backup(root: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    accounts_db = root / "data" / "accounts.db"
    if not accounts_db.exists():
        raise FileNotFoundError(f"accounts database not found: {accounts_db}")
    create_sqlite_backup(accounts_db, backup_dir / "accounts.db")
    for name in ("config.json", "docker-compose.panda.yml"):
        source = root / name
        if source.exists():
            shutil.copy2(source, backup_dir / name)
    manifest = {
        "created_at": utc_now_iso(),
        "source": str(root),
        "files": sorted(item.name for item in backup_dir.iterdir()),
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in backup_dir.iterdir():
        if item.is_file():
            try:
                os.chmod(item, 0o600)
            except OSError:
                pass


def build_mail_config(
    account_service: Any,
    credential: dict[str, str],
    timeout: int,
    interval: int,
    proxy_url: str = "",
) -> dict[str, Any]:
    email = str(credential.get("email") or "").strip().lower()
    domain = email.split("@", 1)[-1] if "@" in email else ""
    # 消费级域名优先走 outlook.live.com；office365 主机常出现 authenticated-but-not-connected。
    imap_host = (
        "outlook.live.com"
        if domain in {"outlook.com", "hotmail.com", "live.com"}
        else "outlook.office365.com"
    )
    return {
        "request_timeout": 30,
        "wait_timeout": timeout,
        "wait_interval": interval,
        "user_agent": account_service._OAUTH_USER_AGENT,
        "api_use_register_proxy": bool(str(proxy_url or "").strip()),
        "proxy": str(proxy_url or "").strip(),
        "providers": [
            {
                "type": "outlook_token",
                "enable": True,
                "label": "PandaOutlookManualRecovery",
                "mode": "auto",
                "imap_host": imap_host,
                "message_limit": 10,
                "mailboxes": [credential],
            }
        ],
    }


def preflight_mailbox_access(
    mail_config: dict[str, Any],
    mailbox: dict[str, Any],
) -> dict[str, Any]:
    """恢复前先验证邮箱可读，避免 ChatGPT 登录已发出 OTP 后才发现 Graph/IMAP 都挂。"""
    from services import outlook_mail as mail_provider

    provider = mail_provider._create_provider(
        mail_config,
        str(mailbox.get("provider") or ""),
        str(mailbox.get("provider_ref") or ""),
    )
    try:
        fetch_recent = getattr(provider, "fetch_recent_messages", None)
        if not callable(fetch_recent):
            raise RuntimeError("Outlook mailbox provider missing fetch_recent_messages")
        messages = fetch_recent(mailbox)
        return {
            "ok": True,
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "imap_host": str(mailbox.get("_outlook_imap_host") or ""),
        }
    finally:
        provider.close()


def build_mailbox(credential: dict[str, str], boundary: datetime) -> dict[str, Any]:
    return {
        "provider": "outlook_token",
        "provider_ref": "outlook_token#1",
        "address": credential["email"],
        "label": "PandaOutlookManualRecovery",
        "client_id": credential["client_id"],
        "refresh_token": credential["refresh_token"],
        "_code_not_before": boundary,
    }


def generate_recovery_password() -> str:
    # 不含邮箱/姓名等个人信息；满足当前至少 12 位要求，并包含多类字符。
    return f"R9!{secrets.token_urlsafe(24)}"


def _login_error_code(result: dict[str, Any]) -> str:
    return _extract_error_code(result.get("detail") or result)


def _extract_error_code(value: object) -> str:
    """兼容 auth API 将 code 放在顶层或 detail.error 内的两种响应形态。"""
    if not isinstance(value, dict):
        return ""
    direct = str(value.get("code") or "").strip().lower()
    if direct:
        return direct
    for key in ("error", "detail", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            code = _extract_error_code(nested)
            if code:
                return code
    return ""


def _terminal_recovery_reason(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    explicit = str(result.get("terminal_reason") or "").strip().lower()
    if explicit in TERMINAL_RECOVERY_REASONS:
        return explicit
    code = _extract_error_code(result)
    return code if code in TERMINAL_RECOVERY_REASONS else ""


def _terminal_recovery_message(reason: str) -> str:
    return TERMINAL_RECOVERY_MESSAGES.get(
        str(reason or "").strip().lower(),
        "OpenAI 账号处于不可自动恢复的终态",
    )


def prime_mailbox_messages(mail_config: dict[str, Any], mailbox: dict[str, Any]) -> int:
    """把当前已有邮件标为已看，后续只消费本次登录新产生的 OTP。"""
    from services import outlook_mail as mail_provider

    provider = mail_provider._create_provider(
        mail_config,
        str(mailbox.get("provider") or ""),
        str(mailbox.get("provider_ref") or ""),
    )
    try:
        fetch_recent = getattr(provider, "fetch_recent_messages", None)
        if not callable(fetch_recent):
            return 0
        messages = fetch_recent(mailbox)
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen = {str(item) for item in seen_value}
        added = 0
        for message in messages:
            ref = mail_provider._message_tracking_ref(message)
            if ref not in seen:
                seen_value.append(ref)
                seen.add(ref)
                added += 1
        return added
    finally:
        provider.close()


def login_with_password_retries(
    *,
    account_service: Any,
    email: str,
    password: str,
    mail_config: dict[str, Any],
    mailbox: dict[str, Any],
    wait_for_code: Callable[[dict[str, Any], dict[str, Any]], str | None],
    max_attempts: int = 3,
    prime_mailbox: Callable[[dict[str, Any], dict[str, Any]], int] | None = None,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {"ok": False, "error": "login_not_attempted"}
    attempts = max(1, max_attempts)
    original_wait_timeout = int(mail_config.get("wait_timeout") or 180)
    for attempt in range(1, attempts + 1):
        mail_config["wait_timeout"] = original_wait_timeout if attempt == 1 else min(original_wait_timeout, 25)
        if prime_mailbox is not None:
            prime_mailbox(mail_config, mailbox)
        mailbox["_code_not_before"] = outlook_code_not_before(
            tolerate_clock_skew=prime_mailbox is not None
        )

        def resolve_otp() -> str | None:
            return wait_for_code(mail_config, mailbox)

        result = account_service._login_with_password(email, password, otp_resolver=resolve_otp)
        if result.get("ok"):
            mail_config["wait_timeout"] = original_wait_timeout
            return result, attempt
        error = str(result.get("error") or "").strip().lower()
        error_code = _login_error_code(result)
        retryable_otp_error = (
            error.startswith("otp_validate_failed_")
            and error_code in {"wrong_email_otp_code", "invalid_auth_step"}
        ) or error == "otp_code_timeout"
        if not retryable_otp_error:
            mail_config["wait_timeout"] = original_wait_timeout
            return result, attempt
    mail_config["wait_timeout"] = original_wait_timeout
    return result, attempts


def reset_openai_password(
    *,
    account_service: Any,
    email: str,
    new_password: str,
    mail_config: dict[str, Any],
    mailbox: dict[str, Any],
    wait_for_code: Callable[[dict[str, Any], dict[str, Any]], str | None],
    otp_attempts: int = 5,
    prime_mailbox: Callable[[dict[str, Any], dict[str, Any]], int] | None = None,
) -> dict[str, Any]:
    from curl_cffi import requests
    from services.proxy_service import proxy_settings
    from utils.pkce import generate_pkce
    from utils.sentinel import build_sentinel_token

    auth_base = "https://auth.openai.com"
    platform_base = "https://platform.openai.com"
    oauth_client_id = account_service._OAUTH_CLIENT_ID
    oauth_redirect_uri = f"{platform_base}/auth/callback"
    user_agent = account_service._OAUTH_USER_AGENT
    auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
    session = requests.Session(
        **proxy_settings.build_session_kwargs(
            impersonate="chrome110",
            verify=False,
            upstream=True,
        )
    )
    stage = "authorize"
    try:
        device_id = str(uuid.uuid4())
        code_verifier, code_challenge = generate_pkce()
        session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
        session.cookies.set("oai-did", device_id, domain="auth.openai.com")
        authorize_params = {
            "issuer": auth_base,
            "client_id": oauth_client_id,
            "audience": "https://api.openai.com/v1",
            "redirect_uri": oauth_redirect_uri,
            "device_id": device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": auth0_client,
        }
        authorize_response = session.get(
            f"{auth_base}/api/accounts/authorize?{urlencode(authorize_params)}",
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "user-agent": user_agent,
                "referer": f"{platform_base}/",
            },
            allow_redirects=True,
            timeout=30,
        )
        if authorize_response.status_code not in (200, 302):
            return {"ok": False, "stage": stage, "error": f"authorize_http_{authorize_response.status_code}"}

        stage = "send_reset_otp"
        if prime_mailbox is not None:
            prime_mailbox(mail_config, mailbox)
        mailbox["_code_not_before"] = outlook_code_not_before(
            tolerate_clock_skew=prime_mailbox is not None
        )
        send_response = session.post(
            f"{auth_base}/api/accounts/password/send-otp",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "referer": f"{auth_base}/reset-password",
                "user-agent": user_agent,
                "oai-device-id": device_id,
            },
            json={},
            timeout=30,
        )
        try:
            send_data = send_response.json() if send_response.text else {}
        except Exception:
            send_data = {}
        if send_response.status_code != 200:
            error_code = _extract_error_code(send_data)
            return {
                "ok": False,
                "stage": stage,
                "error_code": error_code,
                "terminal_reason": error_code if error_code in TERMINAL_RECOVERY_REASONS else "",
                "error": f"password_send_otp_http_{send_response.status_code}: {sanitize_error(send_data or send_response.text)}",
            }

        otp_headers = {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "origin": auth_base,
            "referer": f"{auth_base}/email-verification",
            "user-agent": user_agent,
            "oai-device-id": device_id,
        }
        try:
            sentinel_value, oai_sc_value = build_sentinel_token(session, device_id, "authorize_continue")
            otp_headers["OpenAI-Sentinel-Token"] = sentinel_value
            if oai_sc_value:
                session.cookies.set("oai-sc", oai_sc_value, domain=".openai.com")
        except Exception:
            pass

        otp_data: dict[str, Any] = {}
        validated = False
        used_attempts = 0
        original_wait_timeout = int(mail_config.get("wait_timeout") or 180)
        for used_attempts in range(1, max(1, otp_attempts) + 1):
            mail_config["wait_timeout"] = original_wait_timeout if used_attempts == 1 else min(original_wait_timeout, 25)
            stage = "wait_reset_otp"
            code = str(wait_for_code(mail_config, mailbox) or "").strip()
            if not code:
                break
            stage = "validate_reset_otp"
            otp_response = session.post(
                f"{auth_base}/api/accounts/email-otp/validate",
                headers=otp_headers,
                json={"code": code},
                timeout=30,
            )
            try:
                otp_data = otp_response.json() if otp_response.text else {}
            except Exception:
                otp_data = {}
            if otp_response.status_code == 200:
                validated = True
                break
            error = otp_data.get("error") if isinstance(otp_data, dict) else None
            error_code = _extract_error_code(otp_data)
            if error_code != "wrong_email_otp_code":
                return {
                    "ok": False,
                    "stage": stage,
                    "otp_attempts": used_attempts,
                    "error_code": error_code,
                    "terminal_reason": error_code if error_code in TERMINAL_RECOVERY_REASONS else "",
                    "error": f"reset_otp_http_{otp_response.status_code}: {sanitize_error(error or otp_data)}",
                }
        mail_config["wait_timeout"] = original_wait_timeout
        if not validated:
            return {"ok": False, "stage": stage, "otp_attempts": used_attempts, "error": "reset_otp_not_validated"}

        continue_url = str(otp_data.get("continue_url") or "").strip()
        if not continue_url:
            return {"ok": False, "stage": stage, "otp_attempts": used_attempts, "error": "reset_otp_has_no_continue_url"}
        if urlparse(continue_url).path != "/reset-password/new-password":
            return {
                "ok": False,
                "stage": stage,
                "otp_attempts": used_attempts,
                "error": f"unexpected_reset_continue_path:{urlparse(continue_url).path}",
            }

        stage = "open_new_password_page"
        page_response = session.get(
            continue_url,
            headers={"referer": f"{auth_base}/email-verification", "user-agent": user_agent},
            allow_redirects=True,
            timeout=30,
        )
        if page_response.status_code != 200:
            return {"ok": False, "stage": stage, "error": f"reset_page_http_{page_response.status_code}"}

        stage = "reset_password"
        reset_headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": auth_base,
            "referer": continue_url,
            "user-agent": user_agent,
            "oai-device-id": device_id,
        }
        try:
            sentinel_value, oai_sc_value = build_sentinel_token(session, device_id, "password_reset")
            reset_headers["OpenAI-Sentinel-Token"] = sentinel_value
            if oai_sc_value:
                session.cookies.set("oai-sc", oai_sc_value, domain=".openai.com")
        except Exception:
            pass
        reset_response = session.post(
            f"{auth_base}/api/accounts/password/reset",
            headers=reset_headers,
            json={"password": new_password},
            timeout=30,
        )
        try:
            reset_data = reset_response.json() if reset_response.text else {}
        except Exception:
            reset_data = {}
        if reset_response.status_code != 200:
            return {
                "ok": False,
                "stage": stage,
                "otp_attempts": used_attempts,
                "error": f"password_reset_http_{reset_response.status_code}: {sanitize_error(reset_data or reset_response.text)}",
            }

        reset_continue_url = str(reset_data.get("continue_url") or "").strip() if isinstance(reset_data, dict) else ""
        auth_code = str((parse_qs(urlparse(reset_continue_url).query).get("code") or [""])[0]).strip()
        if not auth_code:
            return {
                "ok": True,
                "stage": "password_reset_only",
                "otp_attempts": used_attempts,
                "response_keys": sorted(reset_data) if isinstance(reset_data, dict) else [],
            }

        stage = "exchange_reset_auth_code"
        token_response = session.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers={
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9",
                "auth0-client": auth0_client,
                "cache-control": "no-cache",
                "content-type": "application/json",
                "origin": platform_base,
                "pragma": "no-cache",
                "referer": f"{platform_base}/",
                "user-agent": user_agent,
            },
            json={
                "client_id": oauth_client_id,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": oauth_redirect_uri,
            },
            verify=False,
            timeout=60,
        )
        try:
            token_data = token_response.json() if token_response.text else {}
        except Exception:
            token_data = {}
        access_token = str(token_data.get("access_token") or "").strip() if isinstance(token_data, dict) else ""
        refresh_token = str(token_data.get("refresh_token") or "").strip() if isinstance(token_data, dict) else ""
        if token_response.status_code != 200 or not access_token or not refresh_token:
            return {
                "ok": True,
                "stage": "password_reset_only",
                "otp_attempts": used_attempts,
                "response_keys": sorted(reset_data) if isinstance(reset_data, dict) else [],
                "token_exchange_error": f"token_exchange_http_{token_response.status_code}: {sanitize_error(token_data)}",
            }
        jwt_payload = account_service._decode_jwt_payload(access_token)
        email_from_jwt = str(
            jwt_payload.get("https://api.openai.com/profile", {}).get("email") or email
        ).strip()
        login_result = {
            "ok": True,
            "email": email_from_jwt,
            "account_id": str(
                jwt_payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or ""
            ).strip(),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": str(token_data.get("id_token") or "").strip(),
            "expires_at": jwt_payload.get("exp"),
            "source_type": "password_reset",
        }
        return {
            "ok": True,
            "stage": "done",
            "otp_attempts": used_attempts,
            "response_keys": sorted(reset_data) if isinstance(reset_data, dict) else [],
            "login_result": login_result,
        }
    except Exception as exc:
        return {"ok": False, "stage": stage, "error": sanitize_error(f"{type(exc).__name__}: {exc}")}
    finally:
        session.close()


def login_with_chatgpt_email_otp(
    *,
    account_service: Any,
    email: str,
    mail_config: dict[str, Any],
    mailbox: dict[str, Any],
    wait_for_code: Callable[[dict[str, Any], dict[str, Any]], str | None],
    prime_mailbox: Callable[[dict[str, Any], dict[str, Any]], int] | None = None,
    otp_attempts: int = 5,
    proxy: str = "",
) -> dict[str, Any]:
    """走 ChatGPT 当前默认邮箱 OTP 登录，返回 access token 和 NextAuth session token。"""
    from curl_cffi import requests
    from services.account_fingerprint import build_aligned_chrome_fp
    from services.proxy_service import proxy_settings
    from utils.sentinel import build_sentinel_token

    chatgpt_base = "https://chatgpt.com"
    auth_base = "https://auth.openai.com"
    fp = build_aligned_chrome_fp()
    user_agent = fp["user-agent"]
    device_id = fp["oai-device-id"]
    session = requests.Session(
        **proxy_settings.build_session_kwargs(
            account={"proxy": str(proxy or "").strip()},
            impersonate=fp["impersonate"],
            verify=False,
            upstream=True,
        )
    )
    # NextAuth CSRF/signin 走 Panda/本机出口；authorize/OTP 仍走账号 sticky Webshare。
    nextauth_session = requests.Session(
        **proxy_settings.build_session_kwargs(
            account=None,
            proxy="",
            impersonate=fp["impersonate"],
            verify=False,
            upstream=False,
        )
    )
    stage = "csrf"
    original_wait_timeout = int(mail_config.get("wait_timeout") or 180)
    try:
        for auth_session in (nextauth_session, session):
            auth_session.cookies.set("oai-did", device_id, domain=".chatgpt.com")
            auth_session.cookies.set("oai-did", device_id, domain="chatgpt.com")
        csrf_response = nextauth_session.get(
            f"{chatgpt_base}/api/auth/csrf",
            headers={"accept": "application/json", "referer": f"{chatgpt_base}/", "user-agent": user_agent},
            timeout=30,
        )
        try:
            csrf_data = csrf_response.json() if csrf_response.text else {}
        except Exception:
            csrf_data = {}
        csrf_token = str(csrf_data.get("csrfToken") or "").strip() if isinstance(csrf_data, dict) else ""
        if csrf_response.status_code != 200 or not csrf_token:
            return {"ok": False, "stage": stage, "error": f"nextauth_csrf_http_{csrf_response.status_code}"}

        stage = "signin"
        query = {
            "prompt": "login",
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        signin_response = session.post(
            f"{chatgpt_base}/api/auth/signin/openai?{urlencode(query)}",
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": chatgpt_base,
                "referer": f"{chatgpt_base}/auth/login",
                "user-agent": user_agent,
                "x-auth-return-redirect": "1",
            },
            data={"csrfToken": csrf_token, "callbackUrl": f"{chatgpt_base}/", "json": "true"},
            timeout=30,
        )
        try:
            signin_data = signin_response.json() if signin_response.text else {}
        except Exception:
            signin_data = {}
        authorize_url = str(signin_data.get("url") or "").strip() if isinstance(signin_data, dict) else ""
        if signin_response.status_code != 200 or not authorize_url:
            return {
                "ok": False,
                "stage": stage,
                "error": f"nextauth_signin_http_{signin_response.status_code}: {sanitize_error(signin_data)}",
            }

        if prime_mailbox is not None:
            prime_mailbox(mail_config, mailbox)
        mailbox["_code_not_before"] = outlook_code_not_before(
            tolerate_clock_skew=prime_mailbox is not None
        )
        stage = "authorize"
        authorize_response = session.get(
            authorize_url,
            headers={"referer": f"{chatgpt_base}/", "user-agent": user_agent},
            allow_redirects=True,
            timeout=45,
        )
        if authorize_response.status_code not in (200, 302):
            return {"ok": False, "stage": stage, "error": f"nextauth_authorize_http_{authorize_response.status_code}"}

        otp_headers = {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "origin": auth_base,
            "referer": f"{auth_base}/email-verification",
            "user-agent": user_agent,
            "oai-device-id": device_id,
        }
        try:
            sentinel_value, oai_sc_value = build_sentinel_token(session, device_id, "authorize_continue")
            otp_headers["OpenAI-Sentinel-Token"] = sentinel_value
            if oai_sc_value:
                session.cookies.set("oai-sc", oai_sc_value, domain=".openai.com")
        except Exception:
            pass

        otp_data: dict[str, Any] = {}
        validated = False
        used_attempts = 0
        for used_attempts in range(1, max(1, otp_attempts) + 1):
            mail_config["wait_timeout"] = original_wait_timeout if used_attempts == 1 else min(original_wait_timeout, 25)
            stage = "wait_email_otp"
            code = str(wait_for_code(mail_config, mailbox) or "").strip()
            if not code:
                break
            stage = "validate_email_otp"
            otp_response = session.post(
                f"{auth_base}/api/accounts/email-otp/validate",
                headers=otp_headers,
                json={"code": code},
                timeout=30,
            )
            try:
                otp_data = otp_response.json() if otp_response.text else {}
            except Exception:
                otp_data = {}
            if otp_response.status_code == 200:
                validated = True
                break
            error = otp_data.get("error") if isinstance(otp_data, dict) else None
            error_code = _extract_error_code(otp_data)
            if error_code != "wrong_email_otp_code":
                terminal_reason = error_code if error_code in TERMINAL_RECOVERY_REASONS else ""
                return {
                    "ok": False,
                    "stage": stage,
                    "otp_attempts": used_attempts,
                    "error_code": error_code,
                    "terminal_reason": terminal_reason,
                    "error": f"nextauth_otp_http_{otp_response.status_code}: {sanitize_error(error or otp_data)}",
                }
        mail_config["wait_timeout"] = original_wait_timeout
        if not validated:
            return {"ok": False, "stage": stage, "otp_attempts": used_attempts, "error": "nextauth_otp_not_validated"}

        continue_url = str(otp_data.get("continue_url") or "").strip()
        if not continue_url:
            return {"ok": False, "stage": stage, "otp_attempts": used_attempts, "error": "nextauth_otp_has_no_continue_url"}
        stage = "callback"
        callback_response = session.get(
            continue_url,
            headers={"referer": f"{auth_base}/email-verification", "user-agent": user_agent},
            allow_redirects=True,
            timeout=60,
        )
        if callback_response.status_code != 200:
            return {"ok": False, "stage": stage, "error": f"nextauth_callback_http_{callback_response.status_code}"}

        stage = "session"
        session_response = session.get(
            f"{chatgpt_base}/api/auth/session",
            headers={"accept": "application/json", "referer": f"{chatgpt_base}/", "user-agent": user_agent},
            timeout=30,
        )
        try:
            session_data = session_response.json() if session_response.text else {}
        except Exception:
            session_data = {}
        access_token = str(session_data.get("accessToken") or "").strip() if isinstance(session_data, dict) else ""
        session_token = str(session_data.get("sessionToken") or "").strip() if isinstance(session_data, dict) else ""
        if session_response.status_code != 200 or not access_token:
            return {
                "ok": False,
                "stage": stage,
                "error": f"nextauth_session_http_{session_response.status_code}: access token missing",
            }
        jwt_payload = account_service._decode_jwt_payload(access_token)
        profile = jwt_payload.get("https://api.openai.com/profile", {})
        email_from_jwt = str(profile.get("email") or email).strip() if isinstance(profile, dict) else email
        auth_payload = jwt_payload.get("https://api.openai.com/auth", {})
        account_id = str(auth_payload.get("chatgpt_account_id") or "").strip() if isinstance(auth_payload, dict) else ""
        return {
            "ok": True,
            "stage": "done",
            "otp_attempts": used_attempts,
            "email": email_from_jwt,
            "account_id": account_id,
            "access_token": access_token,
            "refresh_token": "",
            "id_token": "",
            "expires_at": jwt_payload.get("exp"),
            "chatgpt_session_token": session_token,
            "chatgpt_session_expires": session_data.get("expires") if isinstance(session_data, dict) else None,
            "source_type": "email_otp",
            "fp": dict(fp),
        }
    except Exception as exc:
        return {"ok": False, "stage": stage, "error": sanitize_error(f"{type(exc).__name__}: {exc}")}
    finally:
        mail_config["wait_timeout"] = original_wait_timeout
        session.close()


def validate_webshare_proxy(spec: ProxySpec, timeout: int) -> dict[str, Any]:
    from services.proxy_health import validate_http_proxy

    result = validate_http_proxy(spec.url, timeout=float(timeout), require_sticky=True, sticky_gap_sec=2.0)
    if "error" in result and result.get("error"):
        result["error"] = sanitize_error(result["error"])
    return result


def choose_working_proxy(
    candidates: Sequence[ProxySpec],
    *,
    start_index: int,
    max_attempts: int,
    timeout: int,
    validator: Callable[[ProxySpec, int], dict[str, Any]] = validate_webshare_proxy,
) -> tuple[ProxySpec | None, list[dict[str, Any]]]:
    if not candidates:
        return None, []
    attempts: list[dict[str, Any]] = []
    count = min(len(candidates), max(1, max_attempts))
    for offset in range(count):
        spec = candidates[(start_index + offset) % len(candidates)]
        result = dict(validator(spec, timeout))
        result["proxy"] = spec.label
        attempts.append(result)
        if result.get("ok"):
            return spec, attempts
    return None, attempts


def write_report(report_dir: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def emit_progress(index: int, email: str, stage: str, **extra: Any) -> None:
    payload = {"event": "recovery_progress", "index": index, "email": mask_email(email), "stage": stage}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def recover_one_account(
    *,
    index: int,
    account: dict[str, Any],
    credential: dict[str, str] | None,
    proxy_specs: Sequence[ProxySpec],
    account_service: Any,
    wait_for_code: Callable[[dict[str, Any], dict[str, Any]], str | None],
    otp_timeout: int,
    otp_interval: int,
    proxy_attempts: int,
    proxy_timeout: int,
    activate_login_proxy: Callable[[str], None] | None = None,
    prime_mailbox: Callable[[dict[str, Any], dict[str, Any]], int] | None = None,
    allow_password_reset: bool = False,
    prefer_yumail_otp: bool = False,
    skip_mailbox_if_missing: bool = False,
) -> dict[str, Any]:
    email = normalize_email(account.get("email"))
    old_token = str(account.get("access_token") or "").strip()
    row: dict[str, Any] = {
        "index": index,
        "email": mask_email(email),
        "old_token_hash": token_hash(old_token),
        "old_quota": int(account.get("quota") or 0),
        "stage": "starting",
        "ok": False,
    }
    started = time.time()
    new_token = ""
    resolved_new_token = ""
    new_existed_before = False

    emit_progress(index, email, "proxy_preflight")
    candidates = list(proxy_specs)
    existing_proxy = str(account.get("proxy") or "").strip()
    if existing_proxy:
        sticky = ProxySpec(existing_proxy, proxy_label_from_url(existing_proxy))
        candidates = [sticky] + [spec for spec in candidates if spec.url != sticky.url]
    proxy, proxy_checks = choose_working_proxy(
        candidates,
        start_index=index - 1,
        max_attempts=proxy_attempts,
        timeout=proxy_timeout,
    )
    row["proxy_checks"] = proxy_checks
    if proxy is None:
        row.update({"stage": "proxy_preflight", "error": "no Webshare proxy passed ChatGPT csrf validation"})
        row["elapsed_sec"] = round(time.time() - started, 1)
        return row
    row["proxy"] = proxy.label
    if activate_login_proxy is not None:
        activate_login_proxy(proxy.url)

    openai_password = str(account.get("password") or "").strip()
    has_mailbox_credential = bool(
        credential
        and str(credential.get("client_id") or "").strip()
        and str(credential.get("refresh_token") or "").strip()
    )
    use_yumail = False
    if prefer_yumail_otp:
        try:
            from services import yumail_otp as yumail_otp_mod

            if not yumail_otp_mod.is_configured():
                if has_mailbox_credential:
                    row["yumail_skipped_reason"] = "yumail_not_configured"
                    row["otp_fallback"] = "graph"
                else:
                    row.update(
                        {
                            "stage": "mailbox_preflight",
                            "error": "yumail_not_configured: prefer_yumail_otp 但未配置 YUMAIL_API_KEY",
                        }
                    )
                    row["elapsed_sec"] = round(time.time() - started, 1)
                    return row
            else:
                probe = yumail_otp_mod.probe_reachable()
                if probe.get("ok"):
                    use_yumail = True
                elif has_mailbox_credential:
                    row["yumail_skipped_reason"] = str(probe.get("error") or "yumail_unreachable")
                    row["otp_fallback"] = "graph"
                else:
                    row.update(
                        {
                            "stage": "mailbox_preflight",
                            "error": f"yumail_unreachable: {probe.get('error') or 'probe failed'}",
                        }
                    )
                    row["elapsed_sec"] = round(time.time() - started, 1)
                    return row
        except Exception as exc:  # noqa: BLE001
            if has_mailbox_credential:
                row["yumail_skipped_reason"] = str(exc)[:240]
                row["otp_fallback"] = "graph"
            else:
                row.update(
                    {
                        "stage": "mailbox_preflight",
                        "error": sanitize_error(f"yumail_unreachable: {exc}", limit=400),
                    }
                )
                row["elapsed_sec"] = round(time.time() - started, 1)
                return row

    mailbox: dict[str, Any] | None = None
    mail_config: dict[str, Any] | None = None
    if has_mailbox_credential and credential is not None:
        login_credential = dict(credential)
        login_credential["email"] = email
        login_credential["password"] = openai_password
        mailbox = build_mailbox(login_credential, datetime.now(timezone.utc))
        mail_config = build_mail_config(
            account_service,
            login_credential,
            otp_timeout,
            otp_interval,
            proxy_url=proxy.url,
        )

    try:
        if has_mailbox_credential and mailbox is not None and mail_config is not None and not use_yumail:
            row["stage"] = "mailbox_preflight"
            emit_progress(index, email, "mailbox_preflight")
            try:
                preflight = preflight_mailbox_access(mail_config, mailbox)
                row["mailbox_preflight"] = preflight
                if preflight.get("imap_host"):
                    row["mailbox_imap_host"] = preflight.get("imap_host")
                    mailbox["_outlook_prefer_imap"] = True
                    mailbox["_outlook_imap_host"] = str(preflight.get("imap_host") or "")
                    for provider in mail_config.get("providers") or []:
                        if not isinstance(provider, dict):
                            continue
                        if str(provider.get("type") or "").strip() != "outlook_token":
                            continue
                        provider["mode"] = "imap"
                        provider["imap_host"] = str(
                            preflight.get("imap_host") or provider.get("imap_host") or "outlook.live.com"
                        )
            except Exception as exc:
                row.update(
                    {
                        "stage": "mailbox_preflight",
                        "error": sanitize_error(
                            f"mailbox_preflight: {exc}. "
                            "Graph Mail.Read 可能缺同意，且 IMAP 主机均不可读；请换带 Mail.Read 的 refresh_token 或确认 IMAP 可用",
                            limit=800,
                        ),
                    }
                )
                row["elapsed_sec"] = round(time.time() - started, 1)
                return row
        elif not has_mailbox_credential:
            if use_yumail or skip_mailbox_if_missing:
                row["mailbox_preflight_skipped"] = True
                if not use_yumail:
                    row.update(
                        {
                            "stage": "mailbox_preflight",
                            "error": "need_outlook_mailbox_credentials_or_yumail: skip_mailbox_if_missing 但仍无 YuMail OTP",
                        }
                    )
                    row["elapsed_sec"] = round(time.time() - started, 1)
                    return row
            else:
                row.update(
                    {
                        "stage": "mailbox_preflight",
                        "error": "need_outlook_mailbox_credentials_or_yumail: 无邮箱凭据且未配置 YuMail OTP",
                    }
                )
                row["elapsed_sec"] = round(time.time() - started, 1)
                return row

        def resolve_otp_code(_mail_config: dict[str, Any], _mailbox: dict[str, Any]) -> str | None:
            if use_yumail:
                from services import yumail_otp as yumail_otp_mod

                boundary = _mailbox.get("_code_not_before") if isinstance(_mailbox, dict) else None
                try:
                    return yumail_otp_mod.wait_for_code_by_email(
                        email,
                        not_before=boundary if isinstance(boundary, datetime) else datetime.now(timezone.utc),
                        timeout_sec=float(otp_timeout),
                        poll_interval=float(otp_interval),
                    )
                except RuntimeError as exc:
                    text = str(exc)
                    if "timeout" in text.lower():
                        return None
                    # YuMail 运行中失败且有 Graph 凭据：回退一次
                    if has_mailbox_credential and mailbox is not None and mail_config is not None:
                        row["otp_fallback"] = "graph"
                        row["yumail_runtime_error"] = text[:240]
                        return wait_for_code(_mail_config, _mailbox)
                    raise
            return wait_for_code(_mail_config, _mailbox)

        otp_mailbox = mailbox or {
            "provider": "yumail",
            "address": email,
            "email": email,
            "_code_not_before": datetime.now(timezone.utc),
        }
        otp_mail_config = mail_config or {
            "request_timeout": 30,
            "wait_timeout": otp_timeout,
            "wait_interval": otp_interval,
            "providers": [{"type": "yumail", "enable": True}],
        }

        row["stage"] = "login"
        emit_progress(index, email, "login")
        if openai_password:
            login_result, login_attempts = login_with_password_retries(
                account_service=account_service,
                email=email,
                password=openai_password,
                mail_config=otp_mail_config,
                mailbox=otp_mailbox,
                wait_for_code=resolve_otp_code,
                prime_mailbox=None if use_yumail else prime_mailbox,
            )
        else:
            login_result, login_attempts = ({"ok": False, "error": "missing_openai_password"}, 0)
        row["login_attempts"] = login_attempts
        row["otp_via_yumail"] = bool(use_yumail)
        terminal_reason = _terminal_recovery_reason(login_result)

        if not login_result.get("ok") and not terminal_reason:
            if use_yumail or has_mailbox_credential:
                emit_progress(index, email, "chatgpt_email_otp_login")
                email_otp_result = login_with_chatgpt_email_otp(
                    account_service=account_service,
                    email=email,
                    mail_config=otp_mail_config,
                    mailbox=otp_mailbox,
                    wait_for_code=resolve_otp_code,
                    prime_mailbox=None if use_yumail else prime_mailbox,
                    proxy=proxy.url,
                )
                row["email_otp_attempts"] = int(email_otp_result.get("otp_attempts") or 0)
                if email_otp_result.get("ok"):
                    login_result = email_otp_result
                    row["login_via_chatgpt_email_otp"] = True
                    emit_progress(index, email, "chatgpt_email_otp_token_received")
                else:
                    terminal_reason = _terminal_recovery_reason(email_otp_result)
                    row["chatgpt_email_otp_error"] = sanitize_error(
                        f"{email_otp_result.get('stage') or 'email_otp'}: {email_otp_result.get('error') or 'failed'}"
                    )
            else:
                row["chatgpt_email_otp_error"] = "need_outlook_mailbox_credentials_or_yumail"

        if terminal_reason:
            terminal_account = mark_terminal_account(account_service, old_token, terminal_reason)
            row.update(
                {
                    "stage": "terminal",
                    "terminal_reason": terminal_reason,
                    "error": _terminal_recovery_message(terminal_reason),
                    "terminal_persisted": terminal_account is not None,
                }
            )
            emit_progress(index, email, "terminal", terminal_reason=terminal_reason)
            return row

        login_error = str(login_result.get("error") or "").strip().lower()
        login_error_code = _login_error_code(login_result)
        invalid_password = (
            not openai_password
            or login_error in {"invalid_password", "missing_openai_password"}
            or login_error.startswith("password_verify_failed_401")
            or login_error_code == "invalid_username_or_password"
        )
        if not login_result.get("ok") and invalid_password and allow_password_reset and (
            use_yumail or has_mailbox_credential
        ):
            row["stage"] = "password_reset"
            emit_progress(index, email, "password_reset")
            replacement_password = generate_recovery_password()
            reset_result = reset_openai_password(
                account_service=account_service,
                email=email,
                new_password=replacement_password,
                mail_config=otp_mail_config,
                mailbox=otp_mailbox,
                wait_for_code=resolve_otp_code,
                prime_mailbox=None if use_yumail else prime_mailbox,
            )
            row["password_reset"] = bool(reset_result.get("ok"))
            row["password_reset_otp_attempts"] = int(reset_result.get("otp_attempts") or 0)
            if not reset_result.get("ok"):
                reset_terminal_reason = _terminal_recovery_reason(reset_result)
                if reset_terminal_reason:
                    terminal_account = mark_terminal_account(account_service, old_token, reset_terminal_reason)
                    row.update(
                        {
                            "stage": "terminal",
                            "terminal_reason": reset_terminal_reason,
                            "error": _terminal_recovery_message(reset_terminal_reason),
                            "terminal_persisted": terminal_account is not None,
                        }
                    )
                    emit_progress(index, email, "terminal", terminal_reason=reset_terminal_reason)
                    return row
                row["error"] = sanitize_error(
                    f"{reset_result.get('stage') or 'password_reset'}: {reset_result.get('error') or 'failed'}"
                )
                return row

            openai_password = replacement_password
            account["password"] = replacement_password
            account_service.update_account(
                old_token,
                {"password": replacement_password, "updated_at": utc_now_iso()},
                quiet=True,
            )
            reset_login_result = reset_result.get("login_result")
            if isinstance(reset_login_result, dict) and reset_login_result.get("ok"):
                login_result = reset_login_result
                row["login_via_password_reset_code"] = True
                emit_progress(index, email, "password_reset_token_exchanged")
            else:
                row["stage"] = "login_after_password_reset"
                emit_progress(index, email, "login_after_password_reset")
                login_result, reset_login_attempts = login_with_password_retries(
                    account_service=account_service,
                    email=email,
                    password=replacement_password,
                    mail_config=otp_mail_config,
                    mailbox=otp_mailbox,
                    wait_for_code=resolve_otp_code,
                    prime_mailbox=None if use_yumail else prime_mailbox,
                )
                row["login_attempts"] = login_attempts + reset_login_attempts
                if reset_result.get("token_exchange_error"):
                    row["password_reset_token_exchange_error"] = sanitize_error(
                        reset_result.get("token_exchange_error")
                    )

        if not login_result.get("ok"):
            row["login_error_code"] = _login_error_code(login_result)
            primary_error = sanitize_error(login_result.get("error") or "login failed")
            if "need_verification" in primary_error.lower() and not use_yumail and not has_mailbox_credential:
                primary_error = "need_outlook_mailbox_credentials_or_yumail"
            fallback_error = str(row.get("chatgpt_email_otp_error") or "").strip()
            row["error"] = f"{primary_error}; ChatGPT email OTP: {fallback_error}" if fallback_error else primary_error
            return row

        new_token = str(login_result.get("access_token") or "").strip()
        new_refresh_token = str(login_result.get("refresh_token") or "").strip()
        if not new_token:
            row.update({"stage": "token", "error": "login response missing access_token"})
            return row
        row["has_refresh_token"] = bool(new_refresh_token)
        row["has_chatgpt_session_token"] = bool(login_result.get("chatgpt_session_token"))
        new_existed_before = account_service.get_account(new_token) is not None

        row["stage"] = "staging"
        emit_progress(index, email, "staging_new_token")
        now = utc_now_iso()
        staged = build_staged_account(account, login_result, proxy.url, now)
        account_service.add_account_items([staged], include_items=False)

        row["stage"] = "panda_webshare_verify"
        emit_progress(index, email, "panda_webshare_verify")
        refresh_result = account_service.refresh_accounts(
            [new_token],
            defer_invalid_removal=True,
            include_items=False,
        )
        if refresh_result.get("errors") or int(refresh_result.get("refreshed") or 0) <= 0:
            row["error"] = sanitize_error(refresh_result.get("errors") or "Panda backend refresh failed")
            return row

        resolved_new_token = account_service.resolve_access_token(new_token) or new_token
        verified_account = account_service.get_account(resolved_new_token)
        if not isinstance(verified_account, dict):
            row.update({"stage": "panda_webshare_verify", "error": "verified token missing from account service"})
            return row
        if normalize_email(verified_account.get("email")) != email:
            row.update({"stage": "panda_webshare_verify", "error": "verified account email mismatch"})
            return row

        row["stage"] = "commit"
        emit_progress(index, email, "commit")
        now = utc_now_iso()
        commit_updates: dict[str, Any] = {
            "proxy": proxy.url,
            "last_refresh_error": None,
            "last_refresh_error_at": None,
            "outlook_recovery_last_error": None,
            **build_verified_updates(now),
        }
        try:
            from services.account_identity import proxy_binding_hash
            from services.proxy_health import measure_proxy_egress_ip

            commit_updates["proxy_binding_hash"] = proxy_binding_hash(proxy.url)
            egress = measure_proxy_egress_ip(proxy.url, timeout=float(proxy_timeout))
            if egress.get("ok") and egress.get("egress_hash"):
                commit_updates["proxy_egress_hash"] = egress.get("egress_hash")
                row["proxy_egress_hash"] = egress.get("egress_hash")
        except Exception as egress_exc:
            row["egress_writeback_error"] = sanitize_error(f"{type(egress_exc).__name__}: {egress_exc}")
        account_service.update_account(resolved_new_token, commit_updates, quiet=True)
        final_account = account_service.get_account(resolved_new_token)
        if not isinstance(final_account, dict):
            row["error"] = "committed token missing from account service"
            return row
        if has_failure_evidence(final_account):
            row["error"] = "failure evidence remained after successful verification"
            return row
        if str(final_account.get("panda_receive_state") or "") != "verified_ready":
            row["error"] = "Panda receive state did not become verified_ready"
            return row

        old_removed = True
        if resolved_new_token != old_token:
            delete_result = account_service.delete_accounts([old_token], include_items=False)
            old_removed = int(delete_result.get("removed") or 0) > 0 or account_service.get_account(old_token) is None
        if not old_removed:
            row["error"] = "new token verified but old token removal failed"
            return row

        final_account = account_service.get_account(resolved_new_token) or final_account
        schedulable = bool(account_service._is_image_account_schedulable(final_account))
        row.update(
            {
                "ok": True,
                "stage": "done",
                "new_token_hash": token_hash(resolved_new_token),
                "quota": int(final_account.get("quota") or 0),
                "status": str(final_account.get("status") or ""),
                "panda_receive_state": str(final_account.get("panda_receive_state") or ""),
                "schedulable": schedulable,
                "old_removed": old_removed,
                "old_fp_inherited": bool(final_account.get("fp")),
            }
        )
        return row
    except Exception as exc:
        row["error"] = sanitize_error(f"{type(exc).__name__}: {exc}")
        return row
    finally:
        if not row.get("ok") and new_token and new_token != old_token and not new_existed_before:
            cleanup_token = resolved_new_token or account_service.resolve_access_token(new_token) or new_token
            try:
                account_service.delete_accounts([cleanup_token], include_items=False)
                row["new_token_rolled_back"] = True
            except Exception as cleanup_exc:
                row["rollback_error"] = sanitize_error(f"{type(cleanup_exc).__name__}: {cleanup_exc}")
        row["elapsed_sec"] = round(time.time() - started, 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "恢复 Panda 上失效的 Outlook 账号：密码重登 + YuMail/Outlook OTP -> 新 token -> "
            "不继承旧 fp -> Webshare /backend-api 验证 -> 成功后删除旧 token。"
        )
    )
    parser.add_argument("--root", default=str(Path.cwd()), help="Panda 项目根目录")
    parser.add_argument(
        "--credentials-file",
        default="",
        help="可选 Outlook 凭据文件：email----password----client_id----refresh_token（YuMail OTP 时可省略）",
    )
    parser.add_argument("--proxy-file", help="Webshare 代理文件；默认回退到账号已有 proxy")
    parser.add_argument("--target-file", help="可选，每行一个要恢复的邮箱；不传则恢复全部异常 Outlook")
    parser.add_argument("--target-email", action="append", default=[], help="可重复指定目标邮箱")
    parser.add_argument("--limit", type=int, default=0, help="最多恢复数量，0 表示不限")
    parser.add_argument("--dry-run", action="store_true", help="只列出目标和凭据覆盖，不登录、不改库")
    parser.add_argument("--otp-timeout", type=int, default=180, help="等待 OTP 的秒数")
    parser.add_argument("--otp-interval", type=int, default=2, help="轮询邮件间隔秒数")
    parser.add_argument("--proxy-attempts", type=int, default=10, help="每个账号最多尝试的 Webshare 代理数")
    parser.add_argument("--proxy-timeout", type=int, default=20, help="Webshare csrf 单次验证超时秒数")
    parser.add_argument("--allow-password-reset", action="store_true", help="邮箱 OTP 登录也失败时，允许重置 OpenAI 密码")
    parser.add_argument(
        "--allow-account-password",
        action="store_true",
        help="允许仅凭号池 OpenAI 密码选中目标（无需 Outlook 邮箱凭据文件）",
    )
    parser.add_argument(
        "--prefer-yumail-otp",
        action="store_true",
        help="优先用 YuMail/mailManage API 收 OTP（默认环回 8782）",
    )
    parser.add_argument(
        "--skip-mailbox-if-missing",
        action="store_true",
        help="无 Outlook mailbox 凭据时跳过 Graph/IMAP 预检（需 YuMail）",
    )
    parser.add_argument("--report-dir", help="脱敏报告目录")
    parser.add_argument("--backup-dir", help="变更前备份目录；非 dry-run 必填")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")
    if args.otp_timeout < 30 or args.otp_interval < 1:
        raise SystemExit("invalid OTP timing")
    if args.proxy_attempts < 1 or args.proxy_timeout < 5:
        raise SystemExit("invalid proxy validation settings")

    root = Path(args.root).expanduser().resolve()
    credentials_path = Path(args.credentials_file).expanduser().resolve() if str(args.credentials_file or "").strip() else None
    proxy_path = Path(args.proxy_file).expanduser().resolve() if args.proxy_file else None
    target_path = Path(args.target_file).expanduser().resolve() if args.target_file else None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = Path(args.report_dir).expanduser().resolve() if args.report_dir else root / "reports" / f"panda-outlook-manual-recovery-{stamp}"
    backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None

    if credentials_path is not None and not credentials_path.is_file():
        raise SystemExit(f"credentials file not found: {credentials_path}")
    if proxy_path is not None and not proxy_path.is_file():
        raise SystemExit(f"proxy file not found: {proxy_path}")
    if target_path is not None and not target_path.is_file():
        raise SystemExit(f"target file not found: {target_path}")
    if not args.dry_run and backup_dir is None:
        raise SystemExit("--backup-dir is required unless --dry-run is used")
    if credentials_path is None and not args.allow_account_password and not args.prefer_yumail_otp:
        raise SystemExit("credentials file required unless --allow-account-password / --prefer-yumail-otp")

    sys.path.insert(0, str(root))
    from services.account_service import account_service
    from services.config import config
    from services.outlook_mail import parse_outlook_credentials, wait_for_code

    # 恢复报告只输出脱敏阶段信息，压掉后端 debug 中的完整邮箱/账号标识。
    config.data["log_levels"] = ["warning", "error"]

    credentials = (
        parse_outlook_credentials(credentials_path.read_text(encoding="utf-8-sig", errors="ignore"))
        if credentials_path is not None
        else []
    )
    credentials_by_email = {normalize_email(item.get("email")): item for item in credentials if normalize_email(item.get("email"))}
    target_emails = read_target_emails(target_path, args.target_email)
    accounts = account_service.list_accounts()
    selected = select_recovery_targets(
        accounts,
        credential_emails=set(credentials_by_email),
        target_emails=target_emails,
        limit=args.limit,
        allow_account_password=bool(args.allow_account_password or args.prefer_yumail_otp),
    )

    explicit_or_failed = [
        account
        for account in accounts
        if is_outlook_account(account)
        and not is_terminal_recovery_account(account)
        and (
            normalize_email(account.get("email")) in target_emails
            if target_emails
            else has_failure_evidence(account)
        )
    ]
    missing_credentials = sorted(
        mask_email(account.get("email"))
        for account in explicit_or_failed
        if normalize_email(account.get("email")) not in credentials_by_email
    )
    proxy_specs = load_proxy_specs(proxy_path)

    base_summary: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "dry_run": bool(args.dry_run),
        "selected": len(selected),
        "credential_count": len(credentials_by_email),
        "missing_credentials": missing_credentials,
        "proxy_count": len(proxy_specs),
        "report_dir": str(report_dir),
        "backup_dir": str(backup_dir) if backup_dir else None,
    }
    if args.dry_run:
        rows = [
            {
                "index": index,
                "email": mask_email(account.get("email")),
                "old_token_hash": token_hash(account.get("access_token")),
                "old_quota": int(account.get("quota") or 0),
                "status": str(account.get("status") or ""),
                "panda_receive_state": str(account.get("panda_receive_state") or ""),
            }
            for index, account in enumerate(selected, start=1)
        ]
        summary = {**base_summary, "restored": 0, "schedulable": 0, "failed": 0}
        write_report(report_dir, summary, rows)
        print(json.dumps(summary, ensure_ascii=False))
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0

    assert backup_dir is not None
    create_backup(root, backup_dir)

    def activate_login_proxy(proxy_url: str) -> None:
        # 仅覆盖本次恢复进程，不保存 config.json；登录和验证使用同一条已通过 csrf 的 Webshare。
        runtime = dict(config.data.get("proxy_runtime") or {})
        runtime.update(
            {
                "enabled": True,
                "egress_mode": "single_proxy",
                "proxy_url": proxy_url,
                "skip_ssl_verify": False,
            }
        )
        config.data["proxy_runtime"] = runtime
        config.data["proxy"] = proxy_url

    rows: list[dict[str, Any]] = []
    for index, account in enumerate(selected, start=1):
        email = normalize_email(account.get("email"))
        row = recover_one_account(
            index=index,
            account=account,
            credential=credentials_by_email.get(email),
            proxy_specs=proxy_specs,
            account_service=account_service,
            wait_for_code=wait_for_code,
            otp_timeout=args.otp_timeout,
            otp_interval=args.otp_interval,
            proxy_attempts=args.proxy_attempts,
            proxy_timeout=args.proxy_timeout,
            activate_login_proxy=activate_login_proxy,
            prime_mailbox=prime_mailbox_messages,
            allow_password_reset=args.allow_password_reset,
            prefer_yumail_otp=bool(args.prefer_yumail_otp),
            skip_mailbox_if_missing=bool(args.skip_mailbox_if_missing),
        )
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    restored = sum(1 for row in rows if row.get("ok"))
    schedulable = sum(1 for row in rows if row.get("ok") and row.get("schedulable"))
    summary = {
        **base_summary,
        "restored": restored,
        "schedulable": schedulable,
        "verified_not_schedulable": restored - schedulable,
        "failed": len(rows) - restored,
        "all_selected_restored": restored == len(selected),
    }
    write_report(report_dir, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

