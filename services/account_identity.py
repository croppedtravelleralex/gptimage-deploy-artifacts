"""账号级代理、出口与浏览器身份的持久化约束。"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import unquote, urlsplit

from services.account_fingerprint import ensure_complete_fp, normalize_fp


FP_REQUIRED_FIELDS = (
    "user-agent",
    "impersonate",
    "oai-device-id",
    "oai-session-id",
    "sec-ch-ua",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
)

PROXY_IDENTITY_FIELDS = (
    "proxy",
    "proxy_provider",
    "proxy_scope",
    "proxy_binding_hash",
    "proxy_egress_hash",
    "registration_proxy_hash",
    "registration_proxy_scope",
    "registration_proxy_endpoint",
    "registration_egress_hash",
    "lifecycle_ip_mode",
)

PANDA_REQUIRED_IDENTITY_FIELDS = (
    "proxy",
    "proxy_provider",
    "proxy_scope",
    "proxy_binding_hash",
    "proxy_egress_hash",
    "registration_proxy_hash",
    "lifecycle_ip_mode",
    "fp",
)

_LOCAL_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _text(value: object) -> str:
    return str(value or "").strip()


def proxy_binding_hash(value: object) -> str:
    """返回包含代理认证身份的不可逆签名，不暴露代理凭据。

    仅按 ``host:port`` 去重会把同入口、不同用户名会话的固定节点折叠为
    一条。这里把 scheme/host/port/username/password 都纳入摘要，日志和
    报告只保存摘要。
    """

    raw = _text(getattr(value, "url", value))
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(candidate)
        scheme = _text(parsed.scheme).lower() or "http"
        host = _text(parsed.hostname).lower()
        port = int(parsed.port or (443 if scheme == "https" else 80))
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        canonical = "\0".join((scheme, host, str(port), username, password))
    except (TypeError, ValueError):
        canonical = raw
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def is_proxy_reachable_from_panda(value: object) -> bool:
    raw = _text(getattr(value, "url", value))
    if not raw:
        return False
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        return _text(urlsplit(candidate).hostname).lower() not in _LOCAL_PROXY_HOSTS
    except ValueError:
        return False


def normalize_account_identity(account: dict[str, Any]) -> dict[str, Any]:
    """补全可本地推导的身份字段，并生成一次后可持久复用的 fp。"""

    normalized = dict(account or {})
    proxy = _text(normalized.get("proxy"))
    normalized["proxy"] = proxy
    if proxy:
        binding_hash = proxy_binding_hash(proxy)
        normalized["proxy_binding_hash"] = binding_hash
        lifecycle = _text(normalized.get("lifecycle_ip_mode")).lower()
        if not _text(normalized.get("proxy_scope")):
            normalized["proxy_scope"] = (
                "account_sticky" if lifecycle in {"sticky_one_ip_full", "account_sticky"} else "account_proxy"
            )
        # sticky_one_ip_full 明确表示注册到运行全程使用同一账号级代理。
        if lifecycle == "sticky_one_ip_full" and not _text(normalized.get("registration_proxy_hash")):
            normalized["registration_proxy_hash"] = binding_hash
        if (
            lifecycle == "sticky_one_ip_full"
            and _text(normalized.get("proxy_egress_hash"))
            and not _text(normalized.get("registration_egress_hash"))
        ):
            normalized["registration_egress_hash"] = _text(normalized.get("proxy_egress_hash"))
    else:
        normalized["proxy_binding_hash"] = None

    fp, _ = ensure_complete_fp(normalized)
    normalized["fp"] = fp
    return normalized


def merge_account_identity(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    *,
    allow_rebind: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """合并账号更新；普通更新只补缺失字段，不替换已绑定身份。"""

    current = dict(current or {})
    merged = dict(incoming or {})
    conflicts: set[str] = set()

    if allow_rebind:
        current_fp = normalize_fp(current.get("fp"))
        incoming_fp = normalize_fp(merged.get("fp")) if "fp" in merged else {}
        if incoming_fp:
            merged["fp"] = {**current_fp, **incoming_fp}
        if "proxy" in merged:
            proxy = _text(merged.get("proxy"))
            merged["proxy"] = proxy
            merged["proxy_binding_hash"] = proxy_binding_hash(proxy) if proxy else None
            if not proxy:
                for field in PROXY_IDENTITY_FIELDS:
                    if field not in {"proxy", "proxy_binding_hash"}:
                        merged.setdefault(field, None)
        return merged, []

    current_proxy = _text(current.get("proxy"))
    incoming_proxy_present = "proxy" in merged
    incoming_proxy = _text(merged.get("proxy")) if incoming_proxy_present else current_proxy
    proxy_conflict = bool(
        current_proxy
        and incoming_proxy_present
        and (
            not incoming_proxy
            or proxy_binding_hash(incoming_proxy) != proxy_binding_hash(current_proxy)
        )
    )
    if proxy_conflict:
        merged["proxy"] = current_proxy
        conflicts.add("proxy")

    for field in PROXY_IDENTITY_FIELDS:
        if field == "proxy" or field not in merged:
            continue
        old = current.get(field)
        new = merged.get(field)
        old_text = _text(old)
        new_text = _text(new)
        if old_text and (not new_text or new_text != old_text):
            merged[field] = old
            conflicts.add(field)

    if proxy_conflict:
        for field in PROXY_IDENTITY_FIELDS:
            if field != "proxy" and field in current:
                merged[field] = current.get(field)

    if "fp" in merged:
        current_fp = normalize_fp(current.get("fp"))
        incoming_fp = normalize_fp(merged.get("fp"))
        safe_fp = dict(current_fp)
        for key, value in incoming_fp.items():
            if key in current_fp and current_fp[key] != value:
                conflicts.add("fp")
                continue
            safe_fp[key] = value
        merged["fp"] = safe_fp or current_fp

    return merged, sorted(conflicts)


def missing_panda_identity_fields(account: dict[str, Any] | None) -> list[str]:
    """列出上传/接收验收前缺失的账号级身份字段。"""

    item = dict(account or {})
    missing: list[str] = []
    proxy = _text(item.get("proxy"))
    if not proxy:
        missing.append("proxy")
    elif not is_proxy_reachable_from_panda(proxy):
        missing.append("proxy_reachable_from_panda")
    for field in PANDA_REQUIRED_IDENTITY_FIELDS:
        if field in {"proxy", "fp"}:
            continue
        if not _text(item.get(field)):
            missing.append(field)
    fp = normalize_fp(item.get("fp"))
    if any(not _text(fp.get(field)) for field in FP_REQUIRED_FIELDS):
        missing.append("fp")
    return list(dict.fromkeys(missing))
