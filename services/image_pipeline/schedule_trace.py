"""Low-overhead schedule trace (Rust cdylib + Python fallback).

All serial/conc10 acceptance paths should emit through this module when enabled.
"""
from __future__ import annotations

import contextvars
import ctypes
import json
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any

from services.config import config

_CTX: contextvars.ContextVar[ScheduleTraceRun | None] = contextvars.ContextVar(
    "schedule_trace_run",
    default=None,
)

_RUNS: dict[str, ScheduleTraceRun] = {}
_RUNS_LOCK = threading.Lock()

# Event kind ids — must match crates/image_schedule_trace/src/trace.rs
KIND_IDS: dict[str, int] = {
    "task_queued": 1,
    "task_worker_start": 2,
    "pipeline_admit": 3,
    "account_wait_start": 4,
    "account_acquired": 5,
    "ready_buffer_wait_start": 6,
    "ready_buffer_wait_end": 7,
    "ss_queue_enter": 8,
    "ss_slot_acquired": 9,
    "ss_slot_released": 10,
    "sse_stream_end": 11,
    "poll_resolve_end": 12,
    "download_start": 13,
    "download_end": 14,
    "pipeline_finish": 15,
    "ps_queue_enter": 16,
    "ps_slot_acquired": 17,
    "ps_slot_released": 18,
    "task_terminal": 19,
    "global_concurrency_wait_start": 20,
    "global_concurrency_wait_end": 21,
}


def _pipeline_settings() -> dict[str, Any]:
    try:
        return config.get_image_pipeline_settings()
    except Exception:
        return {}


def enabled() -> bool:
    settings = _pipeline_settings()
    if "schedule_trace_enabled" in settings:
        return bool(settings.get("schedule_trace_enabled"))
    return True


def pack_pool_aux(*, active: int, queued: int, slot: int | None = None) -> int:
    if slot is not None:
        return ((int(active) & 0xFFFF) << 16) | (int(slot) & 0xFFFF)
    return ((int(active) & 0xFFFF) << 16) | (int(queued) & 0xFFFF)


class _RustLib:
    def __init__(self) -> None:
        self._lib = None
        self._load()

    def _lib_path(self) -> Path | None:
        root = Path(__file__).resolve().parents[2]
        name = "image_schedule_trace.dll" if platform.system() == "Windows" else "libimage_schedule_trace.so"
        for candidate in (
            root / "crates" / "image_schedule_trace" / "target" / "release" / name,
            root / "native" / name,
        ):
            if candidate.is_file():
                return candidate
        return None

    def _load(self) -> None:
        path = self._lib_path()
        if path is None:
            return
        try:
            lib = ctypes.CDLL(str(path))
            lib.ist_trace_begin.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            lib.ist_trace_begin.restype = ctypes.c_uint64
            lib.ist_trace_emit.argtypes = [ctypes.c_uint64, ctypes.c_uint8, ctypes.c_uint32]
            lib.ist_trace_emit.restype = None
            lib.ist_trace_set_account.argtypes = [ctypes.c_uint64, ctypes.c_char_p]
            lib.ist_trace_set_account.restype = None
            lib.ist_trace_finish.argtypes = [ctypes.c_uint64]
            lib.ist_trace_finish.restype = ctypes.c_void_p
            lib.ist_trace_free_string.argtypes = [ctypes.c_void_p]
            lib.ist_trace_free_string.restype = None
            self._lib = lib
        except OSError:
            self._lib = None

    @property
    def available(self) -> bool:
        return self._lib is not None


_RUST = _RustLib()


class ScheduleTraceRun:
    __slots__ = ("task_key", "account_email", "engine", "_handle", "_py_events", "_origin")

    def __init__(self, task_key: str, account_email: str = "") -> None:
        self.task_key = str(task_key or "").strip()
        self.account_email = str(account_email or "").strip()
        self.engine = "rust" if _RUST.available else "python"
        self._origin = time.monotonic()
        self._py_events: list[tuple[int, int, int]] = []
        self._handle = 0
        if _RUST.available:
            h = _RUST._lib.ist_trace_begin(
                self.task_key.encode("utf-8"),
                self.account_email.encode("utf-8"),
            )
            self._handle = int(h)

    def set_account_email(self, email: str) -> None:
        email = str(email or "").strip()
        if not email:
            return
        self.account_email = email
        if self._handle and _RUST.available:
            _RUST._lib.ist_trace_set_account(self._handle, email.encode("utf-8"))

    def emit(self, kind: str, aux: int = 0) -> None:
        if not enabled():
            return
        kid = KIND_IDS.get(kind)
        if kid is None:
            return
        if self._handle and _RUST.available:
            _RUST._lib.ist_trace_emit(self._handle, ctypes.c_uint8(kid), ctypes.c_uint32(int(aux)))
            return
        mono_ns = int((time.monotonic() - self._origin) * 1_000_000_000)
        self._py_events.append((kid, mono_ns, int(aux)))

    def finish(self) -> dict[str, Any]:
        if self._handle and _RUST.available:
            ptr = _RUST._lib.ist_trace_finish(self._handle)
            self._handle = 0
            if not ptr:
                return {"engine": "rust", "task_key": self.task_key, "error": "finish_failed"}
            try:
                raw = ctypes.cast(ptr, ctypes.c_char_p).value
                text = raw.decode("utf-8") if raw else "{}"
            finally:
                _RUST._lib.ist_trace_free_string(ptr)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"engine": "rust", "task_key": self.task_key, "raw": text}
        return _finish_python(self)

    def to_dict(self) -> dict[str, Any]:
        return self.finish()


def _finish_python(run: ScheduleTraceRun) -> dict[str, Any]:
    from services.image_pipeline.schedule_trace_model import build_model_from_events

    events = [(k, ns, aux) for k, ns, aux in run._py_events]
    model = build_model_from_events(events)
    named_events = []
    inv = {v: k for k, v in KIND_IDS.items()}
    for kid, mono_ns, aux in run._py_events:
        active = (aux >> 16) & 0xFFFF
        queued = aux & 0xFFFF
        row: dict[str, Any] = {
            "kind": inv.get(kid, str(kid)),
            "mono_ns": mono_ns,
            "aux": aux,
        }
        if kid in (8, 16):
            row["pool_active"] = active
            row["pool_queued"] = queued
        if kid in (9, 10, 17, 18):
            row["slot"] = aux & 0xFFFF
        named_events.append(row)
    return {
        "engine": "python",
        "version": "0.1.0",
        "task_key": run.task_key,
        "account_email": run.account_email,
        "event_count": len(named_events),
        "events": named_events,
        "phases_ms": model["phases_ms"],
        "explanations": model["explanations"],
        "checkpoints": model["checkpoints"],
    }


def begin(task_key: str, account_email: str = "") -> ScheduleTraceRun:
    run = ScheduleTraceRun(task_key, account_email)
    with _RUNS_LOCK:
        _RUNS[task_key] = run
    run.emit("task_queued")
    return run


def get(task_key: str) -> ScheduleTraceRun | None:
    with _RUNS_LOCK:
        return _RUNS.get(task_key)


def pop(task_key: str) -> ScheduleTraceRun | None:
    with _RUNS_LOCK:
        return _RUNS.pop(task_key, None)


def bind(run: ScheduleTraceRun | None) -> contextvars.Token:
    return _CTX.set(run)


def unbind(token: contextvars.Token) -> None:
    _CTX.reset(token)


def active() -> ScheduleTraceRun | None:
    return _CTX.get()


def emit(kind: str, aux: int = 0) -> None:
    run = active()
    if run is not None:
        run.emit(kind, aux)


def engine_info() -> dict[str, str]:
    path = _RUST._lib_path()
    return {
        "engine": "rust" if _RUST.available else "python",
        "lib_path": str(path) if path else "",
        "enabled": str(enabled()),
    }
