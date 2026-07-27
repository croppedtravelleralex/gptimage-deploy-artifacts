"""YuMail / mailManage OTP client (Outlook + yumail.co pool).

Panda 上 `yumail.relai.asia` 反代到 mailManage `:8782`（非旧 yumailManage `:8780`）。
恢复链路默认走本机环回，避免公网 CF。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from services.config import DATA_DIR

DEFAULT_API_BASE = "http://127.0.0.1:8782/api/v1"
SECRET_API_KEY_FILE = DATA_DIR / "runlogs" / "yumail_api_key.secret.txt"
_OTP_RE = re.compile(r"(?<![#&])\b(\d{6})\b")
# OpenAI 验证码主题/发件过滤；避免裸 otp/verify 命中营销信。
_OPENAI_HINTS = (
    "openai",
    "chatgpt",
    "noreply@openai",
    "验证码",
    "代码为",
    "verification code",
    "one-time code",
    "one-time password",
    "código",
    "codigo",
    "inicio de sesión",
)
# 已知非 OTP 的 6 位噪声（电话区号片段等），抽码时跳过。
_OTP_NOISE_CODES = frozenset({"177010"})


def resolve_api_base(explicit: str | None = None) -> str:
    raw = str(explicit or os.getenv("YUMAIL_API_BASE") or DEFAULT_API_BASE).strip()
    base = raw.rstrip("/")
    # 允许用户只写 http://127.0.0.1:8782/api → 自动补 /v1
    if base.endswith("/api"):
        base = f"{base}/v1"
    return base


def resolve_api_key(explicit: str | None = None) -> str:
    key = str(explicit or os.getenv("YUMAIL_API_KEY") or "").strip()
    if key:
        return key
    path = Path(os.getenv("YUMAIL_API_KEY_FILE") or SECRET_API_KEY_FILE)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8-sig", errors="ignore").strip().splitlines()[0].strip()
    except OSError:
        pass
    return ""


def is_configured(*, api_base: str | None = None, api_key: str | None = None) -> bool:
    return bool(resolve_api_key(api_key) and resolve_api_base(api_base))


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "gptimage-yumail-otp/1.0",
    }


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = None
    headers = _auth_headers(api_key)
    if payload is not None:
        # 用 data= 发 JSON，避免 curl_cffi chrome impersonate 吞 body；标准库同形。
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        data = raw
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if not body:
                return {}
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else {"data": parsed}
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"yumail_otp_http_{exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"yumail_unreachable: {exc.reason}") from exc


def probe_reachable(*, api_base: str | None = None, api_key: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    base = resolve_api_base(api_base)
    key = resolve_api_key(api_key)
    if not key:
        return {"ok": False, "error": "yumail_not_configured"}
    try:
        data = _request_json("GET", f"{base}/pool/whoami", api_key=key, timeout=timeout)
        return {"ok": True, "api_base": base, "whoami": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "api_base": base, "error": str(exc)[:240]}


def _extract_otp_from_text(*parts: object) -> str | None:
    blob = "\n".join(str(part or "") for part in parts)
    if not blob.strip():
        return None
    match = re.search(
        r"(?:Verification code|code is|代码为|验证码|código(?: de un solo uso)?|codigo)[:\s]*(\d{6})",
        blob,
        re.I,
    )
    if match and match.group(1) not in _OTP_NOISE_CODES:
        return match.group(1)
    for hit in _OTP_RE.findall(blob):
        if hit not in _OTP_NOISE_CODES:
            return hit
    return None


def _message_is_openai_otp(subject: str, sender: str, body: str) -> bool:
    blob = f"{subject}\n{sender}\n{body}".lower()
    return any(hint in blob for hint in _OPENAI_HINTS)


def _parse_received_ts(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def resolve_outlook_token_id(
    email: str,
    *,
    api_base: str | None = None,
    api_key: str | None = None,
) -> str:
    target = str(email or "").strip().lower()
    if not target:
        raise RuntimeError("yumail_outlook_email_required")
    base = resolve_api_base(api_base)
    key = resolve_api_key(api_key)
    if not key:
        raise RuntimeError("yumail_not_configured")
    data = _request_json("GET", f"{base}/outlook/tokens?limit=2000", api_key=key, timeout=30.0)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("email") or "").strip().lower() == target:
            token_id = str(item.get("id") or "").strip()
            if token_id:
                return token_id
    raise RuntimeError("yumail_outlook_token_not_found")


def _list_outlook_folder(
    token_id: str,
    *,
    junk: bool,
    api_base: str,
    api_key: str,
) -> list[dict[str, Any]]:
    path = "outlook/inbox/junk" if junk else "outlook/inbox"
    url = f"{api_base}/{path}?{urlencode({'id': token_id})}"
    data = _request_json("GET", url, api_key=api_key, timeout=45.0)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return [item for item in items if isinstance(item, dict)]


def wait_for_outlook_otp(
    email: str,
    *,
    not_before: datetime | float | None = None,
    timeout_sec: float = 180.0,
    poll_interval: float = 4.0,
    api_base: str | None = None,
    api_key: str | None = None,
) -> str:
    """轮询 Outlook inbox+junk，抽取 OpenAI/ChatGPT 验证码（兼容中文「验证码」）。

    优先 mailManage Graph inbox；若长期为空（常见：令牌仅有 IMAP 权限、无 Mail.Read），
    回退到本机 secret 中的 client_id/refresh_token 走 IMAP XOAUTH2。
    """
    base = resolve_api_base(api_base)
    key = resolve_api_key(api_key)
    if not key:
        raise RuntimeError("yumail_not_configured")
    token_id = resolve_outlook_token_id(email, api_base=base, api_key=key)
    if isinstance(not_before, datetime):
        boundary = not_before.astimezone(timezone.utc).timestamp() - 3.0
    elif isinstance(not_before, (int, float)) and float(not_before) > 0:
        boundary = float(not_before) - 3.0
    else:
        boundary = time.time() - 3.0

    deadline = time.monotonic() + max(30.0, float(timeout_sec))
    seen: set[str] = set()
    last_error = ""
    empty_rounds = 0
    while time.monotonic() < deadline:
        try:
            messages = _list_outlook_folder(token_id, junk=False, api_base=base, api_key=key)
            messages.extend(_list_outlook_folder(token_id, junk=True, api_base=base, api_key=key))
            if not messages:
                empty_rounds += 1
            for msg in messages:
                mid = str(msg.get("id") or msg.get("message_id") or "").strip()
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                received = _parse_received_ts(msg.get("received") or msg.get("receivedDateTime") or msg.get("date"))
                # 无时间戳则跳过，避免复用旧 OTP。
                if received is None:
                    continue
                if received < boundary:
                    continue
                subject = str(msg.get("subject") or "")
                sender = str(msg.get("from") or msg.get("sender") or "")
                body = str(msg.get("body") or msg.get("bodyPreview") or msg.get("preview") or "")
                if not _message_is_openai_otp(subject, sender, body):
                    continue
                code = _extract_otp_from_text(subject, body)
                if code:
                    return code
            # Graph 连续空箱：改走 IMAP（Mail.Read 缺失时的常见形态）
            if empty_rounds >= 2:
                code = _try_imap_otp(email, boundary=boundary)
                if code:
                    return code
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:240]
            if str(exc).startswith("yumail_unreachable"):
                raise
            # Graph 失败也尝试 IMAP
            try:
                code = _try_imap_otp(email, boundary=boundary)
                if code:
                    return code
            except Exception as imap_exc:  # noqa: BLE001
                last_error = f"{last_error}; imap:{str(imap_exc)[:160]}"
        time.sleep(max(2.0, float(poll_interval)))
    if last_error:
        raise RuntimeError(f"yumail_otp_timeout: {last_error}")
    raise RuntimeError("yumail_otp_timeout")


def _load_outlook_imap_cred(email: str) -> dict[str, str] | None:
    """从 secret 文件加载 Outlook IMAP 凭据（不进 git）。

    文件：`data/runlogs/outlook_imap_creds.secret.json`
    形如：{"ivette@outlook.com": {"client_id":"...","refresh_token":"..."}}
    """
    target = str(email or "").strip().lower()
    path = Path(os.getenv("OUTLOOK_IMAP_CREDS_FILE") or (DATA_DIR / "runlogs" / "outlook_imap_creds.secret.json"))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    item = data.get(target)
    if not isinstance(item, dict):
        # 允许大小写不一致的 key
        for k, v in data.items():
            if str(k).strip().lower() == target and isinstance(v, dict):
                item = v
                break
    if not isinstance(item, dict):
        return None
    client_id = str(item.get("client_id") or "").strip()
    refresh = str(item.get("refresh_token") or "").strip()
    if not client_id or not refresh:
        return None
    return {"client_id": client_id, "refresh_token": refresh, "email": target}


def _outlook_imap_access_token(client_id: str, refresh_token: str) -> str:
    import urllib.parse

    scope = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": scope,
        }
    ).encode("utf-8")
    req = Request(
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:240]
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"outlook_imap_token_http_{exc.code}: {detail}") from exc
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("outlook_imap_token_missing")
    return token


def _try_imap_otp(email: str, *, boundary: float) -> str | None:
    import email as email_lib
    import imaplib
    from email.header import decode_header, make_header
    from email.utils import parsedate_to_datetime

    cred = _load_outlook_imap_cred(email)
    if not cred:
        return None
    access = _outlook_imap_access_token(cred["client_id"], cred["refresh_token"])
    auth_string = f"user={cred['email']}\x01auth=Bearer {access}\x01\x01"
    hosts = ("outlook.office365.com", "outlook.live.com")
    last_err = ""
    for host in hosts:
        imap = None
        try:
            imap = imaplib.IMAP4_SSL(host, timeout=30)
            imap.authenticate("XOAUTH2", lambda _=None, s=auth_string: s.encode("utf-8"))
            for mailbox_name in ("INBOX", "Junk"):
                try:
                    status, _ = imap.select(mailbox_name, readonly=True)
                except Exception:
                    continue
                if status != "OK":
                    continue
                status, data = imap.uid("search", None, "ALL")
                if status != "OK" or not data or not data[0]:
                    continue
                uids = data[0].split()[-20:]
                for uid in reversed(uids):
                    status, fetched = imap.uid("fetch", uid, "(RFC822)")
                    if status != "OK":
                        continue
                    raw = next(
                        (part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)),
                        b"",
                    )
                    if not raw:
                        continue
                    msg = email_lib.message_from_bytes(raw)
                    try:
                        received = parsedate_to_datetime(str(msg.get("Date") or "")).timestamp()
                    except Exception:
                        received = None
                    if received is not None and received < boundary:
                        continue

                    def _decode(value: object) -> str:
                        text = str(value or "")
                        if not text:
                            return ""
                        try:
                            return str(make_header(decode_header(text)))
                        except Exception:
                            return text

                    subject = _decode(msg.get("Subject"))
                    sender = _decode(msg.get("From"))
                    parts: list[str] = []
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_maintype() == "multipart":
                                continue
                            try:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                            except Exception:
                                continue
                    else:
                        try:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
                        except Exception:
                            pass
                    body = "\n".join(parts)
                    if not _message_is_openai_otp(subject, sender, body):
                        continue
                    code = _extract_otp_from_text(subject, body)
                    if code:
                        return code
            return None
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:200]
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass
    if last_err:
        raise RuntimeError(last_err)
    return None


def wait_for_pool_otp(
    email: str,
    *,
    not_before: datetime | float | None = None,
    timeout_sec: float = 120.0,
    poll_interval: float = 8.0,
    sender_contains: str = "openai",
    subject_contains: str = "",
    api_base: str | None = None,
    api_key: str | None = None,
) -> str:
    """yumail.co 等池账号：POST /pool/otp/poll。"""
    base = resolve_api_base(api_base)
    key = resolve_api_key(api_key)
    if not key:
        raise RuntimeError("yumail_not_configured")
    if isinstance(not_before, datetime):
        boundary = not_before.astimezone(timezone.utc).timestamp() - 3.0
    elif isinstance(not_before, (int, float)) and float(not_before) > 0:
        boundary = float(not_before) - 3.0
    else:
        boundary = time.time() - 3.0
    payload = {
        "email": str(email or "").strip(),
        "timeout_sec": int(max(10, min(300, timeout_sec))),
        "poll_interval": float(max(3.0, min(30.0, poll_interval))),
        "match": {
            "sender_contains": sender_contains,
            "subject_contains": subject_contains,
        },
        # 服务端若支持则过滤；客户端仍校验 received。
        "not_before": datetime.fromtimestamp(boundary, tz=timezone.utc).isoformat(),
    }
    data = _request_json(
        "POST",
        f"{base}/pool/otp/poll",
        api_key=key,
        payload=payload,
        timeout=float(timeout_sec) + 30.0,
    )
    if data.get("found") and str(data.get("otp") or "").strip():
        received = _parse_received_ts(data.get("received") or data.get("received_at") or data.get("date"))
        if received is not None and received < boundary:
            raise RuntimeError("yumail_otp_timeout: stale_otp_before_boundary")
        return str(data.get("otp")).strip()
    err = str(data.get("error") or data.get("message") or "timeout").strip()
    raise RuntimeError(f"yumail_otp_timeout: {err}")


def wait_for_code_by_email(
    email: str,
    *,
    not_before: datetime | float | None = None,
    timeout_sec: float = 180.0,
    poll_interval: float = 4.0,
    api_base: str | None = None,
    api_key: str | None = None,
) -> str:
    """按邮箱域名分流：Outlook → Graph inbox；其它 → pool otp/poll。"""
    addr = str(email or "").strip().lower()
    if addr.endswith(("@outlook.com", "@hotmail.com", "@live.com")):
        return wait_for_outlook_otp(
            addr,
            not_before=not_before,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
            api_base=api_base,
            api_key=api_key,
        )
    return wait_for_pool_otp(
        addr,
        not_before=not_before,
        timeout_sec=timeout_sec,
        poll_interval=max(3.0, poll_interval),
        api_base=api_base,
        api_key=api_key,
    )
