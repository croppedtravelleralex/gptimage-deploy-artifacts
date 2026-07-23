"""Background Webshare pool CF403 scanner with irregular intervals."""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from services.config import config
from services.proxy_cf_failover import load_proxy_pool
from services.proxy_cf_probe import probe_proxy_cf
from services.proxy_quarantine import (
    is_gpt_unavailable_proxy,
    list_quarantine_entries,
    mark_gpt_unavailable,
    proxy_endpoint_key,
)

DEFAULT_WEBSHARE_CF_SCAN_SETTINGS: dict[str, object] = {
    "enabled": False,
    "pool_path": "",
    "interval_min_sec": 3600,
    "interval_max_sec": 14400,
    "batch_size": 20,
    "workers": 4,
    "auto_quarantine": True,
    "skip_quarantined": True,
    "active_window": ["08:00", "23:00"],
    "timezone": "Asia/Singapore",
    "startup_delay_sec": 120,
    "probe_timeout_sec": 45.0,
}


def _settings() -> dict[str, Any]:
    raw = config.get_webshare_cf_scan_settings() if hasattr(config, "get_webshare_cf_scan_settings") else {}
    return raw if isinstance(raw, dict) else {}


def _pool_path(settings: dict[str, Any]) -> Path | None:
    override = str(settings.get("pool_path") or "").strip()
    if not override:
        return None
    candidate = Path(override)
    if candidate.is_file():
        return candidate
    try:
        from services.config import DATA_DIR

        basename = candidate.name
        for rel in (Path("runlogs") / basename, Path(basename)):
            under_data = Path(DATA_DIR) / rel
            if under_data.is_file():
                return under_data
    except Exception:
        pass
    return None


def _latest_report_path() -> Path:
    try:
        from services.config import DATA_DIR

        return Path(DATA_DIR) / "runlogs" / "webshare_cf_scan_latest.json"
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "runlogs" / "webshare_cf_scan_latest.json"


def _in_active_window(settings: dict[str, Any]) -> bool:
    window = settings.get("active_window")
    if not isinstance(window, (list, tuple)) or len(window) < 2:
        return True
    tz_name = str(settings.get("timezone") or "Asia/Singapore").strip() or "Asia/Singapore"
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    start = str(window[0] or "00:00")
    end = str(window[1] or "23:59")
    current = now.strftime("%H:%M")
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _next_interval_sec(settings: dict[str, Any]) -> float:
    low = max(300.0, float(settings.get("interval_min_sec") or 3600))
    high = max(low, float(settings.get("interval_max_sec") or low))
    return random.uniform(low, high)


def _select_batch(pool: list[str], *, offset: int, batch_size: int, skip_quarantined: bool) -> tuple[list[str], int]:
    if not pool:
        return [], 0
    size = max(1, int(batch_size or 1))
    selected: list[str] = []
    cursor = int(offset or 0) % len(pool)
    scanned = 0
    while len(selected) < size and scanned < len(pool):
        proxy = pool[cursor]
        cursor = (cursor + 1) % len(pool)
        scanned += 1
        if skip_quarantined and is_gpt_unavailable_proxy(proxy):
            continue
        selected.append(proxy)
    next_offset = cursor % len(pool)
    return selected, next_offset


def _scan_nodes_by_endpoint(latest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(latest, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for node in latest.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        endpoint = str(node.get("proxy_endpoint") or node.get("proxy_hash") or "").strip().lower()
        if endpoint:
            out[endpoint] = node
    return out


def build_pool_inventory(settings: dict[str, Any], *, latest_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarize Webshare pool: available vs CF403/quarantined nodes with probe details."""
    pool = load_proxy_pool(_pool_path(settings))
    pool_endpoints: list[str] = []
    seen_pool: set[str] = set()
    for proxy in pool:
        endpoint = proxy_endpoint_key(proxy)
        if not endpoint or endpoint in seen_pool:
            continue
        seen_pool.add(endpoint)
        pool_endpoints.append(endpoint)

    scan_index = _scan_nodes_by_endpoint(latest_report)
    scan_at = str((latest_report or {}).get("generated_at") or "").strip() or None
    quarantine_entries = list_quarantine_entries()
    quarantine_by_endpoint = {
        str(item.get("endpoint") or "").lower(): item for item in quarantine_entries if item.get("endpoint")
    }

    available_endpoints: list[dict[str, Any]] = []
    cf403_nodes: list[dict[str, Any]] = []
    for endpoint in sorted(pool_endpoints):
        blocked = is_gpt_unavailable_proxy(endpoint)
        if not blocked:
            scan_node = scan_index.get(endpoint) or {}
            available_endpoints.append(
                {
                    "endpoint": endpoint,
                    "host": endpoint.split(":", 1)[0],
                    "last_probe_ok": bool(scan_node.get("ok")) if scan_node else None,
                    "last_scan_at": scan_at if scan_node else None,
                    "egress_ip": str((scan_node.get("egress") or {}).get("ip") or "") or None,
                }
            )
            continue
        note = quarantine_by_endpoint.get(endpoint, {})
        scan_node = scan_index.get(endpoint) or {}
        cf403_nodes.append(
            {
                "endpoint": endpoint,
                "host": endpoint.split(":", 1)[0],
                "reason": str(note.get("reason") or scan_node.get("cf_classification") or "cf403"),
                "former_account": note.get("former_account"),
                "last_scan_at": scan_at if scan_node else None,
                "home_status": scan_node.get("home_status"),
                "requirements_status": scan_node.get("requirements_status"),
                "cf_classification": scan_node.get("cf_classification"),
                "egress_ip": str((scan_node.get("egress") or {}).get("ip") or "") or None,
                "error": str(scan_node.get("error") or "")[:240] or None,
                "quarantined": True,
            }
        )

    quarantine_in_pool = {str(item.get("endpoint") or "").lower() for item in cf403_nodes}
    quarantine_outside_pool = [
        item
        for item in quarantine_entries
        if str(item.get("endpoint") or "").lower() not in quarantine_in_pool
    ]

    return {
        "pool_total": len(pool_endpoints),
        "available_count": len(available_endpoints),
        "cf403_count": len(cf403_nodes),
        "quarantine_total": len(quarantine_entries),
        "quarantine_in_pool_count": len(cf403_nodes),
        "quarantine_outside_pool_count": len(quarantine_outside_pool),
        "available_endpoints": available_endpoints,
        "cf403_nodes": cf403_nodes,
        "quarantine_outside_pool": quarantine_outside_pool,
    }


class WebshareCfScanService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool_offset = 0
        self._running = False
        self._status: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "last_scan_at": None,
            "next_scan_at": None,
            "last_error": "",
            "last_summary": {},
            "pool_total": 0,
            "scanned_total": 0,
        }

    def status(self) -> dict[str, Any]:
        settings = _settings()
        latest = self._read_latest_report()
        inventory = build_pool_inventory(settings, latest_report=latest)
        with self._lock:
            out = dict(self._status)
            out["settings"] = settings
            out["enabled"] = bool(settings.get("enabled"))
            out["pool_offset"] = self._pool_offset
            out["latest_report_path"] = str(_latest_report_path())
            out["inventory"] = inventory
            out["counts"] = {
                "pool_total": inventory["pool_total"],
                "available_count": inventory["available_count"],
                "cf403_count": inventory["cf403_count"],
                "quarantine_total": inventory["quarantine_total"],
                "quarantine_in_pool_count": inventory["quarantine_in_pool_count"],
                "quarantine_outside_pool_count": inventory["quarantine_outside_pool_count"],
            }
            if latest:
                out["latest_report"] = {
                    "generated_at": latest.get("generated_at"),
                    "summary": latest.get("summary"),
                }
            return out

    def inventory(self) -> dict[str, Any]:
        settings = _settings()
        latest = self._read_latest_report()
        return build_pool_inventory(settings, latest_report=latest)

    def _read_latest_report(self) -> dict[str, Any] | None:
        path = _latest_report_path()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _write_report(self, report: dict[str, Any]) -> None:
        path = _latest_report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="webshare-cf-scan", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        settings = _settings()
        if not settings.get("enabled") and not force:
            raise RuntimeError("webshare_cf_scan is disabled")
        return self._scan_batch(settings)

    def _scan_batch(self, settings: dict[str, Any]) -> dict[str, Any]:
        pool = load_proxy_pool(_pool_path(settings))
        with self._lock:
            self._running = True
            batch, next_offset = _select_batch(
                pool,
                offset=self._pool_offset,
                batch_size=int(settings.get("batch_size") or 20),
                skip_quarantined=bool(settings.get("skip_quarantined", True)),
            )
            self._pool_offset = next_offset

        nodes: list[dict[str, Any]] = []
        workers = max(1, int(settings.get("workers") or 4))
        timeout = float(settings.get("probe_timeout_sec") or 45.0)
        if batch:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(probe_proxy_cf, proxy, timeout=timeout): proxy for proxy in batch
                }
                for future in as_completed(futures):
                    proxy = futures[future]
                    try:
                        node = future.result()
                    except Exception as exc:  # noqa: BLE001
                        node = {
                            "ok": False,
                            "cf403": True,
                            "cf_classification": "error",
                            "proxy_endpoint": proxy_endpoint_key(proxy),
                            "error": f"{type(exc).__name__}: {exc}"[:240],
                        }
                    node["proxy_hash"] = proxy_endpoint_key(proxy)
                    nodes.append(node)

        nodes.sort(key=lambda item: str(item.get("proxy_endpoint") or ""))
        cf403_nodes = [node for node in nodes if node.get("cf403")]
        quarantined: list[str] = []
        if bool(settings.get("auto_quarantine", True)):
            for node in cf403_nodes:
                endpoint = str(node.get("proxy_endpoint") or "")
                if not endpoint:
                    continue
                try:
                    mark_gpt_unavailable(
                        endpoint,
                        reason="cf403_scan",
                        former_account="",
                    )
                    quarantined.append(endpoint)
                except Exception:
                    pass

        summary = {
            "batch_size": len(batch),
            "pool_total": len(pool),
            "ok": sum(1 for node in nodes if node.get("ok")),
            "cf403": len(cf403_nodes),
            "quarantined": len(quarantined),
            "errors": sum(1 for node in nodes if node.get("error") and not node.get("ok")),
        }
        inventory = build_pool_inventory(
            settings,
            latest_report={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "nodes": nodes,
            },
        )
        summary.update(
            {
                "available_count": inventory["available_count"],
                "cf403_count": inventory["cf403_count"],
                "quarantine_total": inventory["quarantine_total"],
            }
        )
        report = {
            "schema_version": "webshare-cf-scan/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "inventory": inventory,
            "nodes": nodes,
            "quarantined_endpoints": quarantined,
        }
        self._write_report(report)
        with self._lock:
            self._status["last_scan_at"] = report["generated_at"]
            self._status["last_summary"] = summary
            self._status["pool_total"] = len(pool)
            self._status["scanned_total"] = int(self._status.get("scanned_total") or 0) + len(nodes)
            self._status["last_error"] = ""
            self._running = False
        return report

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
            if enabled and _in_active_window(settings):
                try:
                    self._scan_batch(settings)
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._status["last_error"] = str(exc)[:240]
            wait_sec = _next_interval_sec(settings)
            next_at = datetime.now(timezone.utc).timestamp() + wait_sec
            with self._lock:
                self._status["next_scan_at"] = datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat()
            if self._stop.wait(timeout=wait_sec):
                break
        with self._lock:
            self._status["running"] = False


webshare_cf_scan_service = WebshareCfScanService()
