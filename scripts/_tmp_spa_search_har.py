#!/usr/bin/env python3
"""Focused SPA Search on/off HAR (Clash + session cookie)."""
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

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
HAR_DIR = ROOT / "docs" / "captures" / "spa"
DEFAULT_PROXY = "http://127.0.0.1:7897"


def _log(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _proxy_dict(proxy: str) -> dict[str, str]:
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port or 80}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def _new_chat(page) -> None:
    for sel in ('a[href="/"]', 'button:has-text("New chat")', '[data-testid="create-new-chat-button"]'):
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=4000, force=True)
                page.wait_for_timeout(800)
                _log(phase="new_chat", sel=sel)
                return
        except Exception:
            continue
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(1500)


def _enable_search(page) -> bool:
    # open plus / tools
    for sel in ('[data-testid="composer-plus-btn"]', 'button[aria-label*="Add" i]', 'button:has-text("Tools")'):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=4000, force=True)
                page.wait_for_timeout(700)
                _log(phase="tools_open", sel=sel)
                break
        except Exception:
            continue
    for pattern in (
        re.compile(r"Look something up", re.I),
        re.compile(r"^Web search$", re.I),
        re.compile(r"^Search$", re.I),
        re.compile(r"Search the web", re.I),
        re.compile(r"^Browse", re.I),
    ):
        try:
            page.get_by_role("menuitem", name=pattern).first.click(timeout=3000, force=True)
            _log(phase="search_on", via="menuitem", pattern=pattern.pattern)
            page.wait_for_timeout(800)
            return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(pattern).first
            if loc.is_visible():
                loc.click(timeout=3000, force=True)
                _log(phase="search_on", via="text", pattern=pattern.pattern)
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    menu = page.evaluate(
        """() => Array.from(document.querySelectorAll('[role=\"menuitem\"],button,div[role=\"button\"]'))
          .map(n => (n.innerText||'').trim().slice(0,60))
          .filter(Boolean)
          .slice(0,40)"""
    )
    _log(phase="menu_dump", items=menu)
    hit = page.evaluate(
        """() => {
          const nodes = Array.from(document.querySelectorAll('button,[role=\"menuitem\"],div[role=\"button\"]'));
          const hit = nodes.find(n => /look something up|web\\s*search|^search$/i.test((n.innerText||'').trim()));
          if (hit) { hit.click(); return (hit.innerText||'').trim().slice(0,80); }
          return null;
        }"""
    )
    if hit:
        _log(phase="search_on", via="evaluate", text=hit)
        page.wait_for_timeout(800)
        return True
    _log(phase="search_on_miss")
    return False


def _send(page, text: str, wait_ms: int) -> None:
    page.wait_for_selector("#prompt-textarea", timeout=30000)
    loc = page.locator("#prompt-textarea").first
    loc.click(force=True)
    page.keyboard.type(text, delay=6)
    page.keyboard.press("Enter")
    _log(phase="sent", text=text[:80], wait_ms=wait_ms)
    page.wait_for_timeout(wait_ms)


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    session = secret["chatgpt_session_token"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    har_path = HAR_DIR / f"spa-search-{stamp}.har"
    HAR_DIR.mkdir(parents=True, exist_ok=True)

    launch = {"headless": True, "humanize": False, "proxy": _proxy_dict(DEFAULT_PROXY), "os": "windows"}
    try:
        cm = Camoufox(**{**launch, "geoip": True})
        browser = cm.__enter__()
    except Exception:
        cm = Camoufox(**launch)
        browser = cm.__enter__()
    _log(phase="browser_up", har=str(har_path))

    search_on = False
    try:
        ctx = browser.new_context(record_har_path=str(har_path), record_har_content="embed")
        ctx.add_cookies(
            [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session,
                    "domain": "chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = ctx.new_page()
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(2000)

        _new_chat(page)
        search_on = _enable_search(page)
        _send(page, "What is the capital of Japan? Cite a source if you use the web.", 45000)

        _new_chat(page)
        # ensure no search chip if possible
        _send(page, "Reply with exactly: hello", 20000)

        ctx.close()
    finally:
        cm.__exit__(None, None, None)

    hints_search = 0
    conv_hints: list = []
    if har_path.exists():
        har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
        for e in har.get("log", {}).get("entries", []):
            req = e.get("request") or {}
            url = req.get("url") or ""
            if "/f/conversation" not in url or req.get("method") != "POST":
                continue
            text = ((req.get("postData") or {}).get("text")) or ""
            try:
                body = json.loads(text) if text else {}
            except Exception:
                body = {}
            h = body.get("system_hints")
            if "system_hints" in body:
                conv_hints.append(h)
            if isinstance(h, list) and "search" in h:
                hints_search += 1

    meta = {
        "har": str(har_path),
        "search_on_ui": search_on,
        "system_hints_search_count": hints_search,
        "system_hints_seen": conv_hints[:8],
        "bytes": har_path.stat().st_size if har_path.exists() else 0,
    }
    (HAR_DIR / f"spa-search-{stamp}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _log(phase="done", **meta)
    return 0 if search_on or hints_search else 1


if __name__ == "__main__":
    raise SystemExit(main())
