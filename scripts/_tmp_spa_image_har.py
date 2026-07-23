#!/usr/bin/env python3
"""Focused image capture with existing session cookie — force Create image UI if possible."""
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
OUT_DIR = ROOT / "docs" / "captures" / "spa"
PROXY = "http://127.0.0.1:7897"


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


def _dismiss(page):
    try:
        page.keyboard.press("Escape")
        page.evaluate(
            """() => {
              const m=document.querySelector('#modal-m3m-nux,[data-testid=\"modal-m3m-nux\"]');
              if(m) m.remove();
            }"""
        )
    except Exception:
        pass


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    session_token = secret["chatgpt_session_token"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    har_path = OUT_DIR / f"spa-image-{stamp}.har"

    # headless=True: headed mode can hang under agent sessions with no interactive display
    launch = {"headless": True, "humanize": False, "proxy": _proxy_dict(PROXY), "os": "windows"}
    try:
        cm = Camoufox(**{**launch, "geoip": True})
        browser = cm.__enter__()
    except Exception:
        cm = Camoufox(**launch)
        browser = cm.__enter__()
    _log(phase="browser_up", headless=True)

    try:
        ctx = browser.new_context(record_har_path=str(har_path), record_har_content="embed")
        ctx.add_cookies(
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
        page = ctx.new_page()
        for attempt in range(1, 4):
            try:
                page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)
                break
            except Exception as exc:
                _log(phase="goto_retry", attempt=attempt, error=str(exc)[:120])
                time.sleep(2)
        _dismiss(page)
        page.wait_for_timeout(2000)
        # new chat
        try:
            page.get_by_role("button", name=re.compile("New chat", re.I)).first.click(timeout=5000, force=True)
            page.wait_for_timeout(1000)
        except Exception:
            pass
        _dismiss(page)

        # Force Create image (picture_v2) UI — plus menu / tools / direct hint chip
        opened = False
        for sel in (
            '[data-testid="composer-plus-btn"]',
            'button[aria-label*="Add files" i]',
            'button[aria-label*="Attach" i]',
            'button:has-text("Tools")',
            'button[aria-haspopup="menu"]',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=4000, force=True)
                    opened = True
                    _log(phase="clicked_plus", sel=sel)
                    page.wait_for_timeout(600)
                    break
            except Exception:
                continue
        image_mode = False
        for pattern in (
            re.compile(r"^Create image$", re.I),
            re.compile(r"Create an image", re.I),
            re.compile(r"^Image$", re.I),
        ):
            try:
                loc = page.get_by_role("menuitem", name=pattern).first
                loc.click(timeout=3000, force=True)
                image_mode = True
                _log(phase="image_mode", via="menuitem", pattern=pattern.pattern)
                page.wait_for_timeout(800)
                break
            except Exception:
                pass
            try:
                loc = page.get_by_text(pattern).first
                if loc.is_visible():
                    loc.click(timeout=3000, force=True)
                    image_mode = True
                    _log(phase="image_mode", via="text", pattern=pattern.pattern)
                    page.wait_for_timeout(800)
                    break
            except Exception:
                continue
        if not image_mode:
            # JS fallback: click any control whose text matches Create image
            clicked = page.evaluate(
                """() => {
                  const nodes = Array.from(document.querySelectorAll('button, [role=\"menuitem\"], div[role=\"button\"]'));
                  const hit = nodes.find(n => /create\\s*(an\\s*)?image|^image$/i.test((n.innerText||'').trim()));
                  if (hit) { hit.click(); return (hit.innerText||'').trim().slice(0,80); }
                  return null;
                }"""
            )
            if clicked:
                image_mode = True
                _log(phase="image_mode", via="evaluate", text=clicked)
                page.wait_for_timeout(800)
            else:
                _log(phase="image_mode_miss", opened_plus=opened)

        page.wait_for_selector("#prompt-textarea", timeout=30000)
        loc = page.locator("#prompt-textarea").first
        loc.click(force=True)
        # Short prompt; if UI mode on, avoid "Create an image" NL (want picture_v2 hint path)
        prompt = (
            "a simple flat blue circle icon on white background, no text"
            if image_mode
            else "Create an image of a simple flat blue circle icon on white background, no text"
        )
        page.keyboard.type(prompt, delay=8)
        page.keyboard.press("Enter")
        _log(phase="sent_image_prompt", image_mode=image_mode, prompt=prompt[:60])
        page.wait_for_timeout(75000)
        meta = {"har": str(har_path), "captured_at": stamp, "proxy": PROXY}
        (OUT_DIR / f"spa-image-{stamp}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        ctx.close()
    finally:
        cm.__exit__(None, None, None)
    # Quick post-parse: did any conversation POST carry picture_v2?
    picture_v2_req = False
    system_hints_seen: list = []
    conduit_on_sse = False
    if har_path.exists():
        try:
            har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
            for e in har.get("log", {}).get("entries", []):
                req = e.get("request") or {}
                url = req.get("url") or ""
                if "/f/conversation" not in url or req.get("method") != "POST":
                    continue
                text = ((req.get("postData") or {}).get("text")) or ""
                if "picture_v2" in text:
                    picture_v2_req = True
                try:
                    body = json.loads(text) if text else {}
                except Exception:
                    body = {}
                if "system_hints" in body:
                    system_hints_seen.append(body.get("system_hints"))
                for h in req.get("headers") or []:
                    if str(h.get("name", "")).lower() == "x-conduit-token":
                        conduit_on_sse = True
        except Exception as exc:
            _log(phase="har_parse_error", error=str(exc)[:160])
    _log(
        phase="done",
        har=str(har_path),
        bytes=har_path.stat().st_size if har_path.exists() else 0,
        picture_v2_in_conversation_req=picture_v2_req,
        system_hints_seen=system_hints_seen[:6],
        conduit_header_on_conversation=conduit_on_sse,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
