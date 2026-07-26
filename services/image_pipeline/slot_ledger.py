"""Rust SlotLedger FFI with Python fallback for account/sS lease FSM."""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import platform
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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

    def _release_account_locked(self, holder_key: str) -> bool:
        """Release an account lease. Caller MUST already hold ``self._lock``."""
        lease = self._account.pop(holder_key, None)
        if lease is None:
            return False
        count = int(self._token_counts.get(lease.token_hash, 0)) - 1
        if count <= 0:
            self._token_counts.pop(lease.token_hash, None)
        else:
            self._token_counts[lease.token_hash] = count
        return True

    def release_account(self, holder_key: str) -> bool:
        with self._lock:
            return self._release_account_locked(holder_key)

    def try_acquire_ss(self, holder_key: str, *, deadline_secs: float | None = None) -> bool:
        with self._lock:
            if not holder_key or holder_key in self._ss:
                return False
            now = time.monotonic()
            self._ss[holder_key] = now
            if deadline_secs is not None:
                self._ss_deadline[holder_key] = now + max(0.0, deadline_secs)
            return True

    def _release_ss_locked(self, holder_key: str) -> bool:
        """Release an sS lease. Caller MUST already hold ``self._lock``."""
        if holder_key not in self._ss:
            return False
        self._ss.pop(holder_key, None)
        self._ss_deadline.pop(holder_key, None)
        return True

    def release_ss(self, holder_key: str) -> bool:
        with self._lock:
            return self._release_ss_locked(holder_key)

    def watchdog_tick(self, *, force_release_expired: bool) -> dict[str, int]:
        now = time.monotonic()
        account_forced = 0
        ss_forced = 0
        with self._lock:
            if force_release_expired:
                # NOTE: self._lock is a plain (non-reentrant) Lock, so these MUST
                # call the *_locked internals -- calling the public release_*()
                # here would re-acquire the held lock and self-deadlock forever.
                for key, lease in list(self._account.items()):
                    if lease.deadline_mono is not None and now >= lease.deadline_mono:
                        if self._release_account_locked(key):
                            account_forced += 1
                            self._forced_releases += 1
                for key, deadline in list(self._ss_deadline.items()):
                    if now >= deadline:
                        if self._release_ss_locked(key):
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
        self._lib_path_used: str | None = None
        self._load_error: str | None = None
        self._load()

    def _candidate_paths(self) -> tuple[Path, ...]:
        root = Path(__file__).resolve().parents[2]
        name = (
            "image_schedule_core.dll"
            if platform.system() == "Windows"
            else "libimage_schedule_core.so"
        )
        return (
            root / "crates" / "image_schedule_core" / "target" / "release" / name,
            root / "native" / name,
        )

    def _lib_path(self) -> Path | None:
        for candidate in self._candidate_paths():
            if candidate.is_file():
                return candidate
        return None

    def _load(self) -> None:
        path = self._lib_path()
        if path is None:
            self._load_error = "native library not found"
            logger.warning(
                {
                    "event": "slot_ledger_native_missing",
                    "searched": [str(p) for p in self._candidate_paths()],
                    "error": self._load_error,
                    "fallback": "python",
                    "impact": "slot ledger runs the in-process Python mirror",
                }
            )
            return
        self._lib_path_used = str(path)
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
            else:
                self._load_error = "isc_slot_ledger_create returned a null handle"
                logger.error(
                    {
                        "event": "slot_ledger_native_create_failed",
                        "path": str(path),
                        "error": self._load_error,
                        "fallback": "python",
                        "impact": "slot ledger runs the in-process Python mirror",
                    }
                )
        except (OSError, AttributeError) as exc:
            self._lib = None
            self._handle = 0
            self._load_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                {
                    "event": "slot_ledger_native_load_failed",
                    "path": str(path),
                    "error": self._load_error,
                    "fallback": "python",
                    "impact": "slot ledger runs the in-process Python mirror",
                },
                exc_info=True,
            )

    @property
    def available(self) -> bool:
        return self._lib is not None and self._handle != 0

    @property
    def lib_path(self) -> str | None:
        """Path of the native library that load was attempted against, if any."""
        return self._lib_path_used

    @property
    def load_error(self) -> str | None:
        """Why the native backend is unavailable, or None when it loaded."""
        return self._load_error

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
        self._degraded_logged = False

    @property
    def backend(self) -> str:
        return "rust" if self._use_rust else "python"

    def _log_degraded_once(self) -> None:
        """Re-announce a silent fallback at tick time.

        ``_RustSlotLedger._load`` logs at import time, which on some entrypoints
        runs before logging is configured and would be swallowed. Emitting once
        more from the watchdog guarantees the degradation reaches the log.
        """
        if self._use_rust or self._degraded_logged:
            return
        self._degraded_logged = True
        logger.warning(
            {
                "event": "slot_ledger_backend_degraded",
                "backend": self.backend,
                "rust_lib_path": self._rust.lib_path,
                "rust_load_error": self._rust.load_error,
            }
        )

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

    def watchdog_tick(self, *, force_release_expired: bool = False) -> dict[str, Any]:
        self._log_degraded_once()
        if self._use_rust:
            report = self._rust.watchdog_tick(force_release_expired=force_release_expired)
        else:
            report = self._py.watchdog_tick(force_release_expired=force_release_expired)
        out: dict[str, Any] = dict(report)
        # `backend` stays the legacy 0/1 int; `backend_name` is the readable form.
        out["backend"] = 1 if self._use_rust else 0
        out["backend_name"] = self.backend
        return out

    def stats(self) -> dict[str, Any]:
        """Backend-tagged counters.

        Both concrete ledgers expose ``stats()``; the facade did not, so calling
        ``slot_ledger.stats()`` used to raise AttributeError. It is the same
        payload as :meth:`snapshot`, backend identity included.
        """
        return dict(self.snapshot())

    def snapshot(self) -> dict[str, object]:
        stats = self._rust.stats() if self._use_rust else self._py.stats()
        return {
            "backend": self.backend,
            "rust_available": self._rust.available,
            "rust_lib_path": self._rust.lib_path,
            "rust_load_error": self._rust.load_error,
            **stats,
        }


slot_ledger = SlotLedgerFacade()
