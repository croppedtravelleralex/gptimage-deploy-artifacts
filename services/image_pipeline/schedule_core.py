"""Rust schedule core (dispatch gate + sediment parser) with Python fallback."""
from __future__ import annotations

import ctypes
import json
import platform
import re
from pathlib import Path

_SEDIMENT_RE = re.compile(r"sediment://([^\s\"')\],]+)")


class _RustCore:
    def __init__(self) -> None:
        self._lib = None
        self._load()

    def _lib_path(self) -> Path | None:
        root = Path(__file__).resolve().parents[2]
        name = (
            "image_schedule_core.dll"
            if platform.system() == "Windows"
            else "libimage_schedule_core.so"
        )
        for candidate in (
            root / "crates" / "image_schedule_core" / "target" / "release" / name,
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
            lib.isc_dispatch_should_wait.argtypes = [
                ctypes.c_uint64,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            lib.isc_dispatch_should_wait.restype = ctypes.c_uint8
            lib.isc_sediment_parser_create.restype = ctypes.c_uint64
            lib.isc_sediment_parser_destroy.argtypes = [ctypes.c_uint64]
            lib.isc_sediment_parser_feed.argtypes = [
                ctypes.c_uint64,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            lib.isc_sediment_parser_feed.restype = ctypes.c_uint8
            lib.isc_sediment_parser_ids_json.argtypes = [ctypes.c_uint64]
            lib.isc_sediment_parser_ids_json.restype = ctypes.c_void_p
            lib.isc_free_string.argtypes = [ctypes.c_void_p]
            self._lib = lib
        except OSError:
            self._lib = None

    @property
    def available(self) -> bool:
        return self._lib is not None


_RUST = _RustCore()


def dispatch_should_apply_interval(
    *,
    interval_ms: int,
    inflight: int,
    cap: int,
    queued: int,
) -> bool:
    if interval_ms <= 0:
        return False
    if inflight < cap and queued <= max(0, cap - inflight):
        return False
    if _RUST.available:
        return bool(
            _RUST._lib.isc_dispatch_should_wait(
                0,
                ctypes.c_uint32(interval_ms),
                ctypes.c_uint32(inflight),
                ctypes.c_uint32(cap),
                ctypes.c_uint32(queued),
            )
        )
    return inflight >= cap or queued > max(0, cap - inflight)


class SedimentParser:
    def __init__(self) -> None:
        self._handle = 0
        self._py_ids: set[str] = set()
        if _RUST.available:
            self._handle = int(_RUST._lib.isc_sediment_parser_create())

    def feed(self, chunk: str) -> bool:
        if not chunk:
            return False
        if self._handle and _RUST.available:
            raw = chunk.encode("utf-8")
            return bool(
                _RUST._lib.isc_sediment_parser_feed(
                    self._handle,
                    raw,
                    ctypes.c_uint32(len(raw)),
                )
            )
        found = False
        for match in _SEDIMENT_RE.finditer(chunk):
            sid = match.group(1).strip()
            if sid and sid not in self._py_ids:
                self._py_ids.add(sid)
                found = True
        return found

    def ids(self) -> list[str]:
        if self._handle and _RUST.available:
            ptr = _RUST._lib.isc_sediment_parser_ids_json(self._handle)
            if not ptr:
                return []
            try:
                raw = ctypes.cast(ptr, ctypes.c_char_p).value
                text = raw.decode("utf-8") if raw else "[]"
            finally:
                _RUST._lib.isc_free_string(ptr)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return []
            return [str(x) for x in data if x]
        return sorted(self._py_ids)

    def close(self) -> None:
        if self._handle and _RUST.available:
            _RUST._lib.isc_sediment_parser_destroy(self._handle)
            self._handle = 0

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def engine_info() -> dict[str, str]:
    path = _RUST._lib_path()
    return {
        "engine": "rust" if _RUST.available else "python",
        "lib_path": str(path) if path else "",
    }
