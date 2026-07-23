#!/usr/bin/env python3
"""手动注册辅助：Camoufox + panda/UDeal 代理，同时打开多个代理/指纹检测页。

浏览器会一直开着，直到你在终端按 Enter 关闭。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402

PROXY = "http://127.0.0.1:18030"
PAGES = [
    ("ipify", "https://api.ipify.org?format=json"),
    ("cf_trace", "https://cloudflare.com/cdn-cgi/trace"),
    ("ipinfo", "https://ipinfo.io/json"),
    ("ip_api", "https://ip-api.com/json/?fields=status,message,country,countryCode,regionName,city,isp,org,as,query,proxy,hosting"),
    ("whoer", "https://whoer.net/"),
    ("browserleaks_ip", "https://browserleaks.com/ip"),
    ("browserleaks_webrtc", "https://browserleaks.com/webrtc"),
    ("browserleaks_js", "https://browserleaks.com/javascript"),
    ("pixelscan", "https://pixelscan.net/"),
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/"),
    ("amiunique", "https://amiunique.org/fingerprint"),
    ("chatgpt", "https://chatgpt.com/"),
    ("auth_openai", "https://auth.openai.com/"),
]


def _proxy_dict(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def main() -> int:
    proxy = (sys.argv[1] if len(sys.argv) > 1 else PROXY).strip()
    meta_path = ROOT / "data" / "runlogs" / "udeal_camoufox_egress_last.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    print(
        json.dumps(
            {
                "phase": "manual_camoufox_start",
                "proxy": proxy,
                "session": meta.get("session"),
                "expected_ip": meta.get("remote_ip") or (meta.get("trace") or {}).get("ip"),
                "loc": (meta.get("trace") or {}).get("loc"),
                "tabs": [n for n, _ in PAGES],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    launch_kwargs = {
        "headless": False,
        "os": "windows",
        "humanize": True,
        "proxy": _proxy_dict(proxy),
        "geoip": True,
    }
    try:
        browser_cm = Camoufox(**launch_kwargs)
        browser = browser_cm.__enter__()
    except Exception as exc:
        print(json.dumps({"phase": "geoip_disabled", "error": str(exc)[:160]}, ensure_ascii=False), flush=True)
        launch_kwargs.pop("geoip", None)
        browser_cm = Camoufox(**launch_kwargs)
        browser = browser_cm.__enter__()

    pages = []
    try:
        for name, url in PAGES:
            page = browser.new_page()
            pages.append(page)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                print(json.dumps({"phase": "tab_ok", "name": name, "url": url, "title": page.title()}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(
                    json.dumps({"phase": "tab_warn", "name": name, "url": url, "error": f"{type(exc).__name__}: {exc}"[:220]}, ensure_ascii=False),
                    flush=True,
                )
            time.sleep(0.4)

        print(
            json.dumps(
                {
                    "phase": "ready_for_manual",
                    "hint": "代理/指纹页已打开。请自行去 ChatGPT / auth.openai.com 注册。终端按 Enter 关闭浏览器。",
                    "proxy": proxy,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            input()
        except EOFError:
            # 非交互环境：挂着直到进程被杀
            print(json.dumps({"phase": "wait_forever", "note": "no_tty"}, ensure_ascii=False), flush=True)
            while True:
                time.sleep(3600)
    finally:
        browser_cm.__exit__(None, None, None)
        print(json.dumps({"phase": "closed"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
