#!/usr/bin/env python3
"""Login via HTTP NextAuth email-OTP (Clash), inject session cookie, capture HAR.

Reuses chatgpt_session_token from secret when still valid (skip OTP).
Dismisses ChatGPT NUX modals before sending prompts.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402
from scripts._tmp_proton_camoufox_openai_observe import ProtonOtpInbox  # noqa: E402
from scripts.recover_panda_outlook_accounts import login_with_chatgpt_email_otp  # noqa: E402
from services.account_service import account_service  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "docs" / "captures" / "spa"
PROXY = "http://127.0.0.1:7897"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(**kwargs) -> None:
    print(json.dumps(kwargs, ensure_ascii=False), flush=True)


def _proxy_dict(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _http_otp_login(email: str, proton_password: str) -> dict:
    inbox = ProtonOtpInbox(email, proton_password, PROXY)
    for attempt in range(1, 4):
        try:
            inbox.login()
            _log(phase="proton_login_ok", attempt=attempt)
            break
        except Exception as exc:
            _log(phase="proton_login_retry", attempt=attempt, error=str(exc)[:200])
            time.sleep(2)
            inbox = ProtonOtpInbox(email, proton_password, PROXY)
    else:
        raise RuntimeError("proton_login_failed")

    boundary_holder: dict = {"not_before": datetime.now(timezone.utc)}

    def wait_for_code(_mail: dict, _mailbox: dict) -> str | None:
        not_before = _mailbox.get("_code_not_before") or boundary_holder["not_before"]
        nb = not_before if isinstance(not_before, datetime) else boundary_holder["not_before"]
        return inbox.wait_code(not_before=nb, timeout=180)

    def prime_mailbox(_mail: dict, mailbox: dict) -> int:
        boundary_holder["not_before"] = datetime.now(timezone.utc)
        mailbox["_code_not_before"] = boundary_holder["not_before"]
        return 0

    result = login_with_chatgpt_email_otp(
        account_service=account_service,
        email=email,
        mail_config={"wait_timeout": 180},
        mailbox={"address": email, "email": email, "provider": "proton"},
        wait_for_code=wait_for_code,
        prime_mailbox=prime_mailbox,
        otp_attempts=5,
        proxy=PROXY,
    )
    _log(
        phase="http_otp_result",
        ok=result.get("ok"),
        stage=result.get("stage"),
        error=str(result.get("error") or "")[:200],
        has_at=bool(result.get("access_token")),
        has_sess=bool(result.get("chatgpt_session_token")),
    )
    if not result.get("ok"):
        raise RuntimeError(f"http_otp_failed:{result.get('stage')}:{result.get('error')}")
    return result


def _session_user(page) -> dict | None:
    data = page.evaluate(
        """async () => {
          try {
            const r = await fetch('/api/auth/session', {credentials:'include'});
            return {status: r.status, body: await r.json()};
          } catch (e) { return {status:0, error:String(e)}; }
        }"""
    )
    body = (data or {}).get("body") or {}
    user = body.get("user") if isinstance(body, dict) else None
    _log(
        phase="session_probe",
        status=(data or {}).get("status"),
        has_user=bool(user),
        email=(user or {}).get("email") if isinstance(user, dict) else None,
        url=page.url,
    )
    if isinstance(user, dict) and (user.get("email") or user.get("id")):
        return user
    return None


def _dismiss_modals(page) -> None:
    for _ in range(6):
        closed = False
        for sel in (
            '[data-testid="modal-m3m-nux"] button',
            '[data-testid="modal-m3m-nux"] [aria-label="Close"]',
            'button:has-text("Okay")',
            'button:has-text("Got it")',
            'button:has-text("Continue")',
            'button:has-text("Close")',
            'button:has-text("Not now")',
            'button:has-text("Skip")',
            '[role="dialog"] button[aria-label="Close"]',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=2000, force=True)
                    closed = True
                    _log(phase="modal_closed", sel=sel)
                    page.wait_for_timeout(500)
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        # remove intercepting overlay via DOM if still present
        try:
            page.evaluate(
                """() => {
                  const m = document.querySelector('#modal-m3m-nux,[data-testid="modal-m3m-nux"]');
                  if (m) m.remove();
                  document.querySelectorAll('[data-state="open"].fixed.inset-0').forEach(el => {
                    if (el.id === 'modal-m3m-nux' || el.closest('#modal-m3m-nux')) el.remove();
                  });
                }"""
            )
        except Exception:
            pass
        page.wait_for_timeout(400)
        try:
            if page.locator("#modal-m3m-nux, [data-testid='modal-m3m-nux']").count() == 0:
                if not closed:
                    return
                return
        except Exception:
            return


def _send(page, text: str, wait_ms: int) -> None:
    _dismiss_modals(page)
    page.wait_for_selector("#prompt-textarea", timeout=60000)
    loc = page.locator("#prompt-textarea").first
    # force interact even if overlays linger
    try:
        loc.click(timeout=5000, force=True)
    except Exception:
        pass
    tag = loc.evaluate("el => el.tagName.toLowerCase()")
    if tag == "textarea":
        loc.fill(text)
    else:
        page.keyboard.press("Control+A")
        page.keyboard.type(text, delay=10)
    page.keyboard.press("Enter")
    _log(phase="sent", text=text[:80])
    page.wait_for_timeout(wait_ms)


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    email = secret["email"]
    proton_password = secret["proton_password"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _ts()
    har_path = OUT_DIR / f"spa-camoufox-{stamp}.har"

    session_token = str(secret.get("chatgpt_session_token") or "").strip()
    access_token = str(secret.get("access_token") or "").strip()

    launch_kwargs = {"headless": False, "humanize": True, "proxy": _proxy_dict(PROXY), "os": "windows"}
    try:
        browser_cm = Camoufox(**{**launch_kwargs, "geoip": True})
        browser = browser_cm.__enter__()
    except Exception as exc:
        _log(phase="geoip_off", error=str(exc)[:160])
        browser_cm = Camoufox(**launch_kwargs)
        browser = browser_cm.__enter__()

    try:
        context = browser.new_context(record_har_path=str(har_path), record_har_content="embed")
        page = context.new_page()

        user = None
        if session_token:
            context.add_cookies(
                [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": session_token,
                        "domain": "chatgpt.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    _log(phase="goto_retry", attempt=attempt, error=str(exc)[:160])
                    page.wait_for_timeout(2000)
            if last_exc is not None:
                raise last_exc
            page.wait_for_timeout(2000)
            _dismiss_modals(page)
            user = _session_user(page)
            _log(phase="reuse_session", ok=bool(user))

        if not user:
            _log(phase="need_fresh_http_otp")
            login = _http_otp_login(email, proton_password)
            session_token = str(login.get("chatgpt_session_token") or "").strip()
            access_token = str(login.get("access_token") or "").strip()
            secret["access_token"] = access_token or secret.get("access_token")
            secret["chatgpt_session_token"] = session_token
            secret["http_otp_logged_at"] = datetime.now(timezone.utc).isoformat()
            SECRET.write_text(json.dumps(secret, ensure_ascii=False, indent=2), encoding="utf-8")
            context.add_cookies(
                [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": session_token,
                        "domain": "chatgpt.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(2000)
            _dismiss_modals(page)
            user = _session_user(page)

        if not user:
            raise RuntimeError("refuse_send_without_login")

        _log(phase="login_ok", email=user.get("email"))
        _dismiss_modals(page)
        page.wait_for_selector("#prompt-textarea", timeout=60000)

        # text
        _log(phase="text", as_email=user.get("email"))
        _send(page, "Reply with exactly: PONG", wait_ms=20000)
        _dismiss_modals(page)

        # image
        _log(phase="image")
        try:
            btn = page.get_by_role("button", name=re.compile("New chat", re.I))
            if btn.count() > 0:
                btn.first.click(timeout=5000, force=True)
                page.wait_for_timeout(1200)
        except Exception:
            pass
        _dismiss_modals(page)
        _send(page, "Generate a simple flat blue circle icon, no text", wait_ms=75000)

        meta = {
            "har": str(har_path),
            "email": email,
            "session_email": user.get("email"),
            "proxy": PROXY,
            "url": page.url,
            "captured_at": stamp,
            "logged_in": True,
            "login_method": "http_otp_cookie_inject",
            "has_access_token": bool(access_token),
        }
        (OUT_DIR / f"spa-camoufox-{stamp}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _log(phase="closing", **meta)
        context.close()
    finally:
        browser_cm.__exit__(None, None, None)

    size = har_path.stat().st_size if har_path.exists() else 0
    _log(phase="done", har=str(har_path), bytes=size)
    return 0 if size > 1000 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _log(phase="fatal", error=str(exc))
        raise
