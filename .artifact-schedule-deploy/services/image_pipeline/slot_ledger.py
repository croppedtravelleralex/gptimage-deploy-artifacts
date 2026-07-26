"""Rust SlotLedger FFI with Python fallback for account/sS lease FSM."""
from __future__ import annotations

import ctypes
import hashlib
import json
import platform
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _token_hash(access_token: str) -> int:
    digest = hashlib.blake2b(access_token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little", signed=False)


@dataclass
class _PyLease:
    token_hash: int
    acquired_at: float
    deadline_mono: float | None


class _PySlotLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._account: dict[str, _PyLease] = {}
        self._ss: dict[str, float] = {}
        self._ss_deadline: dict[str, float] = {}
        self._token_counts: dict[int, int] = {}
        self._forced_releases = 0

    def try_acquire_account(
        self,
        holder_key: str,
        token_hash: int,
        *,
        deadline_secs: float | None = None,
    ) -> bool:
        with self._lock:
            if not holder_key or holder_key in self._account:
                return False
            deadline_mono = (
                time.monotonic() + max(0.0, deadline_secs)
                if deadline_secs is not None
                else None
            )
            self._account[holder_key] = _PyLease(
                token_hash=token_hash,
                acquired_at=time.monotonic(),
                deadline_mono=deadline_mono,
            )
            self._token_counts[token_hash] = int(self._token_counts.get(token_hash, 0)) + 1
            return True

    def release_account(self, holder_key: str) -> bool:
        with self._lock:
            lease = self._account.pop(holder_key, None)
            if lease is None:
                return False
            count = int(self._token_counts.get(lease.token_hash, 0)) - 1
            if count <= 0:
                self._token_counts.pop(lease.token_hash, None)
            else:
                self._token_counts[lease.token_hash] = count
            return True

    def try_acquire_ss(self, holder_key: str, *, deadline_secs: float | None = None) -> bool:
        with self._lock:
            if not holder_key or holder_key in self._ss:
                return False
            now = time.monotonic()
            self._ss[holder_key] = now
            if deadline_secs is not None:
                self._ss_deadline[holder_key] = now + max(0.0, deadline_secs)
            return True

    def release_ss(self, holder_key: str) -> bool:
        with self._lock:
            if holder_key not in self._ss:
                return False
            self._ss.pop(holder_key, None)
            self._ss_deadline.pop(holder_key, None)
            return True

    def watchdog_tick(self, *, force_release_expired: bool) -> dict[str, int]:
        now = time.monotonic()
        account_forced = 0
        ss_forced = 0
        with self._lock:
            if force_release_expired:
                for key, lease in list(self._account.items()):
                    if lease.deadline_mono is not None and now >= lease.deadline_mono:
                        if self.release_account(key):
                            account_forced += 1
                            self._forced_releases += 1
                for key, deadline in list(self._ss_deadline.items()):
                    if now >= deadline:
                        if self.release_ss(key):
                            ss_forced += 1
                            self._forced_releases += 1
            return {
                "account_held": len(self._account),
                "ss_held": len(self._ss),
                "total_account_inflight": sum(self._token_counts.values()),
                "account_expired_forced": account_forced,
                "ss_expired_forced": ss_forced,
                "forced_releases": self._forced_releases,
            }

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "account_held": len(self._account),
                "ss_held": len(self._ss),
                "total_account_inflight": sum(self._token_counts.values()),
                "forced_releases": self._forced_releases,
            }


class _RustSlotLedger:
    def __init__(self) -> None:
        self._lib = None
        self._handle = 0
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
            lib.isc_slot_ledger_create.restype = ctypes.c_uint64
            lib.isc_slot_ledger_destroy.argtypes = [ctypes.c_uint64]
            lib.isc_slot_ledger_try_acquire_account.argtypes = [
                ctypes.c_uint64,
                ctypes.c_char_p,
                ctypes.c_uint64,
                ctypes.c_uint64,
            ]
            lib.isc_slot_ledger_try_acquire_account.restype = ctypes.c_uint8
            lib.isc_slot_ledger_release_account.argtypes = [ctypes.c_uint64, ctypes.c_char_p]
            lib.isc_slot_ledger_release_account.restype = ctypes.c_uint8
            lib.isc_slot_ledger_try_acquire_ss.argtypes = [
                ctypes.c_uint64,
                ctypes.c_char_p,
                ctypes.c_uint64,
            ]
            lib.isc_slot_ledger_try_acquire_ss.restype = ctypes.c_uint8
            lib.isc_slot_ledger_release_ss.argtypes = [ctypes.c_uint64, ctypes.c_char_p]
            lib.isc_slot_ledger_release_ss.restype = ctypes.c_uint8
            lib.isc_slot_ledger_watchdog_tick.argtypes = [ctypes.c_uint64, ctypes.c_uint8]
            lib.isc_slot_ledger_watchdog_tick.restype = ctypes.c_void_p
            lib.isc_slot_ledger_stats_json.argtypes = [ctypes.c_uint64]
            lib.isc_slot_ledger_stats_json.restype = ctypes.c_void_p
            lib.isc_free_string.argtypes = [ctypes.c_void_p]
            handle = int(lib.isc_slot_ledger_create())
            if handle:
                self._lib = lib
                self._handle = handle
        except (OSError, AttributeError):
            self._lib = None
            self._handle = 0

    @property
    def available(self) -> bool:
        return self._lib is not None and self._handle != 0

    def _read_json_ptr(self, ptr: ctypes.c_void_p) -> dict[str, Any]:
        if not ptr or not self._lib:
            return {}
        try:
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            text = (raw or b"").decode("utf-8", errors="replace")
            self._lib.isc_free_string(ptr)
            return json.loads(text) if text else {}
        except Exception:
            return {}

    def try_acquire_account(
        self,
        holder_key: str,
        token_hash: int,
        *,
        deadline_secs: float | None = None,
    ) -> bool:
        if not self._lib or not self._handle:
            return False
        deadline_ns = int(max(0.0, deadline_secs or 0.0) * 1_000_000_000)
        return bool(
            self._lib.isc_slot_ledger_try_acquire_account(
                self._handle,
                holder_key.encode("utf-8"),
                int(token_hash),
                ctypes.c_uint64(deadline_ns),
            )
        )

    def release_account(self, holder_key: str) -> bool:
        if not self._lib or not self._handle:
            return False
        return bool(
            self._lib.isc_slot_ledger_release_account(
                self._handle,
                holder_key.encode("utf-8"),
            )
        )

    def try_acquire_ss(self, holder_key: str, *, deadline_secs: float | None = None) -> bool:
        if not self._lib or not self._handle:
            return False
        deadline_ns = int(max(0.0, deadline_secs or 0.0) * 1_000_000_000)
        return bool(
            self._lib.isc_slot_ledger_try_acquire_ss(
                self._handle,
                holder_key.encode("utf-8"),
                ctypes.c_uint64(deadline_ns),
            )
        )

    def release_ss(self, holder_key: str) -> bool:
        if not self._lib or not self._handle:
            return False
        return bool(
            self._lib.isc_slot_ledger_release_ss(
                self._handle,
                holder_key.encode("utf-8"),
            )
        )

    def watchdog_tick(self, *, force_release_expired: bool) -> dict[str, int]:
        if not self._lib or not self._handle:
            return {}
        ptr = self._lib.isc_slot_ledger_watchdog_tick(
            self._handle,
            1 if force_release_expired else 0,
        )
        data = self._read_json_ptr(ptr)
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}

    def stats(self) -> dict[str, int]:
        if not self._lib or not self._handle:
            return {}
        ptr = self._lib.isc_slot_ledger_stats_json(self._handle)
        data = self._read_json_ptr(ptr)
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}


class SlotLedgerFacade:
    """Unified API: Rust when native .so present, else Python mirror."""

    def __init__(self) -> None:
        self._rust = _RustSlotLedger()
        self._py = _PySlotLedger()
        self._use_rust = self._rust.available

    @property
    def backend(self) -> str:
        return "rust" if self._use_rust else "python"

    def try_acquire_account(
        self,
        holder_key: str,
        access_token: str,
        *,
        deadline_secs: float | None = None,
    ) -> bool:
        token_hash = _token_hash(access_token)
        if self._use_rust:
            ok = self._rust.try_acquire_account(holder_key, token_hash, deadline_secs=deadline_secs)
        else:
            ok = self._py.try_acquire_account(holder_key, token_hash, deadline_secs=deadline_secs)
        return ok

    def release_account(self, holder_key: str) -> bool:
        if self._use_rust:
            return self._rust.release_account(holder_key)
        return self._py.release_account(holder_key)

    def try_acquire_ss(self, holder_key: str, *, deadline_secs: float | None = None) -> bool:
        if self._use_rust:
            return self._rust.try_acquire_ss(holder_key, deadline_secs=deadline_secs)
        return self._py.try_acquire_ss(holder_key, deadline_secs=deadline_secs)

    def release_ss(self, holder_key: str) -> bool:
        if self._use_rust:
            return self._rust.release_ss(holder_key)
        return self._py.release_ss(holder_key)

    def watchdog_tick(self, *, force_release_expired: bool = False) -> dict[str, int]:
        if self._use_rust:
            report = self._rust.watchdog_tick(force_release_expired=force_release_expired)
        else:
            report = self._py.watchdog_tick(force_release_expired=force_release_expired)
        report["backend"] = 1 if self._use_rust else 0
        return report

    def snapshot(self) -> dict[str, object]:
        stats = self._rust.stats() if self._use_rust else self._py.stats()
        return {"backend": self.backend, **stats}


slot_ledger = SlotLedgerFacade()
