#!/usr/bin/env python3
"""Acquire existing yumail pool mailbox → OpenAI signup via Camoufox (anti-detect Firefox).

Does NOT call yumail /pool/register. Selenium Chrome 会被 CF 拦；改用 Camoufox。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402

from services.register import mail_provider  # noqa: E402
from services.register.real_browser_register import (  # noqa: E402
    generate_openai_account_password,
    is_registration_transition_path,
    mask_email,
)


def _log(phase: str, **kwargs: Any) -> None:
    print(json.dumps({"phase": phase, **kwargs}, ensure_ascii=False), flush=True)


def _mail_config() -> dict[str, Any]:
    import os

    from services import yumail_otp

    return {
        "request_timeout": 45,
        "wait_timeout": 180,
        "wait_interval": 4,
        "api_use_register_proxy": False,
        "providers": [
            {
                "type": "yumail",
                "enable": True,
                "api_base": yumail_otp.resolve_api_base(os.getenv("YUMAIL_API_BASE")),
                "api_key": yumail_otp.resolve_api_key(os.getenv("YUMAIL_API_KEY")),
                "mode": "acquire",
                "acquire_status": "active",
                "acquire_tag": "dispatchable",
                "otp_sender_contains": "openai",
                "otp_subject_contains": "验证码",
            }
        ],
    }


def _signup_name(email: str) -> str:
    local = email.split("@", 1)[0]
    local = re.sub(r"\d+", " ", local)
    local = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", local)
    words = re.findall(r"[A-Za-z]{2,}", local)
    return " ".join(word.capitalize() for word in words[:3]) or "Alex Morgan"


def _proxy_dict(proxy: str) -> dict[str, str] | None:
    raw = str(proxy or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"
    cfg: dict[str, str] = {"server": server}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _authorize_url(email: str, *, screen_hint: str = "signup", client: str = "platform") -> tuple[str, str]:
    device_id = str(uuid.uuid4())
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    if client == "chatgpt":
        url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(
            {
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "audience": "https://api.openai.com/v1",
                "redirect_uri": "https://chatgpt.com/api/auth/callback/openai",
                "response_type": "code",
                "scope": "openid email profile offline_access model.request model.read organization.read organization.write",
                "prompt": "login",
                "screen_hint": screen_hint,
                "login_hint": email,
                "device_id": device_id,
                "ext-oai-did": device_id,
                "state": str(uuid.uuid4()),
                "ui_locales": "en-US",
            }
        )
        return url, verifier
    url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(
        {
            "issuer": "https://auth.openai.com",
            "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
            "audience": "https://api.openai.com/v1",
            "redirect_uri": "https://platform.openai.com/auth/callback",
            "device_id": device_id,
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "auth0Client": "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9",
            "ui_locales": "en-US",
        }
    )
    return url, verifier


def _page_path(page: Any) -> str:
    return urlparse(page.url or "").path


def _assert_not_cf_blocked(page: Any) -> None:
    title = (page.title() or "").lower()
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "")[:500]
    except Exception:
        pass
    body_l = body.lower()
    if "challenges.cloudflare.com" in body_l or "正在进行安全验证" in body:
        raise RuntimeError(f"cloudflare_challenge title={page.title()!r} body={body[:180]!r}")
    if "糟糕，出错了" in body or "route error" in body_l:
        raise RuntimeError(f"openai_route_error title={page.title()!r} body={body[:180]!r}")
    if "抱歉" in (page.title() or "") and "openai" in title:
        raise RuntimeError(f"openai_error_page title={page.title()!r}")


def _wait_transition(page: Any, timeout_ms: int = 60000) -> None:
    page.wait_for_function(
        """() => {
          const p = location.pathname || '';
          return p.includes('create-account') || p.includes('email-verification')
            || p.includes('about-you') || p.includes('auth/callback')
            || p.includes('log-in') || p.includes('consent');
        }""",
        timeout=timeout_ms,
    )


def _click_continue(page: Any) -> None:
    # 出口 geo 会把 OpenAI UI 切到 UA/ES/…，不能只认英文 Continue
    labels = (
        "继续",
        "Continue",
        "确认",
        "Verify",
        "Next",
        "Продовжити",  # UA
        "Продолжить",  # RU
        "Continuar",
        "Continuer",
        "Weiter",
        "Suivant",
        "Avanti",
        "Dalej",
        "Tovább",
    )
    for label in labels:
        btn = page.get_by_role("button", name=re.compile(label, re.I))
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click(timeout=10000)
            return
    buttons = page.locator("button:visible")
    keys = (
        "continue",
        "继续",
        "确认",
        "next",
        "verify",
        "продовж",
        "продолж",
        "continuar",
        "weiter",
        "suivant",
    )
    for i in range(buttons.count()):
        text = (buttons.nth(i).inner_text() or "").strip().lower()
        if any(k in text for k in keys):
            buttons.nth(i).click(timeout=10000)
            return
    # 最后兜底：主 CTA 常为 type=submit
    submit = page.locator('button[type="submit"]:visible')
    if submit.count() > 0:
        submit.first.click(timeout=10000)
        return
    raise RuntimeError("continue_button_missing")


def _switch_to_otp_signup(page: Any) -> None:
    """密码页改走「使用一次性验证码注册 / Sign up with a one-time code」。"""
    _log("otp_signup_switch", path=_page_path(page), title=page.title())
    clicked = False
    patterns = (
        r"使用一次性验证码注册",
        r"一次性验证码",
        r"Sign up with a one-time code",
        r"one-time code",
        r"Continue with email",
        r"用邮箱.*验证码",
        # UA/RU/HU/other locales (egress geo often flips OpenAI UI language)
        r"Зареєструватися за допомогою одноразового коду",
        r"одноразового коду",
        r"одноразового кода",
        r"одноразов",
        r"Bejelentkezés egyszeri kóddal",
        r"egyszeri kóddal",
        r"egyszeri kód",
        r"Zarejestruj się za pomocą jednorazowego kodu",
        r"jednorazowego kodu",
        r"jednorazow",
        r"código de un solo uso",
        r"einmaligen Code",
        r"code à usage unique",
        r"S'inscrire avec un code",
        r"inscrire avec un code",
        r"utiliser un code",
        r"kóddal",
        r"кодом",
        r"коду",
        r"kodu",
    )
    for pat in patterns:
        loc = page.get_by_role("button", name=re.compile(pat, re.I))
        if loc.count() == 0:
            loc = page.get_by_role("link", name=re.compile(pat, re.I))
        if loc.count() == 0:
            loc = page.locator(f"text=/{pat}/i")
        if loc.count() > 0:
            try:
                loc.first.click(force=True, timeout=10000)
                clicked = True
                break
            except Exception:
                try:
                    loc.first.evaluate("el => el.click()")
                    clicked = True
                    break
                except Exception:
                    continue
    if not clicked:
        # last-resort: any visible control whose text mentions OTP/code across locales
        loose = re.compile(
            r"(one[- ]?time|otp|验证码|одноразов|código|code à usage|einmalig|egyszeri|kóddal|\bkód\b|код[ауом]?|jednorazow|\bkodu\b)",
            re.I,
        )
        for role in ("button", "link"):
            locs = page.get_by_role(role)
            try:
                n = locs.count()
            except Exception:
                n = 0
            for i in range(min(n, 30)):
                try:
                    txt = str(locs.nth(i).inner_text(timeout=800) or "")
                except Exception:
                    continue
                if loose.search(txt):
                    try:
                        locs.nth(i).click(force=True, timeout=10000)
                        clicked = True
                        break
                    except Exception:
                        continue
            if clicked:
                break
    if not clicked:
        raise RuntimeError(f"otp_signup_link_missing {_page_error_snippet(page)}")
    try:
        page.wait_for_function(
            "() => (location.pathname || '').includes('email-verification')",
            timeout=60000,
        )
    except Exception:
        _assert_not_cf_blocked(page)
        _raise_if_auth_or_ban_page(page, where="otp_signup_switch")
        raise RuntimeError(f"otp_signup_switch_stuck {_page_error_snippet(page)}")
    _log("otp_signup_ready", path=_page_path(page), title=page.title())


def _fill_password(page: Any, password: str) -> None:
    # 已在密码页时不要再 wait_for_url（Camoufox/慢代理下偶发空等 90s）
    if "/create-account/password" not in (page.url or ""):
        page.wait_for_url(re.compile(r".*/create-account/password"), timeout=90000)
    _assert_not_cf_blocked(page)
    page.wait_for_timeout(1200)
    _log("password_fill_start", path=_page_path(page), title=page.title())
    pwd = page.locator(
        'input[type="password"]:visible, input[name="new-password"]:visible, input[autocomplete="new-password"]:visible'
    ).first
    pwd.wait_for(state="attached", timeout=20000)
    # React Aria：强制 focus，原生写入，避免 label 挡 click / type 不稳定
    pwd.evaluate(
        """(el, value) => {
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(el, value);
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true}));
        }""",
        password,
    )
    page.wait_for_timeout(400)
    try:
        current = pwd.input_value(timeout=3000) or ""
    except Exception:
        current = ""
    if current != password:
        try:
            pwd.click(force=True, timeout=5000)
        except Exception:
            pass
        pwd.fill(password, force=True)
        page.wait_for_timeout(300)
    _log("password_filled", length=len(password), value_len=len(pwd.input_value() or ""))

    # Continue 也可能被 overlay 挡住
    for attempt in range(1, 5):
        clicked = False
        for label in ("继续", "Continue"):
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() > 0:
                try:
                    btn.first.click(force=True, timeout=10000)
                    clicked = True
                    break
                except Exception:
                    try:
                        btn.first.evaluate("el => el.click()")
                        clicked = True
                        break
                    except Exception:
                        continue
        if not clicked:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
        try:
            page.wait_for_function(
                "() => !(location.pathname || '').includes('/create-account/password')",
                timeout=30000,
            )
            _log("password_nav_ok", path=_page_path(page), attempt=attempt)
            return
        except Exception:
            _assert_not_cf_blocked(page)
            _raise_if_auth_or_ban_page(page, where="after_signup_password")
            body = ""
            try:
                body = (page.locator("body").inner_text(timeout=2000) or "").lower()
            except Exception:
                pass
            if any(
                k in body
                for k in (
                    "password is too short",
                    "password too short",
                    "choose a stronger",
                    "密码过短",
                    "密码太弱",
                    "不符合要求",
                )
            ):
                raise RuntimeError(f"password_rejected {_page_error_snippet(page)}")
            # 注意：页面常驻 “At least 12 characters” 提示，不能当失败条件
            _log("password_retry", attempt=attempt, path=_page_path(page))
            page.wait_for_timeout(1200)
    raise RuntimeError(f"password_submit_stuck {_page_error_snippet(page)}")


def _fill_otp(page: Any, mailbox: dict[str, Any], mail: dict[str, Any], boundary: datetime) -> None:
    _log("otp_wait", email=mask_email(mailbox.get("address")))
    mailbox = dict(mailbox)
    mailbox["_code_not_before"] = boundary
    box: dict[str, Any] = {"code": None, "error": None}
    addr = str(mailbox.get("address") or mailbox.get("email") or "").strip().lower()

    def _poll() -> None:
        try:
            # Outlook/Hotmail/Live 默认走 mailManage；显式 provider 模式用于
            # 已完成 Graph/IMAP 预检的恢复任务，避免本机 YuMail 不可达时误阻断。
            use_mail_provider = str(mail.get("otp_backend") or "").strip().lower() == "provider"
            if addr.endswith(("@outlook.com", "@hotmail.com", "@live.com")) and not use_mail_provider:
                from services import yumail_otp

                box["code"] = yumail_otp.wait_for_code_by_email(
                    addr,
                    not_before=boundary,
                    timeout_sec=float(mail.get("wait_timeout") or 180),
                    poll_interval=float(mail.get("wait_interval") or 4),
                )
            else:
                box["code"] = mail_provider.wait_for_code(mail, mailbox)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=_poll, daemon=True)
    worker.start()
    deadline = time.time() + max(60, int(mail.get("wait_timeout") or 180))
    # 等待期间少碰 page：频繁 title()/path 在 Camoufox+代理下易把 context 弄挂。
    while worker.is_alive() and time.time() < deadline:
        worker.join(timeout=3.0)
    if worker.is_alive():
        raise RuntimeError("yumail_otp_wait_timeout")
    if box["error"] is not None:
        raise RuntimeError(f"yumail_otp_error: {box['error']}")
    code = str(box["code"] or "").strip()
    if not re.fullmatch(r"\d{4,8}", code):
        raise RuntimeError("yumail_otp_invalid")
    _log("otp_got", digits=len(code))

    # 填码前确认页面仍在
    try:
        _assert_not_cf_blocked(page)
        if "email-verification" not in (page.url or ""):
            _log("otp_page_left_before_fill", path=_page_path(page), title=page.title())
            return
    except Exception as exc:
        raise RuntimeError(f"otp_page_dead_before_fill: {exc}") from exc
    # 填码：优先真实按键（触发 React）；DOM 赋值作兜底
    targets: list[Any] = []
    for candidate in (
        page.get_by_label(re.compile(r"^code$|验证码|verification code", re.I)),
        page.get_by_role("textbox", name=re.compile(r"code|验证码", re.I)),
        page.locator(
            'input[autocomplete="one-time-code"]:visible, input[name="code"]:visible, '
            'input[inputmode="numeric"]:visible, input[aria-label*="Code" i]:visible'
        ),
        page.locator("input:visible"),
    ):
        try:
            if candidate.count() > 0:
                targets.append(candidate.first)
                break
        except Exception:
            continue
    filled = False
    if targets:
        target = targets[0]
        otp_cells = page.locator(
            'input[autocomplete="one-time-code"]:visible, input[name="code"]:visible, input[inputmode="numeric"]:visible'
        )
        maxlen = ""
        try:
            maxlen = target.get_attribute("maxlength") or ""
        except Exception:
            pass
        try:
            if maxlen == "1" and otp_cells.count() >= 6:
                for i, digit in enumerate(code[:6]):
                    cell = otp_cells.nth(i)
                    cell.click(force=True, timeout=5000)
                    cell.press_sequentially(digit, delay=50)
                filled = True
            else:
                target.click(force=True, timeout=8000)
                try:
                    target.fill("")
                except Exception:
                    pass
                target.press_sequentially(code, delay=40)
                filled = True
        except Exception as fill_exc:
            _log("otp_type_warn", error=str(fill_exc)[:160])
            target.evaluate(
                """(el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    el.focus();
                    setter.call(el, '');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                }""",
                code,
            )
            filled = True
    if not filled:
        raise RuntimeError("otp_input_missing")
    _log("otp_filled", digits=len(code))
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass

    # 多数页面填满 6 位会自动跳转
    try:
        page.wait_for_function(
            "() => !(location.pathname || '').includes('email-verification')",
            timeout=12000,
        )
        _raise_if_auth_or_ban_page(page, where="otp_auto_nav")
        return
    except RuntimeError:
        raise
    except Exception:
        pass

    time.sleep(0.4)
    for attempt in range(1, 4):
        try:
            _click_continue(page)
        except Exception as click_exc:
            _log("otp_continue_click_warn", error=str(click_exc)[:160])
            try:
                page.locator(
                    'button[type="submit"]:visible, button:has-text("Continue"):visible, '
                    'button:has-text("Verify"):visible, button:has-text("Continuer"):visible'
                ).first.click(timeout=5000)
            except Exception:
                try:
                    page.locator("button:visible").filter(has_text=re.compile(r"continue|continuer|verify", re.I)).first.evaluate(
                        "el => el.click()"
                    )
                except Exception:
                    pass
        try:
            page.wait_for_function(
                "() => !(location.pathname || '').includes('email-verification')",
                timeout=20000,
            )
            _raise_if_auth_or_ban_page(page, where="after_otp_continue")
            return
        except RuntimeError:
            raise
        except Exception:
            _raise_if_auth_or_ban_page(page, where="after_otp_submit")
            body = ""
            try:
                body = (page.locator("body").inner_text(timeout=2000) or "").lower()
            except Exception:
                pass
            if any(k in body for k in ("incorrect", "invalid", "expired", "无效", "错误")):
                raise RuntimeError(f"wrong_email_otp_code {_page_error_snippet(page)}")
            if attempt < 3:
                time.sleep(1.5)
    raise RuntimeError(f"otp_submit_stuck {_page_error_snippet(page)}")


def _fill_about_you(page: Any, email: str) -> None:
    page.wait_for_url("**/about-you**", timeout=90000)
    _assert_not_cf_blocked(page)
    page.wait_for_timeout(1000)
    name = _signup_name(email)
    name_input = page.get_by_label(re.compile(r"full name|name|姓名", re.I))
    if name_input.count() == 0:
        name_input = page.locator(
            'input[name="name"]:visible, input[autocomplete="name"]:visible, input[placeholder*="name" i]:visible'
        )
    if name_input.count() > 0:
        try:
            name_input.first.click(force=True, timeout=5000)
        except Exception:
            pass
        name_input.first.fill(name, force=True)
        page.wait_for_timeout(300)

    age = page.get_by_label(re.compile(r"^age$|年龄", re.I))
    if age.count() == 0:
        age = page.locator('input[name="age"]:visible')
    if age.count() > 0:
        # React Aria label 会挡住普通 click
        try:
            age.first.click(force=True, timeout=5000)
        except Exception:
            pass
        try:
            age.first.fill("30", force=True)
        except Exception:
            age.first.evaluate(
                """(el) => {
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                  setter.call(el, '30');
                  el.dispatchEvent(new Event('input', {bubbles:true}));
                  el.dispatchEvent(new Event('change', {bubbles:true}));
                }"""
            )
        page.wait_for_timeout(300)
    else:
        # birthday: spinbutton / contenteditable / 独立 month/day/year
        filled = False
        segments = page.locator('[role="spinbutton"]:visible, [contenteditable="true"]:visible')
        values = ["01", "01", "1990"]
        if segments.count() >= 3:
            for i in range(3):
                segments.nth(i).click()
                segments.nth(i).fill(values[i])
            filled = True
        if not filled:
            for sel, val in (
                ('input[name="birthday"]:visible', "1990-01-01"),
                ('input[type="date"]:visible', "1990-01-01"),
                ('input[name="birthdate"]:visible', "1990-01-01"),
            ):
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.fill(val)
                    filled = True
                    break
        if not filled:
            # 下拉 Month/Day/Year
            for label, value in (("Month", "January"), ("Day", "1"), ("Year", "1990")):
                combo = page.get_by_role("combobox", name=re.compile(label, re.I))
                if combo.count() == 0:
                    combo = page.locator(f'select[name*="{label.lower()}" i]:visible')
                if combo.count() > 0:
                    try:
                        combo.first.select_option(label=value)
                        filled = True
                    except Exception:
                        try:
                            combo.first.click()
                            page.get_by_role("option", name=re.compile(f"^{re.escape(value)}$", re.I)).first.click()
                            filled = True
                        except Exception:
                            pass
        if not filled:
            raise RuntimeError(f"about_you_birthday_fields_missing {_page_error_snippet(page)}")

    page.wait_for_timeout(500)
    for attempt in range(1, 5):
        clicked = False
        for label in (
            "完成创建账户",
            "Finish creating account",
            "Create account",
            "Finish",
            "完成",
            "继续",
            "Continue",
            "Confirm",
            "确认",
            "Submit",
        ):
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if btn.count() > 0 and btn.first.is_visible():
                try:
                    if attempt <= 2:
                        btn.first.click(timeout=15000)
                    else:
                        btn.first.evaluate("el => el.click()")
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            # 任意主按钮兜底
            primary = page.locator('button[type="submit"]:visible, button:visible')
            for i in range(min(primary.count(), 6)):
                try:
                    primary.nth(i).click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            loose = page.locator("button:visible").filter(
                has_text=re.compile(r"finish creating account|create account|完成创建|finish", re.I)
            )
            if loose.count() > 0:
                try:
                    loose.first.evaluate("el => el.click()")
                    clicked = True
                except Exception:
                    pass
        if not clicked:
            raise RuntimeError(f"finish_account_button_missing {_page_error_snippet(page)}")
        try:
            page.wait_for_function(
                "() => !(location.pathname || '').includes('/about-you')",
                timeout=60000,
            )
            return
        except Exception:
            _raise_if_auth_or_ban_page(page, where="about_you_submit")
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=2000) or ""
            except Exception:
                pass
            if "try again" in body.lower() or "重试" in body:
                retry = page.get_by_role("button", name=re.compile("try again|重试", re.I))
                if retry.count() > 0:
                    retry.first.click()
                    time.sleep(1)
                    continue
            if attempt >= 4:
                try:
                    shot = ROOT / "data" / "runlogs" / f"about_you_stuck_{int(time.time())}.png"
                    page.screenshot(path=str(shot), full_page=True)
                except Exception:
                    shot = None
                raise RuntimeError(f"about_you_submit_stuck shot={shot} {_page_error_snippet(page)}")
            time.sleep(1.5)
    raise RuntimeError("about_you_retry_exhausted")


def _attach_token_capture(page: Any) -> dict[str, Any]:
    """监听 OAuth/session 响应；拿到 RT（或可用 AT）前不要关浏览器。"""
    captured: dict[str, Any] = {"tokens": {}, "events": []}

    def _on_response(response: Any) -> None:
        try:
            url = str(response.url or "")
            status = int(response.status or 0)
            interesting = (
                "/oauth/token" in url
                or "/api/accounts/oauth/token" in url
                or "/api/auth/session" in url
                or url.rstrip("/").endswith("/session")
            )
            if not interesting:
                return
            captured["events"].append({"url": url[:160], "status": status})
            if status != 200:
                return
            data = response.json()
            if not isinstance(data, dict):
                return
            access = str(
                data.get("access_token")
                or data.get("accessToken")
                or ((data.get("user") or {}) if isinstance(data.get("user"), dict) else {}).get("accessToken")
                or ""
            ).strip()
            refresh = str(data.get("refresh_token") or data.get("refreshToken") or "").strip()
            id_token = str(data.get("id_token") or data.get("idToken") or "").strip()
            if access or refresh:
                merged = dict(captured.get("tokens") or {})
                if access:
                    merged["access_token"] = access
                if refresh:
                    merged["refresh_token"] = refresh
                if id_token:
                    merged["id_token"] = id_token
                captured["tokens"] = merged
                _log(
                    "token_captured",
                    has_at=bool(merged.get("access_token")),
                    has_rt=bool(merged.get("refresh_token")),
                    via=url.split("?")[0][-48:],
                )
        except Exception as exc:  # noqa: BLE001
            captured["events"].append({"error": f"{type(exc).__name__}: {exc}"[:160]})

    page.on("response", _on_response)
    return captured


def _exchange_callback_code(page: Any, code_verifier: str, proxy: str) -> dict[str, str]:
    """callback?code= 出现后立刻 PKCE 换 token。"""
    from urllib.parse import parse_qs

    from services.register.openai_register import create_session, request_platform_oauth_token

    qs = parse_qs(urlparse(page.url or "").query)
    code = str((qs.get("code") or [""])[0]).strip()
    if not code or not code_verifier:
        return {}
    session = create_session(proxy)
    try:
        tokens = request_platform_oauth_token(session, code, code_verifier) or {}
    except Exception as exc:  # noqa: BLE001
        _log("token_exchange_warn", error=str(exc)[:220])
        return {}
    finally:
        session.close()
    return {
        "access_token": str(tokens.get("access_token") or "").strip(),
        "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        "id_token": str(tokens.get("id_token") or "").strip(),
    }


def _exchange_chatgpt_callback_code(page: Any, code_verifier: str, proxy: str) -> dict[str, str]:
    """chatgpt.com/api/auth/callback/openai?code=… → auth.openai.com 换票。

    页面本身可能因代理显示 Problem loading page，但地址栏仍带 code，可直接 PKCE 换。
    """
    from urllib.parse import parse_qs

    from services.register.openai_register import create_session

    qs = parse_qs(urlparse(page.url or "").query)
    code = str((qs.get("code") or [""])[0]).strip()
    if not code or not code_verifier:
        return {}
    session = create_session(proxy)
    try:
        resp = session.post(
            "https://auth.openai.com/api/accounts/oauth/token",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/",
            },
            json={
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://chatgpt.com/api/auth/callback/openai",
            },
            verify=False,
            timeout=60,
        )
        data = resp.json() if resp.text else {}
        if resp.status_code != 200 or not isinstance(data, dict):
            _log(
                "chatgpt_token_exchange_warn",
                status=getattr(resp, "status_code", None),
                detail=str(data)[:220],
            )
            return {}
        return {
            "access_token": str(data.get("access_token") or "").strip(),
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "id_token": str(data.get("id_token") or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001
        _log("chatgpt_token_exchange_warn", error=str(exc)[:220])
        return {}
    finally:
        session.close()



def _wait_for_refresh_token(captured: dict[str, Any], *, timeout_sec: float = 60.0) -> dict[str, str]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        tokens = captured.get("tokens") or {}
        if tokens.get("refresh_token") and tokens.get("access_token"):
            return dict(tokens)
        time.sleep(0.35)
    return dict(captured.get("tokens") or {})


def _persist_account(*, email: str, password: str, tokens: dict[str, str], proxy: str, source_detail: str) -> dict[str, Any]:
    from services.account_service import account_service

    proxy_l = (proxy or "").lower()
    if "18030" in proxy_l or "udeal" in proxy_l:
        proxy_provider = "udeal"
        source_detail = source_detail if "udeal" in source_detail else f"{source_detail}_udeal"
    elif "127.0.0.1" in proxy_l or "localhost" in proxy_l:
        proxy_provider = "local"
    else:
        proxy_provider = "webshare"

    item = {
        "email": email,
        "password": password,
        "access_token": tokens.get("access_token") or "",
        "refresh_token": tokens.get("refresh_token") or "",
        "id_token": tokens.get("id_token") or "",
        "chatgpt_session_token": tokens.get("chatgpt_session_token") or "",
        "chatgpt_session_expires": tokens.get("chatgpt_session_expires") or None,
        "expires_at": tokens.get("expires_at") or None,
        "source_type": "web",
        "source_detail": source_detail,
        "proxy": proxy,
        "proxy_provider": proxy_provider,
        "lifecycle_ip_mode": "sticky_one_ip_full" if proxy else "",
        "proxy_scope": "account_sticky" if proxy else "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # 附带 udeal egress 元数据（若刚准备过）
    meta_path = ROOT / "data" / "runlogs" / "udeal_camoufox_egress_last.json"
    if proxy_provider == "udeal" and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                if meta.get("session"):
                    item["udeal_session"] = meta.get("session")
                if meta.get("sticky_http_url"):
                    item["udeal_sticky_http_url"] = meta.get("sticky_http_url")
                if isinstance(meta.get("trace"), dict):
                    item["register_egress_ip"] = meta["trace"].get("ip")
                    item["register_egress_loc"] = meta["trace"].get("loc")
        except Exception:
            pass
    return account_service.add_account_items([item])


def _page_error_snippet(page: Any, *, limit: int = 400) -> str:
    """Authentication Error 等页面：必须读正文，不能只看 title。"""
    chunks: list[str] = []
    try:
        chunks.append(f"title={page.title()!r}")
    except Exception:
        pass
    try:
        chunks.append(f"path={_page_path(page)!r}")
    except Exception:
        pass
    try:
        chunks.append(f"url={(page.url or '')[:160]!r}")
    except Exception:
        pass
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=2500) or "").strip()
    except Exception:
        body = ""
    if body:
        compact = re.sub(r"\s+", " ", body)
        chunks.append(f"body={compact[:limit]!r}")
    return " | ".join(chunks)


_BAN_MARKERS = (
    "banned",
    "suspended",
    "deactivated",
    "disabled",
    "has been locked",
    "account has been",
    "no longer available",
    "violat",
    "封禁",
    "停用",
    "禁用",
    "锁定",
    "无法登录",
)


def _raise_if_auth_or_ban_page(page: Any, *, where: str) -> None:
    title = ""
    body = ""
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    try:
        body = (page.locator("body").inner_text(timeout=2500) or "").lower()
    except Exception:
        body = ""
    snippet = _page_error_snippet(page)
    combined = f"{title}\n{body}"
    if any(m in combined for m in _BAN_MARKERS):
        raise RuntimeError(f"account_banned_or_disabled at={where} {snippet}")
    if "authentication error" in title or "auth error" in title:
        raise RuntimeError(f"authentication_error at={where} {snippet}")


def _fill_login_password(page: Any, password: str) -> None:
    page.wait_for_url(re.compile(r".*/(log-in/password|password).*"), timeout=45000)
    _assert_not_cf_blocked(page)
    page.wait_for_timeout(800)
    pwd = page.locator('input[type="password"]:visible').first
    pwd.click(timeout=10000)
    # React 受控输入：先原生清空再逐字输入
    pwd.evaluate(
        """(el) => {
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(el, '');
          el.dispatchEvent(new Event('input', {bubbles:true}));
        }"""
    )
    pwd.type(password, delay=35)
    page.wait_for_timeout(400)
    try:
        _click_continue(page)
    except Exception:
        pwd.press("Enter")
    page.wait_for_function(
        """() => {
          const p = location.pathname || '';
          const t = (document.title || '').toLowerCase();
          if (t.includes('authentication error')) return 'auth_error';
          if (p.includes('email-verification')) return 'otp';
          if (p.includes('callback') || p.includes('consent') || p.includes('about-you')) return 'ok';
          if (!(p.includes('password') || p.includes('log-in'))) return 'ok';
          return false;
        }""",
        timeout=45000,
    )
    _raise_if_auth_or_ban_page(page, where="after_login_password")
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        pass
    if "incorrect" in body or "wrong password" in body or ("invalid" in body and "password" in body):
        raise RuntimeError(f"login_invalid_password {_page_error_snippet(page)}")


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    proxy = "http://127.0.0.1:7897"
    relogin_email = ""
    relogin_password = ""
    # usage:
    #   python scripts/yumail_camoufox_openai_register.py [proxy]
    #   python scripts/yumail_camoufox_openai_register.py --relogin email password [proxy]
    if args and args[0] == "--relogin":
        if len(args) < 3:
            _log("failed", ok=False, error="usage: --relogin email password [proxy]")
            return 2
        relogin_email = args[1].strip()
        relogin_password = args[2].strip()
        if len(args) >= 4:
            proxy = args[3].strip()
    elif args:
        proxy = args[0].strip()

    mail = _mail_config()
    mode = "relogin" if relogin_email else "register"
    _log("start", engine="camoufox", proxy=proxy, mode=mode, yumail_mode="acquire_only")

    openai_password = ""
    email = ""
    mailbox: dict[str, Any] = {}
    if mode == "relogin":
        email = relogin_email
        openai_password = relogin_password
        mailbox = {"provider": "yumail", "address": email, "email": email}
        _log("relogin_target", email=email)
    else:
        mailbox = mail_provider.create_mailbox(mail)
        email = str(mailbox.get("address") or "").strip()
        if not email or "@" not in email:
            _log("failed", ok=False, error="yumail_acquire_empty")
            return 2
        openai_password = generate_openai_account_password()
        _log("mailbox_acquired", email=email, note="existing yumail pool account")

    # 注册走 platform；重登默认 chatgpt（platform 密码页易 Authentication Error）
    oauth_client = "chatgpt" if mode == "relogin" else "platform"
    if mode == "relogin":
        authorize_url, code_verifier = _authorize_url(email, screen_hint="login", client=oauth_client)
    else:
        authorize_url, code_verifier = _authorize_url(email, screen_hint="signup", client=oauth_client)
    result: dict[str, Any] = {
        "ok": False,
        "email": email,
        "code_verifier": code_verifier,
        "engine": "camoufox",
        "mode": mode,
        "oauth_client": oauth_client,
    }
    result: dict[str, Any] = {
        "ok": False,
        "email": email,
        "code_verifier": code_verifier,
        "engine": "camoufox",
        "mode": mode,
    }
    proxy_cfg = _proxy_dict(proxy)
    launch_kwargs: dict[str, Any] = {
        "headless": False,
        "os": "windows",
        "humanize": True,
    }
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        # geoip 探测失败后同进程二次 Camoufox 会撞 asyncio loop；默认关掉，需时用 CAMOUFOX_GEOIP=1
        import os as _os

        if str(_os.getenv("CAMOUFOX_GEOIP") or "").strip() in {"1", "true", "yes"}:
            launch_kwargs["geoip"] = True

    try:
        browser_cm = Camoufox(**launch_kwargs)
        browser = browser_cm.__enter__()
        try:
            page = browser.new_page()
            captured = _attach_token_capture(page)
            boundary = datetime.now(timezone.utc)
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=90000)
            _wait_transition(page, timeout_ms=60000)
            _assert_not_cf_blocked(page)
            # 给页面脚本/ CF 一点时间，减少 password 提交后 Authentication Error
            page.wait_for_timeout(1500)
            path = _page_path(page)
            _log("authorized", path=path, title=page.title())

            about_you_done = False
            if mode == "relogin":
                # chatgpt 常直接 OTP；platform 常先密码。OTP 后也可能再要密码。
                # boundary 用 authorize 前时间戳，避免漏掉已发出的验证码邮件
                if "email-verification" in path:
                    _fill_otp(page, mailbox, mail, boundary)
                    path = _page_path(page)
                    _log("otp_done", path=path, title=page.title())
                    _assert_not_cf_blocked(page)
                if "password" in path or "log-in" in path:
                    _fill_login_password(page, openai_password)
                    path = _page_path(page)
                    _log("password_done", path=path, title=page.title())
                    _assert_not_cf_blocked(page)
                if "email-verification" in path:
                    boundary = datetime.now(timezone.utc)
                    _fill_otp(page, mailbox, mail, boundary)
                    path = _page_path(page)
                    _log("otp_done_after_password", path=path, title=page.title())
                    _assert_not_cf_blocked(page)
                if "password" in path and "email-verification" not in path:
                    raise RuntimeError(f"relogin_stuck_at_password path={path} title={page.title()!r}")
                if "email-verification" in path:
                    raise RuntimeError(f"relogin_stuck_at_otp path={path} title={page.title()!r}")
            else:
                # 默认走收件 OTP：密码页点「一次性验证码注册」，不先设密
                if path.rstrip("/") == "/create-account/password":
                    boundary = datetime.now(timezone.utc)
                    _switch_to_otp_signup(page)
                    path = _page_path(page)
                    _log("switched_to_otp_signup", path=path, title=page.title())
                    _assert_not_cf_blocked(page)

                if "email-verification" in path:
                    _fill_otp(page, mailbox, mail, boundary)
                    path = _page_path(page)
                    _log("otp_done", path=path, title=page.title())
                    _assert_not_cf_blocked(page)

                # OTP 后若仍要求设密，再填密码
                if path.rstrip("/") == "/create-account/password":
                    _fill_password(page, openai_password)
                    path = _page_path(page)
                    _log("password_done_after_otp", path=path)

                if path.rstrip("/") == "/about-you":
                    _fill_about_you(page, email)
                    path = _page_path(page)
                    about_you_done = True
                    _log("about_you_done", path=path, title=page.title())

            body = ""
            try:
                body = (page.locator("body").inner_text(timeout=3000) or "").lower()
            except Exception:
                pass
            if "registration_disallowed" in body:
                raise RuntimeError("registration_disallowed")

            path_l = path.lower()
            url_l = (page.url or "").lower()
            if mode != "relogin":
                if "email-verification" in path_l or path_l.rstrip("/") == "/create-account/password":
                    raise RuntimeError(f"registration_incomplete_stuck_at_{path}")
                if path_l.rstrip("/") == "/about-you":
                    raise RuntimeError("about_you_not_submitted")
                finished = about_you_done or any(
                    m in path_l or m in url_l
                    for m in ("/auth/callback", "/consent", "callback", "platform.openai.com", "chatgpt.com")
                )
                if not finished:
                    raise RuntimeError(f"registration_incomplete_path_{path}_url_{(page.url or '')[:120]}")

            # —— 拿到 RT 再关浏览器 ——
            tokens = dict(captured.get("tokens") or {})
            page_url = page.url or ""
            # platform callback 可立刻 PKCE 换票；chatgpt SPA 靠监听 /oauth/token
            # 若 callback 页被代理打成 Problem loading page，仍可能带 code=，直接换票。
            if not tokens.get("refresh_token") and "code=" in page_url:
                if "platform.openai.com" in page_url:
                    _log("token_exchange_start", path=path, kind="platform")
                    exchanged = _exchange_callback_code(page, code_verifier, proxy)
                else:
                    _log("token_exchange_start", path=path, kind="chatgpt", url=page_url[:160])
                    exchanged = _exchange_chatgpt_callback_code(page, code_verifier, proxy)
                if exchanged.get("refresh_token"):
                    tokens = exchanged
                    _log("token_exchange_ok", has_rt=True, kind="callback_code")
            if not tokens.get("refresh_token"):
                _log("token_wait_network", timeout_sec=60)
                tokens = _wait_for_refresh_token(captured, timeout_sec=60)
            if not tokens.get("refresh_token"):
                # 再等一会：SPA 可能较慢
                page.wait_for_timeout(3000)
                tokens = _wait_for_refresh_token(captured, timeout_sec=30)
            if not tokens.get("refresh_token"):
                raise RuntimeError(
                    "refresh_token_missing_keep_browser_open_failed "
                    f"events={captured.get('events')[-5:]}"
                )

            source_detail = "yumail_camoufox_relogin" if mode == "relogin" else "yumail_acquire_camoufox"
            add = _persist_account(
                email=email,
                password=openai_password,
                tokens=tokens,
                proxy=proxy,
                source_detail=source_detail,
            )
            result.update(
                {
                    "ok": True,
                    "password": openai_password,
                    "path": path,
                    "url": (page.url or "")[:200],
                    "title": page.title(),
                    "has_refresh_token": True,
                    "has_access_token": bool(tokens.get("access_token")),
                    "source_detail": source_detail,
                    "add": {k: add.get(k) for k in ("added", "updated", "total", "count") if k in add},
                }
            )
            out_name = "yumail_camoufox_relogin_last.json" if mode == "relogin" else "yumail_camoufox_register_last.json"
            out = ROOT / "data" / "runlogs" / out_name
            out.parent.mkdir(parents=True, exist_ok=True)
            # 不把完整 token 明文写进 runlog（只记是否拿到）
            safe = dict(result)
            out.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
            _log("done", ok=True, email=email, path=path, has_rt=True, add=result.get("add"))
            return 0
        finally:
            # 仅在拿到 RT（ok）或彻底失败后关闭；失败也会关，避免僵尸窗口
            browser_cm.__exit__(None, None, None)
    except Exception as exc:
        result.update({"error": f"{type(exc).__name__}: {exc}"})
        out_name = "yumail_camoufox_relogin_last.json" if mode == "relogin" else "yumail_camoufox_register_last.json"
        out = ROOT / "data" / "runlogs" / out_name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        _log("failed", **{k: result.get(k) for k in ("ok", "email", "error", "path", "mode")})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
