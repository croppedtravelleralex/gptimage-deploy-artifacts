"""Regression tests for slot ledger watchdog force-release + backend reporting.

A0-1: ``_PySlotLedger.watchdog_tick(force_release_expired=True)`` used to call the
public ``release_account`` / ``release_ss`` while already holding the
non-reentrant ``_lock``, which self-deadlocks permanently and wedges every later
``try_acquire_*`` / ``release_*`` / ``stats`` call. Every watchdog invocation
below runs on a worker thread with a bounded join so a regression fails fast
instead of hanging the whole suite.

A0-2: a missing/failed native ``.so`` load must be logged loudly and the active
backend must be visible in ``snapshot()`` / ``stats()`` so ``/health`` shows it.
"""

import logging
import threading
from unittest.mock import MagicMock, PropertyMock, patch

from services.image_pipeline.slot_ledger import (
    SlotLedgerFacade,
    _PySlotLedger,
    _RustSlotLedger,
    _token_hash,
)

LOGGER_NAME = "services.image_pipeline.slot_ledger"
WATCHDOG_JOIN_TIMEOUT = 5.0


def _run_guarded(fn, *args, **kwargs):
    """Run ``fn`` on a worker thread and fail fast rather than hang on deadlock."""
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=WATCHDOG_JOIN_TIMEOUT)
    assert not thread.is_alive(), (
        f"{getattr(fn, '__name__', fn)!r} did not return within "
        f"{WATCHDOG_JOIN_TIMEOUT}s - non-reentrant lock self-deadlock (A0-1)"
    )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _python_backed_facade() -> SlotLedgerFacade:
    """Facade pinned to the Python mirror regardless of local build artifacts."""
    with patch.object(_RustSlotLedger, "_lib_path", return_value=None):
        return SlotLedgerFacade()


def _events(caplog, event: str) -> list[dict]:
    """Structured log payloads matching ``event``.

    The module logs dicts, so assert against ``record.msg`` rather than
    ``caplog.text`` - a ``repr``'d dict double-escapes Windows path separators.
    """
    return [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == event
    ]


def _one_event(caplog, event: str) -> dict:
    matched = _events(caplog, event)
    assert len(matched) == 1, (
        f"expected exactly one {event!r} log record, got {len(matched)}; "
        f"all records: {[r.msg for r in caplog.records]}"
    )
    return matched[0]


# --------------------------------------------------------------------------
# A0-1 - watchdog force-release must complete and actually release
# --------------------------------------------------------------------------


def test_watchdog_force_release_expired_account_completes_and_releases():
    ledger = _PySlotLedger()
    token = _token_hash("tok-account")
    assert ledger.try_acquire_account("holder-1", token, deadline_secs=0.0) is True
    assert ledger.stats()["account_held"] == 1

    report = _run_guarded(ledger.watchdog_tick, force_release_expired=True)

    assert report["account_expired_forced"] == 1
    assert report["forced_releases"] == 1
    assert report["account_held"] == 0
    assert report["total_account_inflight"] == 0
    # lock was handed back cleanly - the ledger is not wedged
    assert _run_guarded(ledger.stats)["account_held"] == 0
    assert ledger.try_acquire_account("holder-1", token) is True


def test_watchdog_force_release_expired_ss_completes_and_releases():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_ss("ss-holder", deadline_secs=0.0) is True
    assert ledger.stats()["ss_held"] == 1

    report = _run_guarded(ledger.watchdog_tick, force_release_expired=True)

    assert report["ss_expired_forced"] == 1
    assert report["forced_releases"] == 1
    assert report["ss_held"] == 0
    assert _run_guarded(ledger.stats)["ss_held"] == 0
    assert ledger.try_acquire_ss("ss-holder") is True


def test_watchdog_releases_expired_account_and_ss_in_one_tick():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_account("a1", _token_hash("t1"), deadline_secs=0.0) is True
    assert ledger.try_acquire_account("a2", _token_hash("t2"), deadline_secs=0.0) is True
    assert ledger.try_acquire_ss("s1", deadline_secs=0.0) is True

    report = _run_guarded(ledger.watchdog_tick, force_release_expired=True)

    assert report["account_expired_forced"] == 2
    assert report["ss_expired_forced"] == 1
    assert report["forced_releases"] == 3
    assert report["account_held"] == 0
    assert report["ss_held"] == 0
    assert report["total_account_inflight"] == 0


def test_watchdog_second_tick_is_a_noop_after_forced_release():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_account("a1", _token_hash("t1"), deadline_secs=0.0) is True
    assert ledger.try_acquire_ss("s1", deadline_secs=0.0) is True

    first = _run_guarded(ledger.watchdog_tick, force_release_expired=True)
    second = _run_guarded(ledger.watchdog_tick, force_release_expired=True)

    assert first["forced_releases"] == 2
    assert second["account_expired_forced"] == 0
    assert second["ss_expired_forced"] == 0
    # cumulative counter must not double-count the same leases
    assert second["forced_releases"] == 2


# --------------------------------------------------------------------------
# Idempotency / count integrity
# --------------------------------------------------------------------------


def test_release_account_is_idempotent_and_keeps_counts_clean():
    ledger = _PySlotLedger()
    token = _token_hash("tok-idem")
    assert ledger.try_acquire_account("h", token) is True
    assert ledger.release_account("h") is True
    assert not ledger.release_account("h")
    assert not ledger.release_account("never-acquired")
    assert ledger.stats() == {
        "account_held": 0,
        "ss_held": 0,
        "total_account_inflight": 0,
        "forced_releases": 0,
    }
    assert ledger._token_counts == {}


def test_release_ss_is_idempotent():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_ss("s") is True
    assert ledger.release_ss("s") is True
    assert not ledger.release_ss("s")
    assert not ledger.release_ss("never-acquired")
    assert ledger.stats()["ss_held"] == 0
    assert ledger._ss_deadline == {}


def test_shared_token_inflight_decrements_once_per_holder():
    ledger = _PySlotLedger()
    token = _token_hash("shared")
    assert ledger.try_acquire_account("h1", token) is True
    assert ledger.try_acquire_account("h2", token) is True
    assert ledger.stats()["total_account_inflight"] == 2

    assert ledger.release_account("h1") is True
    assert ledger.stats()["total_account_inflight"] == 1
    assert not ledger.release_account("h1")
    assert ledger.stats()["total_account_inflight"] == 1

    assert ledger.release_account("h2") is True
    assert ledger.stats()["total_account_inflight"] == 0
    assert ledger._token_counts == {}


def test_release_after_forced_release_returns_falsy():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_account("h", _token_hash("t"), deadline_secs=0.0) is True
    assert ledger.try_acquire_ss("s", deadline_secs=0.0) is True
    _run_guarded(ledger.watchdog_tick, force_release_expired=True)

    assert not ledger.release_account("h")
    assert not ledger.release_ss("s")
    assert ledger.stats()["total_account_inflight"] == 0


# --------------------------------------------------------------------------
# Non-expired leases must survive the watchdog
# --------------------------------------------------------------------------


def test_watchdog_does_not_release_unexpired_leases():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_account("live", _token_hash("t-live"), deadline_secs=300.0) is True
    # a lease registered without a deadline must never be force-released
    assert ledger.try_acquire_account("eternal", _token_hash("t-eternal")) is True
    assert ledger.try_acquire_ss("live-ss", deadline_secs=300.0) is True

    report = _run_guarded(ledger.watchdog_tick, force_release_expired=True)

    assert report["account_expired_forced"] == 0
    assert report["ss_expired_forced"] == 0
    assert report["forced_releases"] == 0
    assert report["account_held"] == 2
    assert report["ss_held"] == 1
    assert report["total_account_inflight"] == 2


def test_watchdog_without_force_keeps_expired_leases():
    ledger = _PySlotLedger()
    assert ledger.try_acquire_account("h", _token_hash("t"), deadline_secs=0.0) is True
    assert ledger.try_acquire_ss("s", deadline_secs=0.0) is True

    report = _run_guarded(ledger.watchdog_tick, force_release_expired=False)

    assert report["account_expired_forced"] == 0
    assert report["ss_expired_forced"] == 0
    assert report["account_held"] == 1
    assert report["ss_held"] == 1


# --------------------------------------------------------------------------
# A0-2 - backend identity is reported, degradation is logged
# --------------------------------------------------------------------------


def test_snapshot_reports_python_backend_when_native_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        facade = _python_backed_facade()

    snap = facade.snapshot()
    assert snap["backend"] == "python"
    assert snap["rust_available"] is False
    assert snap["rust_lib_path"] is None
    assert snap["rust_load_error"] == "native library not found"
    assert "account_held" in snap

    payload = _one_event(caplog, "slot_ledger_native_missing")
    assert payload["fallback"] == "python"
    assert payload["error"] == "native library not found"
    # the warning must name every path it looked at
    assert payload["searched"]
    assert all("image_schedule_core" in candidate for candidate in payload["searched"])


def test_snapshot_reports_rust_backend_when_native_available():
    with patch.object(_RustSlotLedger, "available", new_callable=PropertyMock, return_value=True):
        facade = SlotLedgerFacade()
        snap = facade.snapshot()

    assert snap["backend"] == "rust"
    assert snap["rust_available"] is True


def test_facade_stats_exposes_backend():
    facade = _python_backed_facade()
    stats = facade.stats()
    assert stats["backend"] == "python"
    assert stats["rust_available"] is False


def test_watchdog_tick_reports_backend_and_logs_degradation_once(caplog):
    facade = _python_backed_facade()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        first = _run_guarded(facade.watchdog_tick, force_release_expired=True)
        second = _run_guarded(facade.watchdog_tick, force_release_expired=True)

    assert first["backend"] == 0
    assert first["backend_name"] == "python"
    assert second["backend_name"] == "python"
    payload = _one_event(caplog, "slot_ledger_backend_degraded")
    assert payload["backend"] == "python"
    assert payload["rust_load_error"] == "native library not found"


def test_native_load_failure_logs_path_and_exception(tmp_path, caplog):
    fake_lib = tmp_path / "libimage_schedule_core.so"
    fake_lib.write_bytes(b"not a shared object")

    with patch.object(_RustSlotLedger, "_lib_path", return_value=fake_lib):
        with patch(
            "services.image_pipeline.slot_ledger.ctypes.CDLL",
            side_effect=OSError("invalid ELF header"),
        ):
            with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
                rust = _RustSlotLedger()

    assert rust.available is False
    assert rust.lib_path == str(fake_lib)
    assert "invalid ELF header" in (rust.load_error or "")

    payload = _one_event(caplog, "slot_ledger_native_load_failed")
    assert payload["path"] == str(fake_lib)
    assert "invalid ELF header" in payload["error"]
    assert "OSError" in payload["error"]
    assert payload["fallback"] == "python"
    # exc_info must be attached so the traceback reaches the log
    assert caplog.records[-1].exc_info is not None


def test_native_null_handle_logs_and_falls_back(tmp_path, caplog):
    fake_lib = tmp_path / "libimage_schedule_core.so"
    fake_lib.write_bytes(b"stub")
    lib = MagicMock()
    lib.isc_slot_ledger_create.return_value = 0

    with patch.object(_RustSlotLedger, "_lib_path", return_value=fake_lib):
        with patch("services.image_pipeline.slot_ledger.ctypes.CDLL", return_value=lib):
            with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
                rust = _RustSlotLedger()

    assert rust.available is False
    assert "null handle" in (rust.load_error or "")

    payload = _one_event(caplog, "slot_ledger_native_create_failed")
    assert payload["path"] == str(fake_lib)
    assert payload["fallback"] == "python"


def test_facade_delegates_to_python_mirror_when_degraded():
    facade = _python_backed_facade()
    assert facade.try_acquire_account("h", "access-token-1", deadline_secs=0.0) is True
    assert facade.try_acquire_account("h", "access-token-1") is False  # holder already held
    assert facade.snapshot()["account_held"] == 1

    report = _run_guarded(facade.watchdog_tick, force_release_expired=True)
    assert report["account_expired_forced"] == 1
    assert facade.snapshot()["account_held"] == 0
    assert facade.release_account("h") is False
