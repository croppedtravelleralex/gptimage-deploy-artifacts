#!/usr/bin/env python3
"""Single Outlook signup via Camoufox + sticky Webshare. Updates EXECUTION_LOG.md."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402

from services.register import mail_provider  # noqa: E402
from services.register.real_browser_register import (  # noqa: E402
    generate_openai_account_password,
    mask_email,
)
import scripts.yumail_camoufox_openai_register as cam  # noqa: E402

OBS = ROOT / "data/runlogs/single-register-observe-20260717"
LOG = OBS / "EXECUTION_LOG.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_log(section: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write("\n" + section.rstrip() + "\n")
    print(section, flush=True)


def proxy_endpoint(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return f"{parsed.hostname}:{parsed.port}"


def proxy_hash(url: str) -> str:
    return hashlib.sha256(proxy_endpoint(url).encode()).hexdigest()[:12]


def outlook_mail_config(credential: dict[str, str], proxy: str) -> dict[str, Any]:
    email = str(credential.get("email") or "").strip().lower()
    domain = email.split("@", 1)[-1] if "@" in email else ""
    imap_host = "outlook.live.com" if domain in {"outlook.com", "hotmail.com", "live.com"} else "outlook.office365.com"
    return {
        "request_timeout": 45,
        "wait_timeout": 180,
        "wait_interval": 3,
        "otp_backend": "provider",
        # Outlook 直连收信；Camoufox 浏览器本身走 Webshare
        "api_use_register_proxy": False,
        "proxy": "",
        "providers": [
            {
                "type": "outlook_token",
                "enable": True,
                "label": "CamoufoxWebshareObserve",
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
    try:
        sess = crequests.Session(impersonate="chrome", proxies={"http": proxy, "https": proxy}, timeout=25)
        r = sess.get("https://www.cloudflare.com/cdn-cgi/trace")
        text = r.text or ""
        out["http"] = r.status_code
        out["warp"] = "warp=on" in text
        for line in text.splitlines():
            if line.startswith("ip=") or line.startswith("loc=") or line.startswith("colo="):
                k, _, v = line.partition("=")
                out[k] = v
        out["ok"] = r.status_code == 200 and bool(out.get("ip"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _collect_chatgpt_session(page: Any, proxy: str) -> dict[str, str]:
    """Read the NextAuth session created by a successful ChatGPT callback."""
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
                session_token = str(body.get("sessionToken") or session_token or "").strip()
                cam._log(
                    "chatgpt_session_cookie_handoff",
                    status=status,
                    cookie_count=len(browser_cookies),
                    has_at=bool(access_token),
                )
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            cam._log("chatgpt_session_cookie_handoff_warn", error=f"{type(exc).__name__}: {exc}"[:180])
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


def _collect_tokens(
    page: Any,
    captured: dict[str, Any],
    code_verifier: str,
    proxy: str,
    *,
    chatgpt_session: bool = False,
) -> dict[str, str]:
    """Prefer network/session capture; otherwise PKCE-exchange a platform callback."""
    tokens = dict(captured.get("tokens") or {})
    if chatgpt_session and not tokens.get("access_token"):
        tokens.update(_collect_chatgpt_session(page, proxy))
    if tokens.get("access_token") and chatgpt_session:
        return tokens
    url = page.url or ""
    if not tokens.get("refresh_token") and "code=" in url:
        cam._log("token_exchange_start", url=url.split("?")[0][-80:])
        exchanged = cam._exchange_callback_code(page, code_verifier, proxy)
        if exchanged.get("refresh_token"):
            tokens = exchanged
            cam._log("token_exchange_ok", has_rt=True)
    if not tokens.get("refresh_token"):
        tokens = cam._wait_for_refresh_token(captured, timeout_sec=75)
    if not tokens.get("refresh_token"):
        page.wait_for_timeout(4000)
        # callback SPA may rewrite URL; re-check once
        url = page.url or ""
        if "code=" in url:
            exchanged = cam._exchange_callback_code(page, code_verifier, proxy)
            if exchanged.get("refresh_token"):
                tokens = exchanged
        if not tokens.get("refresh_token"):
            tokens = cam._wait_for_refresh_token(captured, timeout_sec=30)
    return tokens


def _prepare_chatgpt_nextauth(email: str, proxy: str) -> tuple[str, list[dict[str, Any]]]:
    """Create the NextAuth state cookie and matching OpenAI authorize URL."""
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

        signin_query = {
            "prompt": "login",
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        signin_response = session.post(
            "https://chatgpt.com/api/auth/signin/openai?" + urlencode(signin_query),
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

        allowed_prefixes = ("__Host-next-auth.", "__Secure-next-auth.")
        cookies: list[dict[str, Any]] = []
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


def _fill_about_you_robust(page: Any, email: str) -> None:
    """Handle current OpenAI about-you (Full name + Age + Finish creating account)."""
    import re

    page.wait_for_timeout(800)
    cam._assert_not_cf_blocked(page)
    name = cam._signup_name(email)
    for sel in (
        'input[name="name"]:visible',
        'input[autocomplete="name"]:visible',
        'input[placeholder*="name" i]:visible',
        'input[placeholder*="Name" i]:visible',
        'input[placeholder*="ім" i]:visible',
        'input[placeholder*="Имя" i]:visible',
        'input[placeholder*="név" i]:visible',
        'form input[type="text"]:visible',
    ):
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                loc.first.click(force=True, timeout=3000)
            except Exception:
                pass
            loc.first.fill(name, force=True)
            break
    filled_age = False
    for sel in (
        'input[name="birthday"]:visible',
        'input[name="age"]:visible',
        'input[autocomplete="bday-year"]:visible',
        'input[inputmode="numeric"]:visible',
        'input[type="number"]:visible',
        'input[placeholder*="Вік" i]:visible',
        'input[placeholder*="Age" i]:visible',
        'input[placeholder*="возраст" i]:visible',
    ):
        loc = page.locator(sel)
        if loc.count() == 0:
            continue
        target = loc.first
        try:
            target.click(force=True, timeout=3000)
        except Exception:
            pass
        try:
            target.fill("30", force=True)
        except Exception:
            target.evaluate(
                """(el) => {
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                  setter.call(el, '30');
                  el.dispatchEvent(new Event('input', {bubbles:true}));
                  el.dispatchEvent(new Event('change', {bubbles:true}));
                }"""
            )
        filled_age = True
        break
    if not filled_age:
        cam._fill_about_you(page, email)
        return
    page.wait_for_timeout(600)
    clicked = False
    for label in (
        "Finish creating account",
        "完成创建账户",
        "Create account",
        "Finish",
        "Continue",
        "继续",
        # UA/RU/HU locales
        "Завершити створення облікового запису",
        "Завершити створення",
        "Завершить создание аккаунта",
        "Завершить создание",
        "Fiók létrehozásának befejezése",
        "Завершити",
        "Завершить",
    ):
        btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
        if btn.count() == 0:
            btn = page.locator(f'button:has-text("{label}"), [role="button"]:has-text("{label}")')
        if btn.count() == 0:
            continue
        for attempt in range(1, 4):
            try:
                if attempt == 1:
                    btn.first.click(timeout=10000, force=True)
                elif attempt == 2:
                    btn.first.evaluate("el => el.click()")
                else:
                    page.keyboard.press("Enter")
                clicked = True
                break
            except Exception:
                page.wait_for_timeout(500)
        if clicked:
            break
    if not clicked:
        # last-resort: button text contains finish/create/заверш across locales
        loose = re.compile(r"(finish|create account|完成创建|заверш|fiók létrehoz)", re.I)
        buttons = page.get_by_role("button")
        try:
            n = buttons.count()
        except Exception:
            n = 0
        for i in range(min(n, 20)):
            try:
                txt = str(buttons.nth(i).inner_text(timeout=800) or "")
            except Exception:
                continue
            if not loose.search(txt):
                continue
            try:
                buttons.nth(i).click(force=True, timeout=10000)
                clicked = True
                break
            except Exception:
                try:
                    buttons.nth(i).evaluate("el => el.click()")
                    clicked = True
                    break
                except Exception:
                    continue
    if not clicked:
        raise RuntimeError(f"finish_account_button_missing {cam._page_error_snippet(page)}")
    page.wait_for_timeout(1500)
    try:
        page.wait_for_function(
            "() => !(location.pathname || '').includes('about-you')",
            timeout=60000,
        )
    except Exception:
        pass
    cam._raise_if_auth_or_ban_page(page, where="after_about_you")
    cam._log("about_you_done", path=cam._page_path(page), title=page.title())


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("register", "relogin"), default="relogin")
    parser.add_argument("--credentials-file", default="")
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--proxy-file", default="")
    parser.add_argument("--chain-proxy-file", default="")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    mode = args.mode

    global OBS, LOG
    if args.out_dir:
        OBS = Path(args.out_dir).expanduser().resolve()
        LOG = OBS / "EXECUTION_LOG.md"
    cred_path = (
        Path(args.credentials_file).expanduser().resolve()
        if args.credentials_file
        else OBS / "browser.selected.credentials.secret.txt"
    )
    proxy_path = (
        Path(args.proxy_file).expanduser().resolve()
        if args.proxy_file
        else OBS / "browser.selected.proxy.secret.txt"
    )
    chain_path = (
        Path(args.chain_proxy_file).expanduser().resolve()
        if args.chain_proxy_file
        else OBS / "browser.selected.chain_proxy.secret.txt"
    )
    if not cred_path.exists() or not proxy_path.exists():
        append_log("## Step 7a — 缺凭据/代理文件\n- 结果：**failed**\n")
        return 2

    credential_lines = [
        line.strip()
        for line in cred_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        if line.strip()
    ]
    if args.account_index < 0 or args.account_index >= len(credential_lines):
        append_log(
            "## Step 7a — 凭据行号越界\n"
            f"- account_index：`{args.account_index}`\n"
            f"- line_count：`{len(credential_lines)}`\n"
            "- 结果：**failed**\n"
        )
        return 2
    raw = credential_lines[args.account_index]
    parts = raw.split("----")
    if len(parts) < 4:
        append_log("## Step 7a — 凭据格式错误\n- 结果：**failed**\n")
        return 2
    credential = {
        "email": parts[0].strip(),
        "password": parts[1].strip(),
        "client_id": parts[2].strip(),
        "refresh_token": parts[3].strip(),
    }
    sticky_proxy = proxy_path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0].strip()
    # Camoufox 出口：优先本机链式入口（本机 -> SSH -> Panda -> Webshare）
    if chain_path.exists():
        proxy = chain_path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0].strip()
        chain_mode = "panda_webshare_chain"
    else:
        proxy = sticky_proxy
        chain_mode = "direct_webshare"
    email = credential["email"].strip().lower()
    mail = outlook_mail_config(credential, sticky_proxy)

    append_log(
        f"""## Step 11a — Camoufox {mode} 启动前探活（链式）
- 时间：{utc_now()}
- email_mask：`{mask_email(email)}`
- sticky_proxy_hash：`{proxy_hash(sticky_proxy)}`
- sticky_endpoint：`{proxy_endpoint(sticky_proxy)}`
- browser_proxy：`{proxy_endpoint(proxy) if '://' in proxy else proxy}`
- chain_mode：`{chain_mode}`
- mode：`{mode}`
- 说明：本机 Camoufox 经 Panda 转发再出 Webshare；收信仍直连 Outlook
"""
    )
    probe = probe_proxy(proxy)
    append_log(f"- proxy_probe：`{json.dumps(probe, ensure_ascii=False)}`\n")
    if not probe.get("ok"):
        append_log("- 结果：**proxy_probe_failed**\n")
        return 3

    # mailbox preflight
    try:
        from scripts.recover_panda_outlook_accounts import build_mailbox, preflight_mailbox_access, prime_mailbox_messages

        boundary = datetime.now(timezone.utc)
        mailbox = build_mailbox(credential, boundary)
        preflight_mailbox_access(mail, mailbox)
        if mailbox.get("_outlook_imap_host"):
            mail["providers"][0]["imap_host"] = mailbox["_outlook_imap_host"]
        # 半成品号常不重发 OTP：禁止 prime 把收件箱现有验证码标成已看，否则会空等
        mailbox.pop("_seen_code_message_refs", None)
        append_log(f"- mailbox_preflight：**ok** imap={mail['providers'][0].get('imap_host')} (no_prime_seen)\n")
    except Exception as exc:  # noqa: BLE001
        append_log(f"- mailbox_preflight：**failed** `{type(exc).__name__}: {exc}`\n- 结果：**failed**\n")
        return 4

    openai_password = generate_openai_account_password()
    # relogin prefers chatgpt client (OTP-first); register uses platform PKCE
    oauth_client = "chatgpt" if mode == "relogin" else "platform"
    screen_hint = "login" if mode == "relogin" else "signup"
    if mode == "relogin":
        authorize_url, code_verifier = "", ""
    else:
        authorize_url, code_verifier = cam._authorize_url(email, screen_hint=screen_hint, client=oauth_client)
    result: dict[str, Any] = {
        "ok": False,
        "email_mask": mask_email(email),
        "proxy_hash": proxy_hash(sticky_proxy),
        "sticky_endpoint": proxy_endpoint(sticky_proxy),
        "browser_proxy": proxy_endpoint(proxy) if "://" in proxy else "configured",
        "chain_mode": chain_mode,
        "engine": "camoufox",
        "mode": mode,
        "oauth_client": oauth_client,
    }
    report_dir = OBS / f"camoufox-{mode}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    report_dir.mkdir(parents=True, exist_ok=True)

    append_log(
        f"""## Step 7b — 启动 Camoufox {mode}
- 时间：{utc_now()}
- report_dir：`{report_dir}`
- oauth_client：`{oauth_client}`
- 状态：running
"""
    )

    proxy_cfg = cam._proxy_dict(proxy)
    launch_kwargs: dict[str, Any] = {"headless": False, "os": "windows", "humanize": True}
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        # 恢复任务不依赖 Camoufox GeoIP。该探针失败后同进程二次初始化
        # Playwright 会遗留半初始化对象，表现为 `_playwright` 缺失。

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
            if mode == "relogin":
                authorize_url, nextauth_cookies = _prepare_chatgpt_nextauth(email, proxy)
                page.context.add_cookies(nextauth_cookies)
                append_log(
                    f"- nextauth_prepared：**ok** cookies={len(nextauth_cookies)} "
                    "state_cookie=true\n"
                )
            captured = cam._attach_token_capture(page)
            # 半成品账号常直接进 email-verification 且不重发信；
            # 若 boundary=goto 时刻，会把上一轮已在收件箱的 OTP 滤掉，表现为“你能看见信、脚本一直不填”。
            page_boundary = datetime.now(timezone.utc)
            # 新的 relogin 会主动触发新 OTP，只允许本轮附近的邮件，避免连续
            # canary 时捞到上一轮验证码。register 半成品仍保留 30 分钟回看。
            otp_boundary = (
                page_boundary - timedelta(seconds=60)
                if mode == "relogin"
                else page_boundary - timedelta(minutes=30)
            )
            boundary = otp_boundary
            mailbox.pop("_seen_code_message_refs", None)
            mailbox["_code_not_before"] = otp_boundary
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=120000)
            cam._wait_transition(page, timeout_ms=90000)
            cam._assert_not_cf_blocked(page)
            page.wait_for_timeout(1500)
            path = cam._page_path(page)
            cam._log("authorized", path=path, title=page.title(), mode=mode)
            append_log(
                f"- authorized path=`{path}` title=`{page.title()}` "
                f"otp_lookback_min=30 page_boundary=`{page_boundary.isoformat()}`\n"
            )

            if mode == "relogin":
                # chatgpt 常直接 OTP；也可能先密码页
                if "email-verification" in path:
                    cam._fill_otp(page, mailbox, mail, boundary)
                    path = cam._page_path(page)
                    append_log(f"- otp_done path=`{path}`\n")
                    cam._assert_not_cf_blocked(page)
                if "password" in path or "log-in" in path:
                    # 注册时生成的 OpenAI 密码已丢失；若卡在密码页，点 OTP 登录分支
                    try:
                        cam._switch_to_otp_signup(page)
                        path = cam._page_path(page)
                        append_log(f"- switched_to_otp_login path=`{path}`\n")
                    except Exception:
                        pass
                    if "email-verification" in path:
                        boundary = datetime.now(timezone.utc)
                        cam._fill_otp(page, mailbox, mail, boundary)
                        path = cam._page_path(page)
                        append_log(f"- otp_done_after_password_gate path=`{path}`\n")
                if "email-verification" in path:
                    boundary = datetime.now(timezone.utc)
                    cam._fill_otp(page, mailbox, mail, boundary)
                    path = cam._page_path(page)
                    append_log(f"- otp_done_final path=`{path}`\n")
            else:
                if path.rstrip("/") == "/create-account/password":
                    boundary = datetime.now(timezone.utc)
                    cam._switch_to_otp_signup(page)
                    path = cam._page_path(page)
                    append_log(f"- switched_to_otp_signup path=`{path}`\n")
                    cam._assert_not_cf_blocked(page)

                if "email-verification" in path:
                    cam._fill_otp(page, mailbox, mail, boundary)
                    path = cam._page_path(page)
                    append_log(f"- otp_done path=`{path}`\n")
                    cam._assert_not_cf_blocked(page)

                if path.rstrip("/") == "/create-account/password":
                    cam._fill_password(page, openai_password)
                    path = cam._page_path(page)
                    append_log(f"- password_done path=`{path}`\n")
                    cam._assert_not_cf_blocked(page)

                if "about-you" in path:
                    _fill_about_you_robust(page, email)
                    path = cam._page_path(page)
                    append_log(f"- about_you_done path=`{path}`\n")

            # wait for callback / tokens — 禁止仅凭 /auth/callback path 提前结束（上次因此丢 code）
            finished = False
            last_url = ""
            for _ in range(60):
                path = cam._page_path(page)
                url = page.url or ""
                last_url = url
                if "code=" in url or captured.get("tokens", {}).get("refresh_token"):
                    finished = True
                    break
                if mode == "relogin" and urlparse(url).hostname == "chatgpt.com" and path == "/":
                    session_tokens = _collect_chatgpt_session(page, proxy)
                    if session_tokens.get("access_token"):
                        captured.setdefault("tokens", {}).update(session_tokens)
                        finished = True
                        break
                page.wait_for_timeout(1500)
            has_code = "code=" in (last_url or "")
            append_log(
                f"- settle path=`{cam._page_path(page)}` has_code={has_code} "
                f"events={len(captured.get('events') or [])} "
                f"url_tail=`{(last_url.split('?')[0] if last_url else '')[-80:]}`\n"
            )
            if not finished:
                raise RuntimeError(
                    f"registration_incomplete_path_{path}_url_{(page.url or '').split('?', 1)[0][:200]}"
                )

            tokens = _collect_tokens(
                page,
                captured,
                code_verifier,
                proxy,
                chatgpt_session=mode == "relogin",
            )
            if not tokens.get("access_token") or (mode == "register" and not tokens.get("refresh_token")):
                raise RuntimeError(
                    "login_token_missing "
                    f"url={(page.url or '').split('?', 1)[0][:160]} events={captured.get('events')[-8:]}"
                )

            add = cam._persist_account(
                email=email,
                password=openai_password if mode == "register" else "",
                tokens=tokens,
                proxy=sticky_proxy,
                source_detail=f"outlook_camoufox_panda_chain_{mode}_observe_20260717",
            )
            token = str(tokens.get("access_token") or "")
            th = hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""

            # isolate for observation
            from services.account_service import account_service

            try:
                account_service.update_account_identity(
                    token,
                    {
                        "panda_receive_state": "identity_isolated",
                        "identity_last_error": "single_register_observe_hold_until_mature",
                        "identity_evidence_state": "single_register_observe",
                        "proxy_egress_ip": probe.get("ip"),
                        "proxy_egress_hash": hashlib.sha256(str(probe.get("ip") or "").encode()).hexdigest()[:16]
                        if probe.get("ip")
                        else "",
                        "register_egress_ip": probe.get("ip"),
                        "lifecycle_ip_mode": "sticky_one_ip_full",
                        "proxy_provider": "webshare",
                        "proxy_scope": "account_sticky",
                    },
                    reason="single_register_observe_20260717",
                    quiet=True,
                    clear_isolation=False,
                )
            except Exception as exc:  # noqa: BLE001
                account_service.update_account(
                    token,
                    {
                        "panda_receive_state": "identity_isolated",
                        "identity_last_error": "single_register_observe_hold_until_mature",
                        "identity_update_reason": "single_register_observe_20260717",
                    },
                    quiet=True,
                )
                append_log(f"- identity_update_warn：`{type(exc).__name__}: {exc}`\n")

            account_service.reload_from_storage()
            refreshed = account_service.get_account(token) or {}
            baseline = {
                "ts": utc_now(),
                "email_mask": mask_email(email),
                "token_hash": th,
                "status": refreshed.get("status"),
                "quota": refreshed.get("quota"),
                "panda_receive_state": refreshed.get("panda_receive_state"),
                "proxy_hash": proxy_hash(sticky_proxy),
                "proxy_egress_ip": refreshed.get("proxy_egress_ip") or probe.get("ip"),
                "fp_hash": (refreshed.get("fp_hash") or "")[:16],
                "mode": mode,
                "chain_mode": chain_mode,
                "add": {k: add.get(k) for k in ("added", "updated", "total", "count") if k in add},
            }
            (OBS / "observe-baseline.json").write_text(
                json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (report_dir / "result.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "email_mask": mask_email(email),
                        "token_hash": th,
                        "proxy_hash": proxy_hash(sticky_proxy),
                        "chain_mode": chain_mode,
                        "path": path,
                        "mode": mode,
                        "probe": probe,
                        "baseline": baseline,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            result.update({"ok": True, "token_hash": th, "path": path, "baseline": baseline})
            append_log(
                f"""## Step 7c — {mode} 成功并本地隔离入库
- 时间：{utc_now()}
- baseline：`{json.dumps(baseline, ensure_ascii=False)}`
- 约束：`panda_receive_state=identity_isolated`；未自动上传 Panda
- 结果：**ok**
"""
            )
            return 0
        finally:
            browser_cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        append_log(
            f"""## Step 7c — {mode} 失败
- 时间：{utc_now()}
- error：`{result['error']}`
- report：`{report_dir / 'result.json'}`
- 结果：**failed**
"""
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
