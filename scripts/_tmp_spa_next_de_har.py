#!/usr/bin/env python3
"""One Camoufox HAR covering D upload + (best-effort) i2i + E search on/off.

Flow:
  1) headless Camoufox + Clash + inject session-token; record HAR
  2) write small probe.png
  3) Upload: New chat → attach PNG → ask color → wait
  4) Search ON: Tools → Search → capital question
  5) Search OFF: New chat (no search chip) → short question
  6) i2i (best-effort): attach PNG → Create image → redder prompt
  7) Parse HAR counts (files/uploaded/azure, file-service/sediment/asset_pointer, system_hints)

Usage:
  python scripts/_tmp_spa_next_de_har.py
  python scripts/_tmp_spa_next_de_har.py --proxy http://127.0.0.1:7897
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
HAR_DIR = ROOT / "docs" / "captures" / "spa"
BENCH_DIR = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"
DEFAULT_PROXY = "http://127.0.0.1:7897"
SESSION_COOKIE = "__Secure-next-auth.session-token"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _proxy_dict(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _write_probe_png(path: Path, size: int = 64, rgb: tuple[int, int, int] = (32, 128, 220)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # type: ignore

        Image.new("RGB", (size, size), rgb).save(path)
        return
    except Exception:
        pass
    # minimal uncompressed RGB PNG via struct/zlib
    w = h = size
    r, g, b = rgb
    raw = b""
    row = bytes([0]) + bytes([r, g, b]) * w
    raw = row * h
    compressed = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    path.write_bytes(png)


def _dismiss(page) -> None:
    for _ in range(5):
        closed = False
        for sel in (
            '[data-testid="modal-m3m-nux"] button',
            '[data-testid="modal-m3m-nux"] [aria-label="Close"]',
            'button:has-text("Okay")',
            'button:has-text("Got it")',
            'button:has-text("Continue")',
            'button:has-text("Not now")',
            'button:has-text("Skip")',
            '[role="dialog"] button[aria-label="Close"]',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=1500, force=True)
                    closed = True
                    page.wait_for_timeout(300)
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.evaluate(
                """() => {
                  const m = document.querySelector('#modal-m3m-nux,[data-testid="modal-m3m-nux"]');
                  if (m) m.remove();
                }"""
            )
        except Exception:
            pass
        page.wait_for_timeout(200)
        if not closed:
            return


def _new_chat(page) -> None:
    _dismiss(page)
    for sel in (
        'a[href="/"]',
        'button:has-text("New chat")',
        '[data-testid="create-new-chat-button"]',
        'a:has-text("New chat")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=4000, force=True)
                page.wait_for_timeout(1000)
                _log(phase="new_chat", sel=sel)
                _dismiss(page)
                return
        except Exception:
            continue
    try:
        page.get_by_role("button", name=re.compile("New chat", re.I)).first.click(timeout=4000, force=True)
        page.wait_for_timeout(1000)
        _log(phase="new_chat", sel="role=button New chat")
    except Exception as exc:
        _log(phase="new_chat_miss", error=str(exc)[:120])
    _dismiss(page)


def _type_and_send(page, text: str, wait_ms: int) -> None:
    _dismiss(page)
    page.wait_for_selector("#prompt-textarea", timeout=60000)
    loc = page.locator("#prompt-textarea").first
    try:
        loc.click(timeout=5000, force=True)
    except Exception:
        pass
    try:
        tag = loc.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = "div"
    if tag == "textarea":
        loc.fill(text)
    else:
        try:
            page.keyboard.press("Control+A")
        except Exception:
            pass
        page.keyboard.type(text, delay=8)
    page.keyboard.press("Enter")
    _log(phase="sent", text=text[:80], wait_ms=wait_ms)
    page.wait_for_timeout(wait_ms)


def _attach_png(page, png_path: Path) -> bool:
    """Attach file via input[type=file] or plus→file picker. Returns True if set_input_files ran."""
    _dismiss(page)
    # prefer existing hidden file input
    for sel in (
        'input[type="file"]',
        'input[accept*="image"]',
        'input[accept*="*"]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.set_input_files(str(png_path))
                _log(phase="attach", via=sel)
                page.wait_for_timeout(1500)
                return True
        except Exception as exc:
            _log(phase="attach_try_fail", sel=sel, error=str(exc)[:100])

    # open plus / attach menu then retry file input
    for sel in (
        '[data-testid="composer-plus-btn"]',
        'button[aria-label*="Add files" i]',
        'button[aria-label*="Attach" i]',
        'button[aria-label*="Upload" i]',
        'button:has-text("Attach")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000, force=True)
                _log(phase="clicked_attach_btn", sel=sel)
                page.wait_for_timeout(500)
                break
        except Exception:
            continue

    try:
        loc = page.locator('input[type="file"]').first
        if loc.count():
            loc.set_input_files(str(png_path))
            _log(phase="attach", via="input[type=file]_after_plus")
            page.wait_for_timeout(2000)
            return True
    except Exception as exc:
        _log(phase="attach_fail", error=str(exc)[:160])
    return False


def _enable_search(page) -> bool:
    """Open Tools/plus and click Search / web menuitem (English UI)."""
    opened = False
    for sel in (
        '[data-testid="composer-plus-btn"]',
        'button:has-text("Tools")',
        'button[aria-label*="Tools" i]',
        'button[aria-haspopup="menu"]',
        'button[aria-label*="Add" i]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3500, force=True)
                opened = True
                _log(phase="tools_open", sel=sel)
                page.wait_for_timeout(600)
                break
        except Exception:
            continue

    for pattern in (
        re.compile(r"^Search$", re.I),
        re.compile(r"Web search", re.I),
        re.compile(r"Search the web", re.I),
        re.compile(r"^Browse$", re.I),
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

    clicked = page.evaluate(
        """() => {
          const nodes = Array.from(document.querySelectorAll('button, [role="menuitem"], div[role="button"]'));
          const hit = nodes.find(n => /^(search|web search|search the web|browse)$/i.test((n.innerText||'').trim()));
          if (hit) { hit.click(); return (hit.innerText||'').trim().slice(0,80); }
          return null;
        }"""
    )
    if clicked:
        _log(phase="search_on", via="evaluate", text=clicked)
        page.wait_for_timeout(800)
        return True
    _log(phase="search_on_miss", opened=opened)
    return False


def _try_create_image(page) -> bool:
    opened = False
    for sel in (
        '[data-testid="composer-plus-btn"]',
        'button:has-text("Tools")',
        'button[aria-haspopup="menu"]',
        'button[aria-label*="Add files" i]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3500, force=True)
                opened = True
                page.wait_for_timeout(500)
                break
        except Exception:
            continue
    for pattern in (
        re.compile(r"^Create image$", re.I),
        re.compile(r"Create an image", re.I),
        re.compile(r"^Image$", re.I),
    ):
        try:
            page.get_by_role("menuitem", name=pattern).first.click(timeout=3000, force=True)
            _log(phase="i2i_mode", via="menuitem", pattern=pattern.pattern)
            page.wait_for_timeout(800)
            return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(pattern).first
            if loc.is_visible():
                loc.click(timeout=3000, force=True)
                _log(phase="i2i_mode", via="text", pattern=pattern.pattern)
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    clicked = page.evaluate(
        """() => {
          const nodes = Array.from(document.querySelectorAll('button, [role="menuitem"], div[role="button"]'));
          const hit = nodes.find(n => /create\\s*(an\\s*)?image|^image$/i.test((n.innerText||'').trim()));
          if (hit) { hit.click(); return (hit.innerText||'').trim().slice(0,80); }
          return null;
        }"""
    )
    if clicked:
        _log(phase="i2i_mode", via="evaluate", text=clicked)
        page.wait_for_timeout(800)
        return True
    _log(phase="i2i_mode_miss", opened=opened)
    return False


def _has_search_chip(page) -> bool:
    try:
        for pat in (re.compile(r"Search", re.I), re.compile(r"Web", re.I)):
            loc = page.get_by_text(pat).first
            if loc.count() and loc.is_visible():
                # rough: chip near composer
                return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                """() => {
                  const t = (document.body && document.body.innerText) || '';
                  return /searching the web|web search on/i.test(t);
                }"""
            )
        )
    except Exception:
        return False


def _parse_har(har_path: Path) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "post_backend_files": 0,
        "post_uploaded": 0,
        "azure_upload_url": 0,
        "file_service_refs": 0,
        "sediment_refs": 0,
        "asset_pointer_refs": 0,
        "system_hints_search": 0,
        "system_hints_picture_v2": 0,
        "conversation_posts": 0,
    }
    if not har_path.exists():
        return counts
    try:
        har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        counts["parse_error"] = str(exc)[:160]
        return counts

    for e in har.get("log", {}).get("entries", []) or []:
        req = e.get("request") or {}
        method = str(req.get("method") or "").upper()
        url = str(req.get("url") or "")
        url_l = url.lower()

        if method == "POST" and "/backend-api/files" in url_l and "/uploaded" not in url_l:
            counts["post_backend_files"] += 1
        if method == "POST" and "/uploaded" in url_l:
            counts["post_uploaded"] += 1
        if "blob.core.windows.net" in url_l or "upload_url" in url_l or "azure" in url_l and "upload" in url_l:
            if method in ("PUT", "POST", "OPTIONS"):
                counts["azure_upload_url"] += 1

        if method != "POST" or "/conversation" not in url_l:
            continue
        if "/prepare" in url_l:
            continue
        counts["conversation_posts"] += 1
        text = ((req.get("postData") or {}).get("text")) or ""
        if "file-service://" in text:
            counts["file_service_refs"] += text.count("file-service://")
        if "sediment://" in text:
            counts["sediment_refs"] += text.count("sediment://")
        if "asset_pointer" in text:
            counts["asset_pointer_refs"] += text.count("asset_pointer")
        try:
            body = json.loads(text) if text else {}
        except Exception:
            body = {}
        hints = body.get("system_hints") if isinstance(body, dict) else None
        if isinstance(hints, list):
            hs = [str(x) for x in hints]
            if any("search" in h for h in hs):
                counts["system_hints_search"] += 1
            if any("picture_v2" in h for h in hs):
                counts["system_hints_picture_v2"] += 1
        elif "search" in text and "system_hints" in text:
            # fallback string scan
            if re.search(r'"system_hints"\s*:\s*\[[^\]]*"search"', text):
                counts["system_hints_search"] += 1
            if "picture_v2" in text:
                counts["system_hints_picture_v2"] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Camoufox HAR: upload + search on/off + i2i best-effort")
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--secret", type=Path, default=SECRET)
    args = ap.parse_args()

    secret = json.loads(args.secret.read_text(encoding="utf-8"))
    session_token = str(secret.get("chatgpt_session_token") or "").strip()
    if not session_token:
        _log(ok=False, error="missing_chatgpt_session_token")
        return 2

    proxy = str(args.proxy)
    HAR_DIR.mkdir(parents=True, exist_ok=True)
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _ts()
    har_path = HAR_DIR / f"spa-next-de-{stamp}.har"
    meta_path = HAR_DIR / f"spa-next-de-{stamp}.meta.json"
    png_path = BENCH_DIR / "probe.png"
    _write_probe_png(png_path)
    _log(phase="probe_png", path=str(png_path), bytes=png_path.stat().st_size)

    phases: dict[str, Any] = {
        "upload_attached": False,
        "search_on": False,
        "search_off_confirmed": False,
        "i2i_attempted": False,
        "i2i_mode": False,
    }

    launch = {"headless": True, "humanize": False, "proxy": _proxy_dict(proxy), "os": "windows"}
    try:
        cm = Camoufox(**{**launch, "geoip": True})
        browser = cm.__enter__()
    except Exception as exc:
        _log(phase="geoip_off", error=str(exc)[:160])
        cm = Camoufox(**launch)
        browser = cm.__enter__()
    _log(phase="browser_up", headless=True, proxy=proxy, har=str(har_path))

    try:
        ctx = browser.new_context(record_har_path=str(har_path), record_har_content="embed")
        ctx.add_cookies(
            [
                {
                    "name": SESSION_COOKIE,
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
        page.wait_for_timeout(1500)

        # --- D: upload ---
        _log(phase="upload_start")
        _new_chat(page)
        phases["upload_attached"] = _attach_png(page, png_path)
        # wait briefly for upload network
        page.wait_for_timeout(3000)
        _type_and_send(page, "what color is the square", wait_ms=25000)

        # --- E: search ON ---
        _log(phase="search_on_start")
        _new_chat(page)
        phases["search_on"] = _enable_search(page)
        _type_and_send(
            page,
            "What is the capital of Japan? Use web if needed.",
            wait_ms=35000,
        )

        # --- E: search OFF ---
        _log(phase="search_off_start")
        _new_chat(page)
        chip = _has_search_chip(page)
        phases["search_off_confirmed"] = not chip
        _log(phase="search_chip_check", has_chip=chip, search_off_confirmed=phases["search_off_confirmed"])
        _type_and_send(page, "Say hello in one word.", wait_ms=20000)

        # --- i2i best-effort ---
        _log(phase="i2i_start")
        _new_chat(page)
        attached = _attach_png(page, png_path)
        page.wait_for_timeout(2000)
        mode = _try_create_image(page)
        phases["i2i_mode"] = mode
        phases["i2i_attempted"] = bool(attached or mode)
        if phases["i2i_attempted"]:
            _type_and_send(page, "make a redder version of this image", wait_ms=60000)
        else:
            _log(phase="i2i_skipped", reason="no_attach_and_no_mode")

        meta = {
            "har": str(har_path),
            "captured_at": stamp,
            "proxy": proxy,
            "email": secret.get("email"),
            "has_session_token": True,
            "phases": phases,
            "probe_png": str(png_path),
        }
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        ctx.close()
    finally:
        cm.__exit__(None, None, None)

    counts = _parse_har(har_path)
    har_bytes = har_path.stat().st_size if har_path.exists() else 0
    _log(
        phase="done",
        har=str(har_path),
        meta=str(meta_path),
        bytes=har_bytes,
        phases=phases,
        **counts,
    )
    return 0 if har_bytes > 1000 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _log(phase="fatal", error=str(exc)[:300])
        raise
