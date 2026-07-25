"""Per-account pre-ticket cache (requirements / turnstile / PoW) with TTL."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from services.config import config

T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
  value: T
  expires_at: float


@dataclass
class PreTicketBundle:
  requirements: Any
  fetched_at: float = field(default_factory=time.time)
  turnstile_solved: bool = False


class PreTicketPool:
  """Cache chat-requirements per access_token to shave 1–4s prepare on hot paths."""

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._cache: dict[str, _CacheEntry[PreTicketBundle]] = {}

  def _ttl_secs(self) -> float:
    try:
      settings = config.get_image_pipeline_settings()
      return max(30.0, float(settings.get("pre_ticket_ttl_secs") or 120))
    except Exception:
      return 120.0

  def get(self, access_token: str) -> PreTicketBundle | None:
    token = str(access_token or "").strip()
    if not token:
      return None
    now = time.time()
    with self._lock:
      entry = self._cache.get(token)
      if entry is None or entry.expires_at <= now:
        if entry is not None:
          self._cache.pop(token, None)
        return None
      return entry.value

  def put(self, access_token: str, bundle: PreTicketBundle) -> None:
    token = str(access_token or "").strip()
    if not token:
      return
    ttl = self._ttl_secs()
    with self._lock:
      self._cache[token] = _CacheEntry(value=bundle, expires_at=time.time() + ttl)

  def get_or_fetch(
    self,
    access_token: str,
    factory: Callable[[], PreTicketBundle],
  ) -> PreTicketBundle:
    cached = self.get(access_token)
    if cached is not None:
      return cached
    bundle = factory()
    self.put(access_token, bundle)
    return bundle

  def invalidate(self, access_token: str) -> None:
    token = str(access_token or "").strip()
    if not token:
      return
    with self._lock:
      self._cache.pop(token, None)

  def evict_expired(self) -> int:
    now = time.time()
    removed = 0
    with self._lock:
      stale = [k for k, e in self._cache.items() if e.expires_at <= now]
      for key in stale:
        self._cache.pop(key, None)
        removed += 1
    return removed

  def snapshot(self) -> dict[str, int | float]:
    now = time.time()
    with self._lock:
      active = sum(1 for e in self._cache.values() if e.expires_at > now)
      return {
        "cached_accounts": active,
        "total_entries": len(self._cache),
        "ttl_secs": self._ttl_secs(),
      }


pre_ticket_pool = PreTicketPool()
