#!/usr/bin/env python3
"""Capture ChatGPT SPA HAR via Chrome + Clash for protocol reverse.

Uses local secret: data/runlogs/spa_repro/qaflow_secret.json
Proxy: http://127.0.0.1:7897 (Clash)

Usage:
  python scripts/_tmp_spa_har_capture.py
  python scripts/_tmp_spa_har_capture.py --headed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "docs" / "captures" / "spa"
PROXY = "http://127.0.0.1:7897"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_secret() -> dict:
    data = json.loads(SECRET.read_text(encoding="utf-8"))
    if not data.get("email") or not data.get("password"):
        raise SystemExit(f"missing email/password in {SECRET}")
    return data


def _click_first(page, selectors: list[str], timeout: int = 5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _fill_first(page, selectors: list[str], value: str, timeout: int = 5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.fill(value, timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _login(page, email: str, password: str) -> None:
    page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=90000)
    time.sleep(2)
    # Continue / Log in buttons
    _click_first(
        page,
        [
            'button:has-text("Log in")',
            'button:has-text("登录")',
            'a:has-text("Log in")',
            'button:has-text("Continue")',
            '[data-testid="login-button"]',
        ],
        timeout=8000,
    )
    time.sleep(2)
    # Email
    if not _fill_first(
        page,
        [
            'input[name="email"]',
            'input[type="email"]',
            "#email",
            'input[autocomplete="username"]',
        ],
        email,
    ):
        # maybe already on password or chatgpt home
        print(json.dumps({"phase": "email_fill_miss", "url": page.url}, ensure_ascii=False))
    else:
        _click_first(
            page,
            [
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("继续")',
                'input[type="submit"]',
            ],
        )
        time.sleep(2)

    # Password
    if _fill_first(
        page,
        [
            'input[name="password"]',
            'input[type="password"]',
            "#password",
            'input[autocomplete="current-password"]',
        ],
        password,
    ):
        _click_first(
            page,
            [
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Log in")',
                'button:has-text("登录")',
            ],
        )
    time.sleep(5)
    # Wait until we are on chatgpt app (not auth)
    deadline = time.time() + 120
    while time.time() < deadline:
        url = page.url
        print(json.dumps({"phase": "login_wait", "url": url}, ensure_ascii=False))
        if "chatgpt.com" in url and "auth" not in url and "login" not in url:
            # check composer
            try:
                page.wait_for_selector("#prompt-textarea, textarea, [contenteditable='true']", timeout=10000)
                print(json.dumps({"phase": "login_ok", "url": url}, ensure_ascii=False))
                return
            except PwTimeout:
                pass
        # OTP prompt?
        body = ""
        try:
            body = page.inner_text("body")[:500]
        except Exception:
            pass
        if re.search(r"code|验证码|OTP|check your email", body, re.I):
            print(json.dumps({"phase": "otp_required", "snippet": body[:200]}, ensure_ascii=False))
            # leave headed window for manual OTP if needed
            page.wait_for_timeout(90000)
        page.wait_for_timeout(2000)
    raise RuntimeError(f"login_timeout url={page.url}")


def _send_prompt(page, text: str) -> None:
    # composer
    selectors = ["#prompt-textarea", "textarea[name='prompt-textarea']", "[contenteditable='true']"]
    filled = False
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "textarea":
                loc.fill(text)
            else:
                loc.click()
                page.keyboard.type(text, delay=20)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        raise RuntimeError("composer_not_found")
    page.keyboard.press("Enter")
    # wait for response streaming to settle a bit
    page.wait_for_timeout(15000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", default=True)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--text", default="Reply with exactly: PONG")
    ap.add_argument("--image", default="Generate a simple flat icon of a blue circle, no text")
    ap.add_argument("--skip-image", action="store_true")
    args = ap.parse_args()
    headed = not args.headless

    secret = _load_secret()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _ts()
    har_path = OUT_DIR / f"spa-full-{stamp}.har"
    meta_path = OUT_DIR / f"spa-full-{stamp}.meta.json"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / "data" / "runlogs" / "spa_repro" / "chrome_profile"),
            channel="chrome",
            executable_path=CHROME if Path(CHROME).exists() else None,
            headless=not headed,
            proxy={"server": PROXY},
            record_har_path=str(har_path),
            record_har_content="embed",
            record_har_mode="full",
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _login(page, secret["email"], secret["password"])
            print(json.dumps({"phase": "text_send", "text": args.text}, ensure_ascii=False))
            _send_prompt(page, args.text)
            page.wait_for_timeout(8000)
            if not args.skip_image:
                print(json.dumps({"phase": "image_send", "text": args.image}, ensure_ascii=False))
                # new chat if possible
                _click_first(page, ['a:has-text("New chat")', 'button:has-text("New chat")', '[data-testid="create-new-chat-button"]'])
                page.wait_for_timeout(2000)
                _send_prompt(page, args.image)
                page.wait_for_timeout(45000)
            meta = {
                "har": str(har_path),
                "email": secret["email"],
                "proxy": PROXY,
                "final_url": page.url,
                "captured_at": stamp,
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"phase": "done", **meta}, ensure_ascii=False))
        finally:
            context.close()

    print(json.dumps({"phase": "har_written", "path": str(har_path), "bytes": har_path.stat().st_size}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"phase": "fatal", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
