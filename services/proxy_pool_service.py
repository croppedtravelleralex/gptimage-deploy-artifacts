"""Thread-safe proxy pool with tier-aware assignment and lazy loading."""
from __future__ import annotations

import os
import threading
from enum import Enum
from pathlib import Path

from services.account_identity import proxy_binding_hash


class ProxyTier(Enum):
    RESIDENTIAL = "residential"
    DATACENTER = "datacenter"


_SECRET_DIR = Path(__file__).resolve().parents[1] / "data" / "runlogs"

_RESIDENTIAL_FILE = _SECRET_DIR / "webshare_residential_proxies.secret.txt"
_DATACENTER_FILE = _SECRET_DIR / "webshare_100_proxies.secret.txt"
_PANDA_DATACENTER = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")


def _parse_line(line: str) -> str:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    parts = text.split(":")
    if len(parts) >= 4:
        from urllib.parse import quote

        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return ""


def _resolve_datacenter_path() -> Path:
    override = str(os.environ.get("GPTIMAGE_WEBSHARE_POOL") or "").strip()
    if override:
        return Path(override)
    for candidate in (_PANDA_DATACENTER, _DATACENTER_FILE):
        if candidate.is_file():
            return candidate
    return _DATACENTER_FILE


class ProxyPoolService:
    """Lazy-loaded pool manager for residential and datacenter proxy tiers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._residential: list[str] = []
        self._datacenter: list[str] = []
        self._residential_idx = 0
        self._datacenter_idx = 0

    # -- public api -------------------------------------------------------

    def load_pools(self) -> None:
        """Read proxy URLs from secret files. Idempotent if reloading."""
        residential_path = _RESIDENTIAL_FILE
        datacenter_path = _resolve_datacenter_path()

        residential: list[str] = []
        datacenter: list[str] = []
        seen_r: set[str] = set()
        seen_d: set[str] = set()

        if residential_path.is_file():
            for line in residential_path.read_text(encoding="utf-8", errors="replace").splitlines():
                url = _parse_line(line)
                if not url:
                    continue
                key = proxy_binding_hash(url)
                if not key or key in seen_r:
                    continue
                seen_r.add(key)
                residential.append(url)

        if datacenter_path.is_file():
            for line in datacenter_path.read_text(encoding="utf-8", errors="replace").splitlines():
                url = _parse_line(line)
                if not url:
                    continue
                key = proxy_binding_hash(url)
                if not key or key in seen_d:
                    continue
                seen_d.add(key)
                datacenter.append(url)

        with self._lock:
            self._residential = residential
            self._datacenter = datacenter
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_pools()

    def assign_proxy(self, tier: ProxyTier | str) -> str:
        """Return the next available proxy URL from *tier*, round-robin.

        Returns empty string when the pool is exhausted.
        """
        self._ensure_loaded()
        tier_enum = ProxyTier(tier) if isinstance(tier, str) else tier

        with self._lock:
            if tier_enum is ProxyTier.RESIDENTIAL:
                pool = self._residential
                idx = self._residential_idx
            else:
                pool = self._datacenter
                idx = self._datacenter_idx

            if not pool:
                return ""

            url = pool[idx % len(pool)]
            next_idx = (idx + 1) % len(pool)

            if tier_enum is ProxyTier.RESIDENTIAL:
                self._residential_idx = next_idx
            else:
                self._datacenter_idx = next_idx

            return url

    @staticmethod
    def binding_key(proxy: object) -> str:
        """Return a stable hashed identifier for *proxy* (host:port+credentials)."""
        return proxy_binding_hash(proxy)

    def is_residential(self, proxy_url: str) -> bool:
        """Return *True* when *proxy_url* belongs to the residential pool."""
        self._ensure_loaded()
        if not proxy_url:
            return False
        key = proxy_binding_hash(proxy_url)
        if not key:
            return False
        with self._lock:
            for entry in self._residential:
                if entry and proxy_binding_hash(entry) == key:
                    return True
            return False

    def count(self, tier: ProxyTier | str) -> int:
        """Return the number of proxies in *tier*."""
        self._ensure_loaded()
        t = ProxyTier(tier) if isinstance(tier, str) else tier
        with self._lock:
            return len(self._residential) if t is ProxyTier.RESIDENTIAL else len(self._datacenter)


proxy_pool_service = ProxyPoolService()
