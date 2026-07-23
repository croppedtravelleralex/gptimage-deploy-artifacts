#!/usr/bin/env python3
"""差 IP（账号 Webshare）暖机交接：Camoufox page.goto 暖机 → curl_cffi 对照。

APIRequest 经 Webshare 易 ECONNRESET；改用真页面导航取 cookie。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402
from curl_cffi import requests  # noqa: E402

from services.openai_backend_api import DEFAULT_CLIENT_BUILD_NUMBER, DEFAULT_CLIENT_VERSION  # noqa: E402
from utils.helper import anonymize_token, new_uuid  # noqa: E402
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources  # noqa: E402
from utils.turnstile import solve_turnstile_token  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"
BASE = "https://chatgpt.com"


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _proxy_dict(proxy: str) -> dict[str, str]:
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port or 80}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def _fp(secret: dict) -> dict:
    fp = dict(secret.get("fp") if isinstance(secret.get("fp"), dict) else {})
    fp.setdefault(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    )
    fp.setdefault("impersonate", "chrome131")
    fp.setdefault("oai-device-id", new_uuid())
    fp.setdefault("oai-session-id", new_uuid())
    fp.setdefault("accept-language", "en-US,en;q=0.9")
    return fp


def _hdr(fp: dict, path: str, token: str, cookie: str = "") -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": fp["user-agent"],
        "Accept-Language": fp["accept-language"],
        "Authorization": f"Bearer {token}",
        "OAI-Device-Id": fp["oai-device-id"],
        "OAI-Session-Id": fp["oai-session-id"],
        "OAI-Client-Version": DEFAULT_CLIENT_VERSION,
        "OAI-Client-Build-Number": DEFAULT_CLIENT_BUILD_NUMBER,
        "Origin": BASE,
        "Referer": BASE + "/",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def _cookie_header(cookies: list[dict]) -> tuple[str, list[str]]:
    parts, names = [], []
    for c in cookies:
        n = str(c.get("name") or "").strip()
        if not n:
            continue
        names.append(n)
        parts.append(f"{n}={c.get('value') or ''}")
    return "; ".join(parts), sorted(set(names))


def _camoufox_page_warm(proxy: str, session_token: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "home_status": 0, "cookie_names": []}
    launch = {"headless": True, "humanize": False, "proxy": _proxy_dict(proxy), "os": "windows"}
    try:
        cm = Camoufox(**{**launch, "geoip": True})
        browser = cm.__enter__()
    except Exception:
        cm = Camoufox(**launch)
        browser = cm.__enter__()
    try:
        ctx = browser.new_context()
        if session_token:
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
        t0 = time.time()
        try:
            page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=60000)
            eg = json.loads(page.inner_text("body") or "{}")
            out["egress"] = {"ok": True, "ip": eg.get("ip"), "ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            out["egress"] = {"ok": False, "error": str(exc)[:120]}
        _log(phase="warm_egress", **out.get("egress", {}))

        resp = page.goto(BASE + "/", wait_until="domcontentloaded", timeout=120000)
        out["home_status"] = int(resp.status if resp else 0)
        page.wait_for_timeout(2500)
        # poke sentinel via page.request after cookies settled
        try:
            html = page.content()
            scripts, build = parse_pow_resources(html)
            p_token = build_legacy_requirements_token(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
                scripts,
                build,
            )
            prep = page.request.post(
                BASE + "/backend-api/sentinel/chat-requirements/prepare",
                data=json.dumps({"p": p_token}),
                headers={"Content-Type": "application/json"},
                timeout=90000,
            )
            out["req_prepare_status"] = int(prep.status)
            _log(phase="warm_req_prepare", status=out["req_prepare_status"])
        except Exception as exc:
            out["req_prepare_error"] = str(exc)[:160]
            _log(phase="warm_req_prepare_error", error=out["req_prepare_error"])

        cookie_header, names = _cookie_header(ctx.cookies())
        out["cookie_names"] = names
        out["cookie_count"] = len(names)
        out["cookie_header"] = cookie_header
        out["ok"] = out.get("home_status", 0) in (200, 304) and len(names) > 0
        _log(phase="warm_done", ok=out["ok"], home=out["home_status"], cookie_names=names)
    finally:
        cm.__exit__(None, None, None)
    return out


def _curl_prepare(fp: dict, token: str, proxy: str, cookie: str, label: str) -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "ok": False}
    proxies = {"http": proxy, "https": proxy}
    for attempt in range(1, 6):
        try:
            s = requests.Session(impersonate=fp["impersonate"])
            home = s.get(
                BASE + "/",
                headers={"User-Agent": fp["user-agent"], **({"Cookie": cookie} if cookie else {})},
                proxies=proxies,
                timeout=90,
            )
            out["home_status"] = home.status_code
            scripts, build = parse_pow_resources(home.text or "")
            p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)
            prep = s.post(
                BASE + "/backend-api/sentinel/chat-requirements/prepare",
                headers=_hdr(fp, "/backend-api/sentinel/chat-requirements/prepare", token, cookie),
                json={"p": p_token},
                proxies=proxies,
                timeout=90,
            )
            out["req_prepare_status"] = prep.status_code
            out["ok"] = prep.status_code == 200
            body = (prep.text or "")[:80]
            _log(phase="curl_prepare", label=label, status=prep.status_code, home=home.status_code, attempt=attempt)
            out["attempt"] = attempt
            return out
        except Exception as exc:
            _log(phase="curl_retry", label=label, attempt=attempt, error=str(exc)[:160])
            time.sleep(0.8)
    out["error"] = "exhausted_retries"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", type=Path, default=SECRET)
    args = ap.parse_args()
    secret = json.loads(args.secret.read_text(encoding="utf-8"))
    proxy = str(secret.get("proxy") or "").strip()
    token = str(secret.get("access_token") or "").strip()
    session = str(secret.get("chatgpt_session_token") or "").strip()
    if not proxy or not token:
        _log(ok=False, error="missing_proxy_or_token")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fp = _fp(secret)
    _log(phase="start", proxy_host=urlparse(proxy).hostname, email=secret.get("email"))

    warm = _camoufox_page_warm(proxy, session)
    cookie = str(warm.pop("cookie_header", "") or "")
    report = {
        "mode": "warm_handoff_webshare_page",
        "proxy_host": urlparse(proxy).hostname,
        "email": secret.get("email"),
        "token_fp": anonymize_token(token),
        "camoufox_warm": {k: v for k, v in warm.items() if k != "cookie_header"},
        "curl_arms": [],
        "ok": False,
    }

    cold = _curl_prepare(fp, token, proxy, "", "cold")
    warm_arm = _curl_prepare(fp, token, proxy, cookie, "warm")
    report["curl_arms"] = [cold, warm_arm]
    report["compare"] = {
        "cold_prepare": cold.get("req_prepare_status"),
        "warm_prepare": warm_arm.get("req_prepare_status"),
        "cold_ok": cold.get("ok"),
        "warm_ok": warm_arm.get("ok"),
        "camoufox_home": warm.get("home_status"),
        "camoufox_ok": warm.get("ok"),
    }
    report["ok"] = bool(warm.get("ok")) and (bool(cold.get("ok")) or bool(warm_arm.get("ok")))
    path = OUT_DIR / f"warm_handoff_webshare_{int(time.time())}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(phase="done", path=str(path), compare=report["compare"], ok=report["ok"])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
