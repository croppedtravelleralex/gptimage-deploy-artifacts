#!/usr/bin/env python3
"""Register OpenAI via Camoufox using an existing Proton.me mailbox for OTP.

Serial observe path:
  Proton inbox (Clash 7897) ← OTP
  Camoufox → sticky Webshare → OpenAI signup
  local persist + identity_isolated
  export account blob for Panda import
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import bcrypt
from curl_cffi import requests as crequests
from proton.srp import User as SrpUser
from protonmail.models import PgpPairKeys
from protonmail.pgp import PGP
from protonmail.utils.utils import bcrypt_b64_encode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.yumail_camoufox_openai_register as cam  # noqa: E402
from camoufox.sync_api import Camoufox  # noqa: E402
from services.register.real_browser_register import (  # noqa: E402
    generate_openai_account_password,
    mask_email,
)

PROTON_ACCOUNT_API = "https://account.proton.me/api"
PROTON_MAIL_API = "https://mail.proton.me/api"
PROTON_APP = "web-mail@5.0.76.5"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def proxy_endpoint(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return f"{parsed.hostname}:{parsed.port}"


def proxy_hash(url: str) -> str:
    return hashlib.sha256(proxy_endpoint(url).encode()).hexdigest()[:12]


def webshare_line_to_url(line: str) -> str:
    parts = line.strip().split(":")
    if len(parts) < 4:
        raise ValueError(f"bad_webshare_line:{line[:40]}")
    host, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
    return f"http://{user}:{password}@{host}:{port}"


def extract_modulus_b64(signed: str) -> bytes:
    begin = "-----BEGIN PGP SIGNED MESSAGE-----"
    sig = "-----BEGIN PGP SIGNATURE-----"
    body = signed.split(begin, 1)[1].split(sig, 1)[0]
    lines = body.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    while i < len(lines) and lines[i].strip() != "":
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    payload = "".join(l.strip() for l in lines[i:] if l.strip())
    return base64.b64decode(payload)


class ProtonOtpInbox:
    def __init__(self, email: str, password: str, mail_proxy: str) -> None:
        self.email = email.strip().lower()
        self.username = self.email.split("@", 1)[0]
        self.password = password
        self.proxies = {"http": mail_proxy, "https": mail_proxy} if mail_proxy else {}
        self.session = crequests.Session(impersonate="chrome124", proxies=self.proxies)
        self.tok: dict[str, str] = {}
        self.pgp: PGP | None = None
        self.seen: set[str] = set()

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "x-pm-appversion": PROTON_APP,
            "Accept": "application/vnd.protonmail.v1+json",
            "Origin": "https://account.proton.me",
            "Referer": "https://account.proton.me/",
        }

    def _auth_headers(self) -> dict[str, str]:
        return {
            **self._base_headers(),
            "Authorization": f"Bearer {self.tok['AccessToken']}",
            "x-pm-uid": self.tok["UID"],
            "Origin": "https://mail.proton.me",
            "Referer": "https://mail.proton.me/",
        }

    def login(self) -> None:
        h = self._base_headers()
        info = self.session.post(
            f"{PROTON_ACCOUNT_API}/core/v4/auth/info",
            headers=h,
            json={"Username": self.username},
            timeout=40,
        ).json()
        if "Modulus" not in info:
            raise RuntimeError(f"proton_auth_info:{info}")
        n_bin = extract_modulus_b64(info["Modulus"])
        usr = SrpUser(self.password, n_bin)
        ce = usr.get_challenge()
        cp = usr.process_challenge(
            base64.b64decode(info["Salt"]),
            base64.b64decode(info["ServerEphemeral"]),
            info["Version"],
        )
        auth = self.session.post(
            f"{PROTON_ACCOUNT_API}/core/v4/auth",
            headers=h,
            json={
                "Username": self.username,
                "ClientEphemeral": base64.b64encode(ce).decode(),
                "ClientProof": base64.b64encode(cp).decode(),
                "SRPSession": info["SRPSession"],
                "PersistentCookies": 1,
            },
            timeout=40,
        ).json()
        if "AccessToken" not in auth:
            raise RuntimeError(f"proton_auth_failed:{auth}")
        usr.verify_session(base64.b64decode(auth["ServerProof"]))
        self.tok = {
            "AccessToken": auth["AccessToken"],
            "RefreshToken": auth["RefreshToken"],
            "UID": auth["UID"],
        }
        self._load_pgp()

    def _load_pgp(self) -> None:
        ah = self._auth_headers()
        user = self.session.get(f"{PROTON_MAIL_API}/core/v4/users", headers=ah, timeout=40).json()["User"]
        salts = self.session.get(f"{PROTON_MAIL_API}/core/v4/keys/salts", headers=ah, timeout=40).json()["KeySalts"]
        key_salt = next(x["KeySalt"] for x in salts if x.get("KeySalt"))
        passphrase = bcrypt.hashpw(
            self.password.encode(),
            b"$2y$10$" + bcrypt_b64_encode(base64.b64decode(key_salt))[:22],
        )[29:].decode()
        pgp = PGP()
        uk = user["Keys"][0]
        pgp.pairs_keys.append(
            PgpPairKeys(
                is_user_key=True,
                is_primary=True,
                fingerprint_private=uk["Fingerprint"],
                private_key=uk["PrivateKey"],
                passphrase=passphrase,
                email=user["Email"],
            )
        )
        for addr in self.session.get(f"{PROTON_MAIL_API}/core/v4/addresses", headers=ah, timeout=40).json()["Addresses"]:
            for ak in addr.get("Keys") or []:
                addr_pass = pgp.decrypt(ak["Token"], uk["PrivateKey"], passphrase)
                pgp.pairs_keys.append(
                    PgpPairKeys(
                        is_user_key=False,
                        is_primary=bool(ak.get("Primary")),
                        fingerprint_public=ak["Fingerprints"][0],
                        fingerprint_private=ak["Fingerprints"][1],
                        public_key=ak["PublicKey"],
                        private_key=ak["PrivateKey"],
                        passphrase=addr_pass,
                        email=addr["Email"],
                    )
                )
        self.pgp = pgp

    def _decrypt_body(self, body: str) -> str:
        if not body:
            return ""
        if "-----BEGIN PGP" in body and self.pgp is not None:
            try:
                return self.pgp.decrypt(body)
            except Exception:
                return body
        return body

    def _extract_code(self, text: str) -> str | None:
        for pat in (
            r"verification code[:\s]*([0-9]{4,8})",
            r"Your code is[:\s]*([0-9]{4,8})",
            r"临时验证码[：:\s]*([0-9]{4,8})",
            r"验证码[：:\s]*([0-9]{4,8})",
            r"\b(\d{6})\b",
        ):
            m = re.search(pat, text or "", re.I)
            if m:
                return m.group(1)
        return None

    def wait_code(self, *, not_before: datetime, timeout: float = 180) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ah = self._auth_headers()
            r = self.session.get(
                f"{PROTON_MAIL_API}/mail/v4/messages?Page=0&PageSize=30&Limit=30",
                headers=ah,
                timeout=40,
            )
            if r.status_code == 401:
                self.login()
                continue
            for msg in r.json().get("Messages") or []:
                mid = str(msg.get("ID") or "")
                if not mid or mid in self.seen:
                    continue
                subj = str(msg.get("Subject") or "")
                sender = str(((msg.get("Sender") or {}).get("Address")) or "")
                blob = (subj + " " + sender).lower()
                if not any(k in blob for k in ("openai", "chatgpt", "验证码", "verification", "one-time", "login code")):
                    # still mark older unrelated mail as seen later; peek first
                    pass
                detail = self.session.get(
                    f"{PROTON_MAIL_API}/mail/v4/messages/{mid}",
                    headers=ah,
                    timeout=40,
                ).json().get("Message") or {}
                body = self._decrypt_body(str(detail.get("Body") or ""))
                plain = re.sub(r"<[^>]+>", " ", body)
                plain = re.sub(r"\s+", " ", plain)
                hay = f"{subj} {sender} {plain}"
                code = self._extract_code(hay)
                self.seen.add(mid)
                if not code:
                    continue
                if not any(k in hay.lower() for k in ("openai", "chatgpt", "验证码", "verification", "one-time", "login")):
                    continue
                # TimeFilter: Proton message Time is unix seconds
                ts = int(msg.get("Time") or 0)
                if ts and datetime.fromtimestamp(ts, tz=timezone.utc) < (not_before - timedelta(minutes=2)):
                    continue
                return code
            time.sleep(3)
        raise RuntimeError("proton_otp_timeout")


def probe_proxy(proxy: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "proxy_hash": proxy_hash(proxy)}
    try:
        sess = crequests.Session(impersonate="chrome", proxies={"http": proxy, "https": proxy}, timeout=25)
        r = sess.get("https://api.ipify.org?format=json", timeout=25)
        out["status"] = r.status_code
        if r.status_code == 200:
            out["ok"] = True
            out["ip"] = (r.json() or {}).get("ip")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _collect_tokens(page: Any, captured: dict[str, Any], code_verifier: str, proxy: str) -> dict[str, str]:
    """Prefer network capture; otherwise PKCE-exchange any callback?code= URL."""
    tokens = dict(captured.get("tokens") or {})
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
        url = page.url or ""
        if "code=" in url:
            exchanged = cam._exchange_callback_code(page, code_verifier, proxy)
            if exchanged.get("refresh_token"):
                tokens = exchanged
        if not tokens.get("refresh_token"):
            tokens = cam._wait_for_refresh_token(captured, timeout_sec=30)
    return tokens


def isolate_account(token: str, sticky_proxy: str, probe: dict[str, Any], note: str) -> dict[str, Any]:
    from services.account_service import account_service

    updates = {
        "panda_receive_state": "identity_isolated",
        "identity_last_error": note,
        "identity_evidence_state": "proton_camoufox_observe",
        "proxy": sticky_proxy,
        "proxy_egress_ip": probe.get("ip"),
        "proxy_egress_hash": hashlib.sha256(str(probe.get("ip") or "").encode()).hexdigest()[:16]
        if probe.get("ip")
        else "",
        "register_egress_ip": probe.get("ip"),
        "lifecycle_ip_mode": "sticky_one_ip_full",
        "proxy_provider": "webshare",
        "proxy_scope": "account_sticky",
        "registration_proxy_hash": proxy_hash(sticky_proxy),
    }
    try:
        account_service.update_account_identity(
            token,
            updates,
            reason="proton_camoufox_observe",
            quiet=True,
            clear_isolation=False,
        )
    except Exception:
        account_service.update_account(token, updates, quiet=True)
    account_service.reload_from_storage()
    return account_service.get_account(token) or {}


def export_panda_blob(token: str, out_path: Path) -> dict[str, Any]:
    from services.account_service import account_service

    account_service.reload_from_storage()
    acc = account_service.get_account(token) or {}
    # keep secrets for panda import only
    blob = {
        "email": acc.get("email"),
        "password": acc.get("password"),
        "access_token": acc.get("access_token"),
        "refresh_token": acc.get("refresh_token"),
        "id_token": acc.get("id_token"),
        "chatgpt_session_token": acc.get("chatgpt_session_token"),
        "proxy": acc.get("proxy"),
        "proxy_provider": acc.get("proxy_provider"),
        "lifecycle_ip_mode": acc.get("lifecycle_ip_mode"),
        "proxy_scope": acc.get("proxy_scope"),
        "proxy_egress_ip": acc.get("proxy_egress_ip"),
        "proxy_egress_hash": acc.get("proxy_egress_hash"),
        "register_egress_ip": acc.get("register_egress_ip"),
        "registration_proxy_hash": acc.get("registration_proxy_hash"),
        "panda_receive_state": "identity_isolated",
        "panda_sync_state": "ready",
        "source_type": "web",
        "source_detail": acc.get("source_detail") or "proton_camoufox_observe",
        "fp": acc.get("fp"),
        "status": acc.get("status") or "正常",
        "type": acc.get("type") or "free",
        "created_at": acc.get("created_at") or utc_now(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"email_mask": mask_email(str(blob.get("email") or "")), "path": str(out_path)}


def register_one(
    *,
    proton_email: str,
    proton_password: str,
    sticky_proxy: str,
    mail_proxy: str,
    out_dir: Path,
    browser_proxy: str = "",
) -> dict[str, Any]:
    from services.register import mail_provider

    browser_proxy = (browser_proxy or sticky_proxy).strip()
    report_dir = out_dir / f"proton-reg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{proton_email.split('@')[0][-6:]}"
    report_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "ok": False,
        "email_mask": mask_email(proton_email),
        "proxy_hash": proxy_hash(sticky_proxy),
        "browser_proxy": proxy_endpoint(browser_proxy),
        "ts": utc_now(),
        "out": str(report_dir),
    }

    probe = probe_proxy(browser_proxy)
    result["probe"] = probe
    if not probe.get("ok"):
        result["error"] = "proxy_probe_failed"
        (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    inbox = ProtonOtpInbox(proton_email, proton_password, mail_proxy)
    for attempt in range(1, 6):
        try:
            inbox.login()
            break
        except Exception as exc:  # noqa: BLE001
            if attempt >= 5:
                result["error"] = f"proton_login_failed:{type(exc).__name__}:{exc}"[:300]
                (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                return result
            time.sleep(8 * attempt)
    result["proton_login"] = "ok"

    # Patch mail wait to Proton inbox; Camoufox OTP filler calls wait_for_code(mail, mailbox)
    def _wait_for_code(_mail: dict[str, Any], mailbox: dict[str, Any]) -> str:
        not_before = mailbox.get("_code_not_before")
        if not isinstance(not_before, datetime):
            not_before = datetime.now(timezone.utc) - timedelta(minutes=5)
        return inbox.wait_code(not_before=not_before, timeout=float(_mail.get("wait_timeout") or 180))

    mail_provider.wait_for_code = _wait_for_code  # type: ignore[method-assign]

    email = proton_email.strip().lower()
    openai_password = generate_openai_account_password()
    mailbox = {"provider": "proton", "address": email, "email": email}
    mail = {"wait_timeout": 180, "wait_interval": 3, "providers": [{"type": "proton", "enable": True}]}
    authorize_url, code_verifier = cam._authorize_url(email, screen_hint="signup", client="platform")

    proxy_cfg = cam._proxy_dict(browser_proxy)
    launch_kwargs: dict[str, Any] = {"headless": False, "os": "windows", "humanize": True}
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        # 链式入口是 127.0.0.1，geoip 会失真
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
            boundary = page_boundary - timedelta(minutes=5)
            mailbox["_code_not_before"] = boundary
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
                from scripts._tmp_outlook_camoufox_webshare_register import _fill_about_you_robust

                _fill_about_you_robust(page, email)
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

            tokens = _collect_tokens(page, captured, code_verifier, browser_proxy)
            if not tokens.get("refresh_token"):
                raise RuntimeError("refresh_token_missing")

            add = cam._persist_account(
                email=email,
                password=openai_password,
                tokens=tokens,
                proxy=sticky_proxy,
                source_detail="proton_camoufox_webshare_observe_20260719",
            )
            token = str(tokens.get("access_token") or "")
            refreshed = isolate_account(token, sticky_proxy, probe, "proton_observe_hold_until_mature")
            export = export_panda_blob(token, report_dir / "panda_import.secret.json")
            result.update(
                {
                    "ok": True,
                    "token_hash": hashlib.sha256(token.encode()).hexdigest()[:12],
                    "path": path,
                    "add": {k: add.get(k) for k in ("added", "updated", "total") if k in add},
                    "receive": refreshed.get("panda_receive_state"),
                    "quota": refreshed.get("quota"),
                    "export": export,
                }
            )
        finally:
            browser_cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]

    (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proton-email", required=True)
    parser.add_argument("--proton-password", required=True)
    parser.add_argument("--sticky-proxy", required=True, help="http://user:pass@host:port (account binding)")
    parser.add_argument("--browser-proxy", default="", help="Camoufox/token proxy; default=sticky; use chain http://127.0.0.1:18443")
    parser.add_argument("--mail-proxy", default="http://127.0.0.1:7897")
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "data" / "runlogs" / "proton-openai-observe-20260719"),
    )
    args = parser.parse_args()
    out = register_one(
        proton_email=args.proton_email,
        proton_password=args.proton_password,
        sticky_proxy=args.sticky_proxy,
        mail_proxy=args.mail_proxy,
        out_dir=Path(args.out_dir),
        browser_proxy=args.browser_proxy,
    )
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
