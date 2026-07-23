#!/usr/bin/env python3
"""Outlook + Webshare + Camoufox 的固定注册/重登链路。

Authoritative steps (see docs/16-camoufox-stable-pipeline.md):

  1) pick mailbox from Outlook credentials file
  2) pick a Webshare node NOT already used in the account pool
  3) check: proxy egress probe + Outlook mailbox preflight
  4) register: Camoufox OpenAI signup (OTP / about-you / PKCE)
     relogin: ChatGPT NextAuth 初始化（state/PKCE Cookie）→ OTP → session
  5) land: local accounts.db + panda_import.secret.json (identity_isolated)
  6) upload blob to Panda for observe (runtime secret only; not code deploy)

Usage examples:

  python scripts/outlook_camoufox_stable_register.py \\
    --accounts-file "C:/Users/Lenovo/Downloads/0716-4000_015.txt" \\
    --webshare-pool "D:/.../webshare_good_csrf_200.secret.txt" \\
    --exclude-hosts 82.29.223.111,92.113.236.188 \\
    --out-dir data/runlogs/outlook-ws-stable

  python scripts/outlook_camoufox_stable_register.py \\
    --accounts-file ... --proxy "http://user:pass@host:port" --out-dir ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402

from services.register.real_browser_register import (  # noqa: E402
    generate_openai_account_password,
    mask_email,
)
import scripts.yumail_camoufox_openai_register as cam  # noqa: E402

SOURCE_DETAIL = "outlook_camoufox_stable_pipeline"
PIPELINE_VERSION = "2026-07-22"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(phase: str, **kwargs: Any) -> None:
    print(json.dumps({"phase": phase, **kwargs}, ensure_ascii=False), flush=True)


def proxy_endpoint(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = str(parsed.hostname or "")
    return f"{host}:{parsed.port}" if parsed.port else host


def proxy_hash(url: str) -> str:
    return hashlib.sha256(proxy_endpoint(url).encode("utf-8")).hexdigest()[:12]


def redact_proxy_secret(value: object, *proxies: str) -> str:
    text = str(value or "")
    for proxy in proxies:
        raw = str(proxy or "").strip()
        if raw:
            text = text.replace(raw, f"<proxy:{proxy_endpoint(raw)}>")
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        for secret in (parsed.username, parsed.password):
            if secret:
                text = text.replace(secret, "<redacted>")
    return text


def outlook_mail_config(credential: dict[str, str]) -> dict[str, Any]:
    email = str(credential.get("email") or "").strip().lower()
    domain = email.split("@", 1)[-1] if "@" in email else ""
    imap_host = "outlook.live.com" if domain in {"outlook.com", "hotmail.com", "live.com"} else "outlook.office365.com"
    return {
        "request_timeout": 45,
        "wait_timeout": 180,
        "wait_interval": 3,
        "otp_backend": "provider",
        # 邮箱读取不经过浏览器代理，避免把邮箱会话与 OpenAI 出口耦合。
        "api_use_register_proxy": False,
        "proxy": "",
        "providers": [
            {
                "type": "outlook_token",
                "enable": True,
                "label": "OutlookCamoufoxStable",
                "mode": "auto",
                "imap_host": imap_host,
                "message_limit": 10,
                "mailboxes": [credential],
            }
        ],
    }


def probe_proxy(proxy: str) -> dict[str, Any]:
    from curl_cffi import requests as crequests

    out: dict[str, Any] = {"ok": False, "proxy_hash": proxy_hash(proxy)}
    session = None
    try:
        session = crequests.Session(
            impersonate="chrome",
            proxies={"http": proxy, "https": proxy},
            timeout=25,
        )
        response = session.get("https://www.cloudflare.com/cdn-cgi/trace")
        out["http"] = response.status_code
        trace = response.text or ""
        out["warp"] = "warp=on" in trace
        for line in trace.splitlines():
            if line.startswith(("ip=", "loc=", "colo=")):
                key, _, value = line.partition("=")
                out[key] = value
        out["ok"] = response.status_code == 200 and bool(out.get("ip"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = redact_proxy_secret(f"{type(exc).__name__}: {exc}", proxy)[:300]
    finally:
        if session is not None:
            session.close()
    return out


def prepare_chatgpt_nextauth(email: str, proxy: str) -> tuple[str, list[dict[str, Any]]]:
    """在同一浏览器出口上创建匹配的 NextAuth state Cookie 和 authorize URL。"""
    from curl_cffi import requests as crequests

    device_id = str(uuid.uuid4())
    session = crequests.Session(
        impersonate="chrome",
        proxies={"http": proxy, "https": proxy},
        verify=False,
    )
    try:
        session.cookies.set("oai-did", device_id, domain=".chatgpt.com")
        session.cookies.set("oai-did", device_id, domain="chatgpt.com")
        csrf_response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"accept": "application/json", "referer": "https://chatgpt.com/"},
            timeout=30,
        )
        csrf_data = csrf_response.json() if csrf_response.text else {}
        csrf_token = str(csrf_data.get("csrfToken") or "").strip() if isinstance(csrf_data, dict) else ""
        if csrf_response.status_code != 200 or not csrf_token:
            raise RuntimeError(f"nextauth_csrf_http_{csrf_response.status_code}")

        query = {
            "prompt": "login",
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        signin_response = session.post(
            "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query),
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/auth/login",
                "x-auth-return-redirect": "1",
            },
            data={"csrfToken": csrf_token, "callbackUrl": "https://chatgpt.com/", "json": "true"},
            timeout=30,
        )
        signin_data = signin_response.json() if signin_response.text else {}
        authorize_url = str(signin_data.get("url") or "").strip() if isinstance(signin_data, dict) else ""
        parsed = urlparse(authorize_url)
        if (
            signin_response.status_code != 200
            or parsed.hostname != "auth.openai.com"
            or parsed.path != "/api/accounts/authorize"
        ):
            raise RuntimeError(f"nextauth_signin_http_{signin_response.status_code}")

        cookies: list[dict[str, Any]] = []
        allowed_prefixes = ("__Host-next-auth.", "__Secure-next-auth.")
        for cookie in session.cookies.jar:
            if cookie.name != "oai-did" and not cookie.name.startswith(allowed_prefixes):
                continue
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                    "secure": bool(cookie.secure),
                }
            )
        if not any(cookie["name"] == "__Secure-next-auth.state" for cookie in cookies):
            raise RuntimeError("nextauth_state_cookie_missing")
        return authorize_url, cookies
    finally:
        session.close()


def collect_chatgpt_session(page: Any, proxy: str) -> dict[str, str]:
    """读取 callback 建立的 ChatGPT session；浏览器内失败时以同 Cookie/出口交接一次。"""
    body: dict[str, Any] = {}
    status = 0
    try:
        payload = page.evaluate(
            """async () => {
              try {
                const response = await fetch('/api/auth/session', {credentials: 'include'});
                return {status: response.status, body: await response.json()};
              } catch (error) {
                return {status: 0, error: String(error)};
              }
            }"""
        )
        status = int((payload or {}).get("status") or 0)
        raw_body = (payload or {}).get("body") if isinstance(payload, dict) else {}
        body = raw_body if isinstance(raw_body, dict) else {}
    except Exception as exc:  # noqa: BLE001
        cam._log("chatgpt_session_probe_warn", error=f"{type(exc).__name__}: {exc}"[:180])

    access_token = str(body.get("accessToken") or "").strip()
    session_token = str(body.get("sessionToken") or "").strip()
    if not access_token:
        try:
            from services.register.openai_register import create_session

            browser_cookies = list(page.context.cookies("https://chatgpt.com") or [])
            session = create_session(proxy)
            try:
                for cookie in browser_cookies:
                    name = str(cookie.get("name") or "").strip()
                    value = str(cookie.get("value") or "")
                    if not name or not value:
                        continue
                    session.cookies.set(
                        name,
                        value,
                        domain=str(cookie.get("domain") or ".chatgpt.com"),
                        path=str(cookie.get("path") or "/"),
                    )
                    if name in {
                        "__Secure-next-auth.session-token",
                        "__Secure-authjs.session-token",
                        "authjs.session-token",
                    }:
                        session_token = value
                response = session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers={"accept": "application/json", "referer": "https://chatgpt.com/"},
                    timeout=45,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                raw_body = response.json() if response.text else {}
                body = raw_body if isinstance(raw_body, dict) else {}
                access_token = str(body.get("accessToken") or "").strip()
                session_token = str(body.get("sessionToken") or session_token).strip()
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            cam._log(
                "chatgpt_session_cookie_handoff_warn",
                error=redact_proxy_secret(f"{type(exc).__name__}: {exc}", proxy)[:180],
            )
    if not access_token:
        cam._log("chatgpt_session_probe_warn", status=status, error="access_token_missing")
        return {}
    cam._log("chatgpt_session_probe_ok", status=status, has_at=True)
    return {
        "access_token": access_token,
        "refresh_token": "",
        "id_token": "",
        "chatgpt_session_token": session_token,
        "chatgpt_session_expires": str(body.get("expires") or "").strip(),
    }


def collect_platform_tokens(
    page: Any,
    captured: dict[str, Any],
    code_verifier: str,
    proxy: str,
) -> dict[str, str]:
    tokens = dict(captured.get("tokens") or {})
    if not tokens.get("refresh_token") and "code=" in (page.url or ""):
        exchanged = cam._exchange_callback_code(page, code_verifier, proxy)
        if exchanged.get("refresh_token"):
            return exchanged
    if not tokens.get("refresh_token"):
        tokens = cam._wait_for_refresh_token(captured, timeout_sec=75)
    return tokens


def export_panda_blob(token: str, out_path: Path) -> dict[str, Any]:
    from services.account_service import account_service

    account_service.reload_from_storage()
    account = account_service.get_account(token) or {}
    blob = {
        key: account.get(key)
        for key in (
            "email",
            "password",
            "access_token",
            "refresh_token",
            "id_token",
            "chatgpt_session_token",
            "chatgpt_session_expires",
            "proxy",
            "proxy_provider",
            "proxy_scope",
            "lifecycle_ip_mode",
            "proxy_egress_ip",
            "register_egress_ip",
            "source_type",
            "source_detail",
            "fp",
            "status",
            "type",
            "created_at",
        )
    }
    blob.update({"panda_receive_state": "identity_isolated", "panda_sync_state": "ready"})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"email_mask": mask_email(str(blob.get("email") or "")), "path": str(out_path)}


def normalize_proxy(raw: str) -> str:
    line = str(raw or "").strip()
    if not line or line.startswith("#"):
        raise ValueError("empty_proxy")
    if "://" in line:
        return line
    # host:port:user:pass
    if line.count(":") >= 3:
        host, port, user, password = line.split(":", 3)
        return f"http://{user}:{password}@{host}:{port}"
    if ":" in line and "@" not in line:
        return f"http://{line}"
    return line


def proxy_host(proxy: str) -> str:
    u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return str(u.hostname or "")


def load_outlook_line(path: Path, index: int) -> dict[str, str]:
    lines = [
        l.strip()
        for l in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        if l.strip() and "----" in l and not l.strip().startswith("#")
    ]
    if not lines:
        raise RuntimeError("accounts_file_empty")
    if index < 0 or index >= len(lines):
        raise RuntimeError(f"account_index_out_of_range index={index} n={len(lines)}")
    parts = lines[index].split("----")
    if len(parts) < 4:
        raise RuntimeError("account_format_need_email_password_clientid_refreshtoken")
    return {
        "email": parts[0].strip(),
        "password": parts[1].strip(),
        "client_id": parts[2].strip(),
        "refresh_token": parts[3].strip(),
        "raw_line": lines[index],
    }


def load_used_hosts(path: Path | None, extra: list[str]) -> set[str]:
    hosts = {h.strip() for h in extra if h.strip()}
    if path and path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "://" in s or "@" in s:
                hosts.add(proxy_host(normalize_proxy(s)))
            else:
                hosts.add(s.split(":")[0].strip())
    return {h for h in hosts if h}


def pick_fresh_proxy(pool_path: Path, used_hosts: set[str]) -> str:
    lines = [
        l.strip()
        for l in pool_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    for raw in lines:
        try:
            proxy = normalize_proxy(raw)
        except Exception:
            continue
        host = proxy_host(proxy)
        if host and host not in used_hosts:
            return proxy
    raise RuntimeError("no_fresh_webshare_in_pool")


def preflight_mailbox(credential: dict[str, str], sticky_proxy: str) -> dict[str, Any]:
    from scripts.recover_panda_outlook_accounts import build_mailbox, preflight_mailbox_access

    del sticky_proxy
    mail = outlook_mail_config(credential)
    boundary = datetime.now(timezone.utc)
    mailbox = build_mailbox(credential, boundary)
    preflight_mailbox_access(mail, mailbox)
    if mailbox.get("_outlook_imap_host"):
        mail["providers"][0]["imap_host"] = mailbox["_outlook_imap_host"]
    mailbox.pop("_seen_code_message_refs", None)
    return {"mail": mail, "mailbox": mailbox}


def persist_isolated_account(
    *,
    email: str,
    password: str,
    tokens: dict[str, str],
    sticky_proxy: str,
    probe: dict[str, Any],
    report_dir: Path,
    source_detail: str,
) -> dict[str, Any]:
    """只写本地隔离账号和 Panda 导入 blob，不直接修改 Panda。"""
    add = cam._persist_account(
        email=email,
        password=password,
        tokens=tokens,
        proxy=sticky_proxy,
        source_detail=source_detail,
    )
    token = str(tokens.get("access_token") or "")
    from services.account_service import account_service

    account_service.update_account_identity(
        token,
        {
            "panda_receive_state": "identity_isolated",
            "proxy": sticky_proxy,
            "proxy_provider": "webshare",
            "proxy_scope": "account_sticky",
            "lifecycle_ip_mode": "sticky_webshare",
            "proxy_egress_ip": probe.get("ip"),
            "register_egress_ip": probe.get("ip"),
            "source_detail": source_detail,
            "identity_evidence_state": "stable_pipeline_observe",
        },
        reason="outlook_camoufox_stable_pipeline_observe",
        quiet=True,
        clear_isolation=False,
    )
    export = export_panda_blob(token, report_dir / "panda_import.secret.json")
    blob_path = Path(export["path"])
    blob = json.loads(blob_path.read_text(encoding="utf-8"))
    blob.update(
        {
            "proxy": sticky_proxy,
            "proxy_provider": "webshare",
            "proxy_scope": "account_sticky",
            "lifecycle_ip_mode": "sticky_webshare",
            "proxy_egress_ip": probe.get("ip"),
            "source_detail": source_detail,
            "cohort_id": f"outlook_stable_{datetime.now().strftime('%Y%m%d')}",
            "panda_receive_state": "identity_isolated",
        }
    )
    blob_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "token": token,
        "add": {key: add.get(key) for key in ("added", "updated", "total") if key in add},
        "export": export,
    }


def register_outlook(
    *,
    credential: dict[str, str],
    sticky_proxy: str,
    out_dir: Path,
    browser_proxy: str = "",
) -> dict[str, Any]:
    browser_proxy = (browser_proxy or sticky_proxy).strip()
    email = credential["email"].strip().lower()
    report_dir = out_dir / f"outlook-reg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{email.split('@')[0][-8:]}"
    report_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "ok": False,
        "pipeline": PIPELINE_VERSION,
        "source_detail": SOURCE_DETAIL,
        "email_mask": mask_email(email),
        "proxy_hash": proxy_hash(sticky_proxy),
        "browser_proxy": proxy_endpoint(browser_proxy),
        "ts": utc_now(),
        "out": str(report_dir),
    }

    probe = probe_proxy(browser_proxy)
    result["probe"] = probe
    log("proxy_probe", **{k: probe.get(k) for k in ("ok", "ip", "loc", "colo", "http", "error") if k in probe})
    if not probe.get("ok"):
        result["error"] = "proxy_probe_failed"
        (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    try:
        pf = preflight_mailbox(credential, sticky_proxy)
    except Exception as exc:  # noqa: BLE001
        result["error"] = redact_proxy_secret(
            f"mailbox_preflight_failed:{type(exc).__name__}:{exc}",
            sticky_proxy,
            browser_proxy,
        )[:300]
        (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    mail, mailbox = pf["mail"], pf["mailbox"]
    log("mailbox_preflight", email_mask=mask_email(email), imap=mail["providers"][0].get("imap_host"))

    openai_password = generate_openai_account_password()
    authorize_url, code_verifier = cam._authorize_url(email, screen_hint="signup", client="platform")
    proxy_cfg = cam._proxy_dict(browser_proxy)
    launch_kwargs: dict[str, Any] = {"headless": False, "os": "windows", "humanize": True}
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        if "127.0.0.1" not in browser_proxy and "localhost" not in browser_proxy:
            launch_kwargs["geoip"] = True

    try:
        try:
            browser_cm = Camoufox(**launch_kwargs)
            browser = browser_cm.__enter__()
        except Exception as geo_exc:
            if proxy_cfg and launch_kwargs.pop("geoip", None) is not None:
                cam._log("geoip_disabled", error=str(geo_exc)[:160])
                browser_cm = Camoufox(**launch_kwargs)
                browser = browser_cm.__enter__()
            else:
                raise
        try:
            page = browser.new_page()
            captured = cam._attach_token_capture(page)
            page_boundary = datetime.now(timezone.utc)
            boundary = page_boundary - timedelta(minutes=30)
            mailbox["_code_not_before"] = boundary
            mailbox.pop("_seen_code_message_refs", None)
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=120000)
            cam._wait_transition(page, timeout_ms=90000)
            cam._assert_not_cf_blocked(page)
            page.wait_for_timeout(1500)
            path = cam._page_path(page)
            cam._log("authorized", path=path, title=page.title())

            if path.rstrip("/") == "/create-account/password":
                boundary = datetime.now(timezone.utc)
                mailbox["_code_not_before"] = boundary
                cam._switch_to_otp_signup(page)
                path = cam._page_path(page)
                cam._log("switched_to_otp_signup", path=path)

            if "email-verification" in path:
                cam._fill_otp(page, mailbox, mail, boundary)
                path = cam._page_path(page)
                cam._log("otp_done", path=path)

            if path.rstrip("/") == "/create-account/password":
                cam._fill_password(page, openai_password)
                path = cam._page_path(page)
                cam._log("password_done", path=path)

            if "about-you" in path:
                cam._fill_about_you(page, email)
                path = cam._page_path(page)
                cam._log("about_you_done", path=path)

            finished = False
            last_url = ""
            for _ in range(60):
                path = cam._page_path(page)
                last_url = page.url or ""
                if "code=" in last_url or captured.get("tokens", {}).get("refresh_token"):
                    finished = True
                    break
                page.wait_for_timeout(1500)
            if not finished:
                raise RuntimeError(f"registration_incomplete path={path} url={last_url[:180]}")

            tokens = collect_platform_tokens(page, captured, code_verifier, browser_proxy)
            if not tokens.get("refresh_token"):
                raise RuntimeError("refresh_token_missing")

            persisted = persist_isolated_account(
                email=email,
                password=openai_password,
                tokens=tokens,
                sticky_proxy=sticky_proxy,
                probe=probe,
                report_dir=report_dir,
                source_detail=SOURCE_DETAIL,
            )
            token = persisted["token"]
            result.update(
                {
                    "ok": True,
                    "token_hash": hashlib.sha256(token.encode()).hexdigest()[:12],
                    "path": path,
                    "add": persisted["add"],
                    "receive": "identity_isolated",
                    "export": persisted["export"],
                    "next": [
                        "scp panda_import.secret.json to panda /tmp/",
                        "docker exec import with identity_isolated",
                        "refresh; keep observe until mature before set_account_scheduling(True)",
                    ],
                }
            )
        finally:
            browser_cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        result["error"] = redact_proxy_secret(
            f"{type(exc).__name__}: {exc}",
            sticky_proxy,
            browser_proxy,
        )[:500]

    (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def relogin_outlook(
    *,
    credential: dict[str, str],
    sticky_proxy: str,
    out_dir: Path,
    browser_proxy: str = "",
) -> dict[str, Any]:
    """按实测 NextAuth 固定链重登，产物保持隔离，不自动替换 Panda 旧 token。"""
    browser_proxy = (browser_proxy or sticky_proxy).strip()
    email = credential["email"].strip().lower()
    report_dir = out_dir / f"outlook-relogin-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{email.split('@')[0][-8:]}"
    report_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "ok": False,
        "pipeline": PIPELINE_VERSION,
        "mode": "relogin",
        "source_detail": f"{SOURCE_DETAIL}_relogin",
        "email_mask": mask_email(email),
        "proxy_hash": proxy_hash(sticky_proxy),
        "browser_proxy": proxy_endpoint(browser_proxy),
        "ts": utc_now(),
        "out": str(report_dir),
    }

    probe = probe_proxy(browser_proxy)
    result["probe"] = probe
    log("proxy_probe", **{key: probe.get(key) for key in ("ok", "ip", "loc", "colo", "http", "error") if key in probe})
    if not probe.get("ok"):
        result["error"] = "proxy_probe_failed"
        (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    try:
        preflight = preflight_mailbox(credential, sticky_proxy)
        mail, mailbox = preflight["mail"], preflight["mailbox"]
        authorize_url, nextauth_cookies = prepare_chatgpt_nextauth(email, browser_proxy)
        log(
            "nextauth_prepared",
            email_mask=mask_email(email),
            cookie_count=len(nextauth_cookies),
            state_cookie=True,
        )

        proxy_config = cam._proxy_dict(browser_proxy)
        launch_kwargs: dict[str, Any] = {"headless": False, "os": "windows", "humanize": True}
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
        browser_cm = Camoufox(**launch_kwargs)
        browser = browser_cm.__enter__()
        try:
            page = browser.new_page()
            page.context.add_cookies(nextauth_cookies)
            page_boundary = datetime.now(timezone.utc)
            otp_boundary = page_boundary - timedelta(seconds=60)
            mailbox["_code_not_before"] = otp_boundary
            mailbox.pop("_seen_code_message_refs", None)

            page.goto(authorize_url, wait_until="domcontentloaded", timeout=120000)
            cam._wait_transition(page, timeout_ms=90000)
            cam._assert_not_cf_blocked(page)
            page.wait_for_timeout(1500)
            path = cam._page_path(page)
            log("authorized", path=path, title=page.title(), mode="relogin")

            if "password" in path or "log-in" in path:
                try:
                    cam._switch_to_otp_signup(page)
                    path = cam._page_path(page)
                    log("switched_to_otp_login", path=path)
                except Exception as exc:  # noqa: BLE001
                    log("switch_to_otp_login_warn", error=f"{type(exc).__name__}: {exc}"[:180])
            if "email-verification" in path:
                cam._fill_otp(page, mailbox, mail, otp_boundary)
                path = cam._page_path(page)
                cam._assert_not_cf_blocked(page)
                log("otp_done", path=path)
            if "about-you" in path:
                cam._fill_about_you(page, email)
                path = cam._page_path(page)
                log("about_you_done", path=path)

            tokens: dict[str, str] = {}
            last_url = page.url or ""
            for _ in range(60):
                path = cam._page_path(page)
                last_url = page.url or ""
                if urlparse(last_url).hostname == "chatgpt.com" and path == "/":
                    tokens = collect_chatgpt_session(page, browser_proxy)
                    if tokens.get("access_token"):
                        break
                page.wait_for_timeout(1500)
            if not tokens.get("access_token"):
                raise RuntimeError(
                    f"relogin_session_missing path={path} url={last_url.split('?', 1)[0][:180]}"
                )

            persisted = persist_isolated_account(
                email=email,
                password="",
                tokens=tokens,
                sticky_proxy=sticky_proxy,
                probe=probe,
                report_dir=report_dir,
                source_detail=f"{SOURCE_DETAIL}_relogin",
            )
            token = persisted["token"]
            result.update(
                {
                    "ok": True,
                    "token_hash": hashlib.sha256(token.encode()).hexdigest()[:12],
                    "path": path,
                    "add": persisted["add"],
                    "receive": "identity_isolated",
                    "export": persisted["export"],
                    "next": [
                        "backup Panda accounts.db",
                        "import new token as identity_isolated",
                        "verify /backend-api/me through the same sticky Webshare",
                        "remove old token only after verification, then reload_from_storage",
                    ],
                }
            )
        finally:
            browser_cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        result["error"] = redact_proxy_secret(
            f"{type(exc).__name__}: {exc}",
            sticky_proxy,
            browser_proxy,
        )[:500]

    (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Outlook Camoufox fixed register/relogin pipeline")
    ap.add_argument("--mode", choices=("register", "relogin"), default="register")
    ap.add_argument("--accounts-file", required=True, help="Outlook lines: email----password----client_id----refresh_token")
    ap.add_argument("--account-index", type=int, default=0)
    ap.add_argument("--proxy", default="", help="Sticky Webshare URL (or host:port:user:pass)")
    ap.add_argument("--webshare-pool", default="", help="Pool file to pick an unused node from")
    ap.add_argument(
        "--browser-proxy",
        default="",
        help="Camoufox 实际入口；用于本机→Panda→Webshare 链。账号仍绑定 --proxy",
    )
    ap.add_argument(
        "--browser-proxy-file",
        default="",
        help="保存 Camoufox 链式入口的 secret 文件，读取首个非空行",
    )
    ap.add_argument("--exclude-hosts", default="", help="Comma-separated hosts already in use")
    ap.add_argument("--used-hosts-file", default="", help="Optional file of used hosts / proxy URLs")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "runlogs" / "outlook-camoufox-stable"))
    ap.add_argument("--check-only", action="store_true", help="Only pick + probe + mailbox preflight")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    credential = load_outlook_line(Path(args.accounts_file), args.account_index)
    (out_dir / "selected.credentials.secret.txt").write_text(credential["raw_line"] + "\n", encoding="utf-8")
    log("account_selected", email_mask=mask_email(credential["email"]), index=args.account_index)

    exclude = [h.strip() for h in str(args.exclude_hosts or "").split(",") if h.strip()]
    used = load_used_hosts(Path(args.used_hosts_file) if args.used_hosts_file else None, exclude)

    if args.proxy.strip():
        sticky = normalize_proxy(args.proxy)
    elif args.webshare_pool.strip():
        sticky = pick_fresh_proxy(Path(args.webshare_pool), used)
    else:
        raise SystemExit("need --proxy or --webshare-pool")

    host = proxy_host(sticky)
    if host in used:
        raise SystemExit(f"proxy_host_already_used:{host}")
    (out_dir / "selected.proxy.secret.txt").write_text(sticky + "\n", encoding="utf-8")
    log("proxy_selected", endpoint=proxy_endpoint(sticky), host=host)

    if args.browser_proxy.strip() and args.browser_proxy_file.strip():
        raise SystemExit("browser_proxy_conflict: use only one of --browser-proxy/--browser-proxy-file")
    browser_proxy = str(args.browser_proxy or "").strip()
    if args.browser_proxy_file.strip():
        browser_proxy_path = Path(args.browser_proxy_file)
        if not browser_proxy_path.is_file():
            raise SystemExit("browser_proxy_file_missing")
        browser_proxy = next(
            (
                line.strip()
                for line in browser_proxy_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ),
            "",
        )
        if not browser_proxy:
            raise SystemExit("browser_proxy_file_empty")
    browser_proxy = normalize_proxy(browser_proxy) if browser_proxy else sticky
    if browser_proxy != sticky:
        (out_dir / "selected.browser-proxy.secret.txt").write_text(browser_proxy + "\n", encoding="utf-8")
    log(
        "browser_proxy_selected",
        endpoint=proxy_endpoint(browser_proxy),
        chain_mode="panda_webshare_chain" if browser_proxy != sticky else "direct_webshare",
    )

    if args.check_only:
        probe = probe_proxy(browser_proxy)
        log("proxy_probe", **probe)
        if not probe.get("ok"):
            return 3
        try:
            preflight_mailbox(credential, sticky)
            log("mailbox_preflight", ok=True)
        except Exception as exc:  # noqa: BLE001
            log("mailbox_preflight", ok=False, error=f"{type(exc).__name__}:{exc}"[:200])
            return 4
        return 0

    runner = relogin_outlook if args.mode == "relogin" else register_outlook
    out = runner(
        credential=credential,
        sticky_proxy=sticky,
        browser_proxy=browser_proxy,
        out_dir=out_dir,
    )
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
