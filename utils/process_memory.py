"""Lightweight process memory introspection (Linux /proc)."""
from __future__ import annotations

from pathlib import Path


def process_memory_snapshot() -> dict[str, int | float]:
    status_path = Path("/proc/self/status")
    if not status_path.is_file():
        return {}
    fields: dict[str, int] = {}
    for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in {"VmRSS", "VmData", "VmHWM", "Threads"}:
            continue
        parts = value.strip().split()
        if not parts:
            continue
        try:
            fields[key] = int(parts[0])
        except ValueError:
            continue
    rss_kb = int(fields.get("VmRSS") or 0)
    return {
        "rss_mb": round(rss_kb / 1024.0, 1),
        "data_mb": round(int(fields.get("VmData") or 0) / 1024.0, 1),
        "hwm_mb": round(int(fields.get("VmHWM") or 0) / 1024.0, 1),
        "threads": int(fields.get("Threads") or 0),
    }
