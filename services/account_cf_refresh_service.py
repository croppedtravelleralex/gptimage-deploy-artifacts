"""Background refresh for expired per-account CF stamps and stale batch scan reports."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from services.account_service import account_service
from services.config import config
from services.proxy_cf_eligibility import (
    account_cf_cache_ok,
    account_needs_cf_stamp_refresh,
    cf_probe_account_fields,
    cf_scan_index_stale,
    load_scan_index,
    scan_stale_sec,
)
from services.proxy_cf_probe import probe_proxy_cf_with_retries
from services.proxy_quarantine import (
    clear_gpt_unavailable,
    is_gpt_unavailable_proxy,
    mark_gpt_unavailable,
    proxy_endpoint_key,
)

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_CF_REFRESH_SETTINGS: dict[str, object] = {
    "enabled": True,
    "interval_sec": 300.0,
    "startup_delay_sec": 90.0,
    "max_probes_per_tick": 4,
    "probe_timeout_sec": 45.0,
    "min_retry_sec": 3600.0,
    "trigger_batch_scan_when_stale": True,
    "batch_scan_cooldown_sec": 1800.0,
}


def _settings() -> dict[str, Any]:
    return config.get_account_cf_refresh_settings()


class AccountCfRefreshService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_retry_at: dict[str, float] = {}
        self._last_batch_scan_at = 0.0
        self._status: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "last_tick_at": None,
            "next_tick_at": None,
            "last_error": "",
            "last_tick": {},
            "totals": {"probed": 0, "restamped": 0, "failed": 0, "batch_scans": 0},
        }

    def status(self) -> dict[str, Any]:
        settings = _settings()
        with self._lock:
            out = dict(self._status)
        out["settings"] = settings
        out["enabled"] = bool(settings.get("enabled"))
        out["scan_index_size"] = len(load_scan_index())
        out["scan_stale_sec"] = scan_stale_sec()
        out["scan_index_stale"] = cf_scan_index_stale()
        out["pending_retry"] = sum(
            1 for ts in self._next_retry_at.values() if float(ts or 0) > time.time()
        )
        return out

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-cf-refresh", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        settings = _settings()
        if not settings.get("enabled") and not force:
            raise RuntimeError("account_cf_refresh is disabled")
        return self._tick(settings, force=force)

    def _loop(self) -> None:
        settings = _settings()
        startup_delay = max(0.0, float(settings.get("startup_delay_sec") or 0.0))
        if startup_delay > 0:
            self._stop.wait(timeout=startup_delay)
        while not self._stop.is_set():
            settings = _settings()
            enabled = bool(settings.get("enabled"))
            with self._lock:
                self._status["enabled"] = enabled
                self._status["running"] = enabled
            if enabled:
                try:
                    self._tick(settings)
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._status["last_error"] = str(exc)[:240]
            wait_sec = max(60.0, float(settings.get("interval_sec") or 300.0))
            next_at = time.time() + wait_sec
            with self._lock:
                self._status["next_tick_at"] = datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat()
            if self._stop.wait(timeout=wait_sec):
                break
        with self._lock:
            self._status["running"] = False

    def _maybe_trigger_batch_scan(self, settings: dict[str, Any]) -> dict[str, Any] | None:
        if not bool(settings.get("trigger_batch_scan_when_stale", True)):
            return None
        if not cf_scan_index_stale():
            return None
        cooldown = max(300.0, float(settings.get("batch_scan_cooldown_sec") or 1800.0))
        now = time.time()
        if now - self._last_batch_scan_at < cooldown:
            return {"skipped": "batch_scan_cooldown"}
        from services.webshare_cf_scan_service import webshare_cf_scan_service

        scan_settings = config.get_webshare_cf_scan_settings()
        if not str(scan_settings.get("pool_path") or "").strip():
            return {"skipped": "no_pool_path"}
        try:
            report = webshare_cf_scan_service.run_once(force=True)
            self._last_batch_scan_at = now
            with self._lock:
                self._status["totals"]["batch_scans"] = int(self._status["totals"].get("batch_scans") or 0) + 1
            return {"batch_scan": report.get("summary")}
        except Exception as exc:  # noqa: BLE001
            return {"batch_scan_error": str(exc)[:200]}

    def _list_candidates(self) -> list[dict[str, Any]]:
        account_service.reload_from_storage()
        peer = account_service._build_sched_peer_index_locked()
        now = time.time()
        min_retry = max(60.0, float(_settings().get("min_retry_sec") or 3600.0))
        rows: list[dict[str, Any]] = []
        with account_service._lock:
            items = [dict(a) for a in account_service._accounts.values()]
        for acc in items:
            if not account_needs_cf_stamp_refresh(acc, peer_index=peer):
                continue
            email = str(acc.get("email") or "").strip().lower()
            retry_at = float(self._next_retry_at.get(email) or 0)
            if retry_at > now:
                continue
            proxy = str(acc.get("proxy") or "").strip()
            try:
                ok_at = float(acc.get("proxy_cf_ok_at") or 0)
            except (TypeError, ValueError):
                ok_at = 0.0
            rows.append(
                {
                    "email": acc.get("email"),
                    "token": str(acc.get("access_token") or ""),
                    "proxy": proxy,
                    "endpoint": proxy_endpoint_key(proxy),
                    "stamp_age_sec": (now - ok_at) if ok_at > 0 else None,
                    "cache_ok": account_cf_cache_ok(acc, proxy_url=proxy),
                }
            )
        rows.sort(key=lambda item: float(item.get("stamp_age_sec") or 10**12), reverse=True)
        return rows

    def _tick(self, settings: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        batch_scan = self._maybe_trigger_batch_scan(settings)
        candidates = self._list_candidates()
        limit = max(1, int(settings.get("max_probes_per_tick") or 4))
        if force:
            limit = max(limit, len(candidates))
        selected = candidates[:limit]
        timeout = float(settings.get("probe_timeout_sec") or 45.0)
        min_retry = max(60.0, float(settings.get("min_retry_sec") or 3600.0))
        now = time.time()
        rows: list[dict[str, Any]] = []
        for item in selected:
            email = str(item.get("email") or "").strip().lower()
            proxy = str(item.get("proxy") or "").strip()
            token = str(item.get("token") or "").strip()
            if not proxy or not token:
                continue
            was_quarantined = is_gpt_unavailable_proxy(proxy)
            clear_gpt_unavailable(proxy)
            probe = probe_proxy_cf_with_retries(proxy, timeout=timeout)
            cf_ok = bool(probe.get("ok"))
            row = {
                "email": item.get("email"),
                "endpoint": item.get("endpoint"),
                "cf_ok": cf_ok,
                "classification": probe.get("cf_classification"),
                "elapsed_ms": probe.get("elapsed_ms"),
                "probe_attempts": probe.get("probe_attempts"),
                "probe_retries": probe.get("probe_retries"),
                "was_quarantined": was_quarantined,
                "restamped": False,
            }
            if cf_ok:
                fields = cf_probe_account_fields(proxy, probe)
                account_service.update_account_identity(token, fields, reason="cf_refresh_loop", quiet=True)
                row["restamped"] = True
                self._next_retry_at.pop(email, None)
                with self._lock:
                    self._status["totals"]["restamped"] = int(self._status["totals"].get("restamped") or 0) + 1
            else:
                # The endpoint was cleared above so the probe could run. It failed, so put
                # it back: without this the loop would slowly drain the quarantine list.
                mark_gpt_unavailable(proxy, reason="cf403_refresh_probe", former_account=email)
                row["re_quarantined"] = True
                self._next_retry_at[email] = now + min_retry
                with self._lock:
                    self._status["totals"]["failed"] = int(self._status["totals"].get("failed") or 0) + 1
            with self._lock:
                self._status["totals"]["probed"] = int(self._status["totals"].get("probed") or 0) + 1
            rows.append(row)

        tick = {
            "candidates": len(candidates),
            "probed": len(rows),
            "cf_ok": sum(1 for row in rows if row.get("cf_ok")),
            "restamped": sum(1 for row in rows if row.get("restamped")),
            "rows": rows,
            "batch_scan": batch_scan,
        }
        with self._lock:
            self._status["last_tick_at"] = datetime.now(timezone.utc).isoformat()
            self._status["last_tick"] = tick
            self._status["last_error"] = ""
        logger.info({"event": "account_cf_refresh_tick", **{k: tick[k] for k in ("candidates", "probed", "cf_ok", "restamped")}})
        return tick


account_cf_refresh_service = AccountCfRefreshService()
