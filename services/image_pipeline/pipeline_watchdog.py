"""Pipeline watchdog: slot ledger reconcile + inflight drift detection.

The watchdog holds two independent corrective powers with very different blast
radius (audit 28 §A4):

* ``force_release_expired`` -> :meth:`SlotLedgerFacade.watchdog_tick`. Drops
  ledger leases whose deadline has passed. Cheap and close to cosmetic: the
  ledger is a shadow bookkeeping mirror that imports neither ``account_service``
  nor the pipeline pools, so releasing a lease neither decrements
  ``_image_inflight`` nor hands a ``SlotPool`` slot back. Enabled by default.
* ``reconcile_force`` -> :meth:`AccountService.reconcile_inflight`. This one has
  teeth: it rewrites the live ``_image_inflight`` map that gates admission.
  Enabled by default but operator-disablable via config, and additionally gated
  on a multi-tick confirmation streak plus a "every holder is identifiable"
  precondition, so a momentary race can never be mistaken for a leak.

It runs on its own daemon thread (:meth:`start_background`); ``/health`` may also
drive :meth:`tick` for on-demand inspection. Both paths take the same tick lock
and share a coalescing window, so a monitoring burst can neither interleave with
nor re-apply a scheduled correction.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

from services.image_pipeline.slot_ledger import slot_ledger

logger = logging.getLogger(__name__)

_THREAD_NAME = "pipeline-watchdog-loop"
_RESUME_POLL_PROGRESS = "resume_polling"
_POOL_KEYS = ("ps", "ss", "upload", "download")
# Fields on a task dict that carry the *account* access token. ``identity`` is
# deliberately absent: it holds the API caller's identity, not an account token,
# so probing it would attribute a live slot to the wrong key and silently defeat
# the unidentified-holder guard below.
_ACCOUNT_TOKEN_FIELDS = ("resume_access_token", "access_token", "account_token")

_FALLBACK_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "interval_secs": 30.0,
    "startup_delay_secs": 15.0,
    "min_tick_interval_secs": 5.0,
    "force_release_expired": True,
    "reconcile_force": True,
    "reconcile_confirm_ticks": 3,
}


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _token_fingerprint(token: str) -> str:
    """Stable, non-reversible token id safe to put in logs.

    ``AccountService.reconcile_inflight`` keys its drift map by
    ``token[:12] + "..."``. Production access tokens are JWTs, so every account
    shares the same base64 header prefix and those keys **collide across the
    whole pool**, collapsing the drift map to a single entry. The watchdog
    therefore never treats them as identity and fingerprints the full token.
    """
    return hashlib.blake2b(token.encode("utf-8"), digest_size=6).hexdigest()


class PipelineWatchdogService:
    def __init__(self) -> None:
        # ``_lock`` guards the small mutable report/streak state only.
        self._lock = threading.Lock()
        # ``_tick_lock`` serialises whole ticks so a /health tick and a loop tick
        # can never interleave inside a correction.
        self._tick_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick_mono = 0.0
        self._last_report: dict[str, Any] = {}
        self._over_count_streaks: dict[str, int] = {}
        self._tick_count = 0
        self._coalesced_count = 0
        self._corrections_applied = 0

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        try:
            from services.config import config

            settings = config.get_image_pipeline_watchdog_settings()
            if isinstance(settings, dict) and settings:
                return dict(settings)
        except Exception as exc:
            logger.debug({"event": "watchdog_settings_error", "error": str(exc)})
        return dict(_FALLBACK_SETTINGS)

    def _interval_secs(self, settings: dict[str, Any]) -> float:
        return max(0.01, _as_float(settings.get("interval_secs"), 30.0))

    def _coalesce_window_secs(self, settings: dict[str, Any]) -> float:
        """Minimum spacing between two *real* ticks.

        Capped at half the loop interval so the background loop is never
        throttled by its own guard, while a burst of ``/health`` GETs still
        collapses onto one correction.
        """
        configured = max(0.0, _as_float(settings.get("min_tick_interval_secs"), 5.0))
        return min(configured, self._interval_secs(settings) * 0.5)

    # ------------------------------------------------------------------
    # A3-4: expected in-flight baseline
    # ------------------------------------------------------------------

    def _account_slot_holders(self) -> tuple[dict[str, int], int]:
        """``(token -> live account-slot holders, unidentifiable holder count)``.

        Only a ``RUNNING`` task that is *not* resume-polling holds an
        ``_image_inflight`` account slot:

        * ``TIMEOUT_PENDING`` has already given its slot back before the status
          is written -- the hard-timeout branch calls ``release_slot_once()`` on
          every leased token first, and the upstream-timeout branch reaches
          ``code="image_timeout_pending"`` only after
          ``account_service.mark_image_result(token, False)``.
        * a resume poll re-enters as ``RUNNING`` with
          ``progress="resume_polling"`` but reuses ``resume_access_token``
          directly and never acquires a slot at all.

        Counting either one inflates ``expected`` for as long as anything is
        resuming, which is what made ``pipeline_inflight_drift`` structurally
        guaranteed (audit 28 §A4 / fix A3-4). ``RUNNING`` is set *before* the
        worker acquires its slot, so this baseline is an upper bound on real
        holders -- the safe direction for a shrink-only correction.

        The second element counts holders whose token could not be resolved.
        Any over-count could belong to one of those, so the caller must refuse
        to force a correction while it is non-zero.
        """
        counts: dict[str, int] = {}
        unknown = 0
        try:
            from services.image_task_service import TASK_STATUS_RUNNING, image_task_service
        except Exception as exc:
            logger.debug({"event": "watchdog_expected_import_error", "error": str(exc)})
            return counts, unknown

        is_resume_poll = getattr(image_task_service, "_is_resume_polling_task", None)
        try:
            with image_task_service._lock:
                runs = dict(getattr(image_task_service, "_active_pipeline_runs", None) or {})
                for key, task in image_task_service._tasks.items():
                    if not isinstance(task, dict):
                        continue
                    if task.get("status") != TASK_STATUS_RUNNING:
                        continue
                    if str(task.get("progress") or "").strip() == _RESUME_POLL_PROGRESS:
                        continue
                    if callable(is_resume_poll) and is_resume_poll(task):
                        continue
                    token = self._task_account_token(task, runs.get(key))
                    if token:
                        counts[token] = int(counts.get(token, 0)) + 1
                    else:
                        unknown += 1
        except Exception as exc:
            logger.debug({"event": "watchdog_expected_tokens_error", "error": str(exc)})
        return counts, unknown

    @staticmethod
    def _task_account_token(task: dict[str, Any], pipeline_run: object | None) -> str:
        """Account token a live task holds, or ``""`` when it cannot be resolved.

        The task dict only gains ``resume_access_token`` once a conversation id
        exists, so the bound ``PipelineRun._account_access_token`` is the earlier
        and more reliable source during the pre-conversation phase.
        """
        for field in _ACCOUNT_TOKEN_FIELDS:
            token = str(task.get(field) or "").strip()
            if token:
                return token
        return str(getattr(pipeline_run, "_account_access_token", "") or "").strip()

    def _running_token_counts(self) -> dict[str, int]:
        """Legacy name for the expected-inflight baseline.

        Before A3-4 this counted every ``RUNNING`` *or* ``TIMEOUT_PENDING`` task
        and compared that to ``account_service._image_inflight``, which
        permanently over-stated ``expected``. It now delegates to
        :meth:`_account_slot_holders`; the name is kept so any external caller
        keeps working.
        """
        counts, _unknown = self._account_slot_holders()
        return counts

    # ------------------------------------------------------------------
    # A3-1: drift observation and gated correction
    # ------------------------------------------------------------------

    @staticmethod
    def _memory_inflight_counts(account_service: Any) -> dict[str, int]:
        """Snapshot of the live ``_image_inflight`` map, taken under its own lock."""
        try:
            with account_service._image_slot_condition:
                return {
                    str(token): int(value or 0)
                    for token, value in account_service._image_inflight.items()
                }
        except Exception as exc:
            logger.debug({"event": "watchdog_memory_inflight_error", "error": str(exc)})
            return {}

    @staticmethod
    def _account_email(account_service: Any, token: str) -> str:
        try:
            with account_service._lock:
                account = account_service._accounts.get(token)
            if isinstance(account, dict):
                return str(account.get("email") or "")
        except Exception:
            pass
        return ""

    def _over_counted(
        self,
        account_service: Any,
        memory: dict[str, int],
        expected: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        """Tokens whose memory count exceeds the live-holder baseline.

        Keyed by full-token fingerprint rather than by ``reconcile_inflight``'s
        truncated drift key, which collides across JWT tokens.
        """
        over: dict[str, dict[str, Any]] = {}
        for token, memory_count in memory.items():
            expected_count = int(expected.get(token, 0))
            if memory_count <= expected_count:
                continue
            over[_token_fingerprint(token)] = {
                "memory": memory_count,
                "expected": expected_count,
                "delta": memory_count - expected_count,
                "email": self._account_email(account_service, token),
            }
        return over

    def _update_streaks(self, over_counted: dict[str, dict[str, Any]]) -> dict[str, int]:
        """Bump the streak of every still-over-counted token, drop the rest.

        A token that stops over-counting for even one tick restarts from zero,
        so a transient race can never accumulate enough confirmations.
        """
        with self._lock:
            self._over_count_streaks = {
                key: int(self._over_count_streaks.get(key, 0)) + 1 for key in over_counted
            }
            return dict(self._over_count_streaks)

    def _force_decision(
        self,
        settings: dict[str, Any],
        over_counted: dict[str, dict[str, Any]],
        streaks: dict[str, int],
        unknown_holders: int,
    ) -> dict[str, Any]:
        """Whether to hand ``reconcile_inflight`` its teeth this tick.

        ``reconcile_inflight(force=True)`` is all-or-nothing -- it corrects every
        token with ``memory > expected``, not a caller-selected subset -- so
        *every* over-counted token must be confirmed before any of them is
        touched. One flapping token defers the whole correction to a later tick,
        which is the conservative trade.
        """
        enabled = _as_bool(settings.get("reconcile_force"), True)
        confirm_ticks = max(1, _as_int(settings.get("reconcile_confirm_ticks"), 3))
        decision: dict[str, Any] = {
            "force_enabled": enabled,
            "confirm_ticks": confirm_ticks,
            "unknown_holders": unknown_holders,
            "forced": False,
            "reason": "",
        }
        if not over_counted:
            decision["reason"] = "no_over_count"
            return decision
        if not enabled:
            decision["reason"] = "disabled_by_config"
            return decision
        if unknown_holders > 0:
            # An unattributable holder means any over-count could be its live
            # slot. Report, never correct.
            decision["reason"] = "unidentified_holders"
            return decision
        unconfirmed = sorted(key for key in over_counted if int(streaks.get(key, 0)) < confirm_ticks)
        if unconfirmed:
            decision["reason"] = "awaiting_confirmation"
            decision["unconfirmed"] = unconfirmed
            return decision
        decision["forced"] = True
        decision["reason"] = "confirmed"
        return decision

    def _apply_correction(
        self,
        account_service: Any,
        expected: dict[str, int],
        over_counted: dict[str, dict[str, Any]],
        streaks: dict[str, int],
    ) -> dict[str, Any]:
        applied = account_service.reconcile_inflight(expected_by_token=expected, force=True)
        applied = applied if isinstance(applied, dict) else {}
        for key, item in over_counted.items():
            logger.warning(
                {
                    "event": "pipeline_inflight_forced_correction",
                    "token_fp": key,
                    "email": item.get("email") or "",
                    "memory": item.get("memory"),
                    "expected": item.get("expected"),
                    "delta": item.get("delta"),
                    "confirm_streak": int(streaks.get(key, 0)),
                }
            )
        corrected = _as_int(applied.get("corrected"), 0)
        with self._lock:
            self._corrections_applied += corrected
            for key in over_counted:
                self._over_count_streaks.pop(key, None)
        return applied

    # ------------------------------------------------------------------
    # A3-3: pool snapshot key access
    # ------------------------------------------------------------------

    @staticmethod
    def _pool_snapshots(pipeline: dict[str, Any]) -> dict[str, Any]:
        """Pool snapshots, whichever layer ``snapshot()`` puts them on.

        ``ImagePipelineScheduler.snapshot()`` returns ``PipelinePools.snapshot()``
        with its keys flattened at the top level (``ps``/``ss``/``upload``/
        ``download``/``in_flight``) plus ``ready_buffer`` and ``segments``. The
        watchdog used to read ``pipeline["pools"]`` -- a layer that has never
        existed -- so ``pipeline_pools`` came back ``null`` and ``ss_active`` /
        ``ss_queued`` were pinned to 0 forever (audit 28 §A5 / fix A3-3). The
        nested lookup is kept as a fallback in case the shape is ever re-nested.
        """
        nested = pipeline.get("pools")
        source = nested if isinstance(nested, dict) and any(k in nested for k in _POOL_KEYS) else pipeline
        pools: dict[str, Any] = {
            key: source[key] for key in _POOL_KEYS if isinstance(source.get(key), dict)
        }
        if "in_flight" in source:
            pools["in_flight"] = _as_int(source.get("in_flight"), 0)
        return pools

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------

    def tick(self, *, force_release_expired: bool | None = None) -> dict[str, Any]:
        """Run one watchdog pass.

        ``force_release_expired=None`` (the default, and what ``/health`` uses)
        takes the value from config. Passing an explicit bool overrides it.

        Ticks closer together than the coalescing window return the previous
        report tagged ``coalesced``, so on-demand ``/health`` inspection stays
        idempotent and cannot double-apply a correction.
        """
        settings = self._settings()
        if force_release_expired is None:
            force_release_expired = _as_bool(settings.get("force_release_expired"), True)
        with self._tick_lock:
            window = self._coalesce_window_secs(settings)
            elapsed = time.monotonic() - self._last_tick_mono
            if self._last_report and 0.0 <= elapsed < window:
                with self._lock:
                    self._coalesced_count += 1
                    report = dict(self._last_report)
                report["coalesced"] = True
                return report
            return self._tick_locked(settings, force_release_expired=bool(force_release_expired))

    def _tick_locked(self, settings: dict[str, Any], *, force_release_expired: bool) -> dict[str, Any]:
        from services.account_service import account_service
        from services.image_pipeline import image_pipeline_scheduler

        ledger_report = slot_ledger.watchdog_tick(force_release_expired=force_release_expired)

        expected, unknown_holders = self._account_slot_holders()
        memory = self._memory_inflight_counts(account_service)
        # Pass 1 is always observation-only: the force gate needs the drift map
        # before it may decide to act on it.
        inflight_drift = account_service.reconcile_inflight(expected_by_token=expected, force=False)
        inflight_drift = dict(inflight_drift) if isinstance(inflight_drift, dict) else {}
        over_counted = self._over_counted(account_service, memory, expected)
        streaks = self._update_streaks(over_counted)
        decision = self._force_decision(settings, over_counted, streaks, unknown_holders)
        if decision["forced"]:
            applied = self._apply_correction(account_service, expected, over_counted, streaks)
            inflight_drift = applied or inflight_drift

        pipeline = image_pipeline_scheduler.snapshot()
        pipeline = pipeline if isinstance(pipeline, dict) else {}
        pools = self._pool_snapshots(pipeline)
        ss_pool = pools.get("ss") if isinstance(pools.get("ss"), dict) else {}
        ps_pool = pools.get("ps") if isinstance(pools.get("ps"), dict) else {}

        with self._lock:
            self._tick_count += 1
            tick_count = self._tick_count
            coalesced_count = self._coalesced_count
            corrections_applied = self._corrections_applied

        report: dict[str, Any] = {
            "ts": time.time(),
            "slot_ledger": slot_ledger.snapshot(),
            "ledger_watchdog": ledger_report,
            "inflight_drift": inflight_drift,
            "pipeline_pools": pools,
            "ss_active": _as_int(ss_pool.get("active"), 0),
            "ss_queued": _as_int(ss_pool.get("queued"), 0),
            "ss_limit": _as_int(ss_pool.get("limit"), 0),
            "ps_active": _as_int(ps_pool.get("active"), 0),
            "ps_queued": _as_int(ps_pool.get("queued"), 0),
            "pipeline_in_flight": _as_int(pools.get("in_flight"), 0),
            "segments_recent": len(pipeline.get("segments") or []),
            "force_release_expired": bool(force_release_expired),
            "expected_holders": sum(expected.values()),
            "unidentified_holders": unknown_holders,
            "inflight_over_count": over_counted,
            "over_count_streaks": streaks,
            "reconcile": {
                **decision,
                "corrected": _as_int(inflight_drift.get("corrected"), 0),
            },
            "tick_count": tick_count,
            "coalesced_count": coalesced_count,
            "corrections_applied": corrections_applied,
            "coalesced": False,
            "loop": self.loop_status(),
        }
        with self._lock:
            self._last_tick_mono = time.monotonic()
            self._last_report = report
        if inflight_drift.get("drift_count"):
            logger.warning({"event": "pipeline_inflight_drift", **inflight_drift})
        return report

    # ------------------------------------------------------------------
    # A3-2: background loop
    # ------------------------------------------------------------------

    def start_background(self) -> None:
        """Start the watchdog loop thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(self._stop_event,),
                name=_THREAD_NAME,
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop_background(self, *, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
            if not thread.is_alive():
                with self._lock:
                    if self._thread is thread:
                        self._thread = None

    def _run(self, stop_event: threading.Event) -> None:
        """Interval loop. ``stop_event`` is passed in so a restart cannot make an
        old thread observe the new event and keep running."""
        settings = self._settings()
        startup_delay = max(0.0, _as_float(settings.get("startup_delay_secs"), 15.0))
        if startup_delay > 0 and stop_event.wait(startup_delay):
            return
        while not stop_event.is_set():
            settings = self._settings()
            interval = self._interval_secs(settings)
            if not _as_bool(settings.get("enabled"), True):
                if stop_event.wait(min(5.0, interval)):
                    return
                continue
            try:
                self.tick()
            except Exception as exc:
                logger.warning(
                    {"event": "pipeline_watchdog_tick_error", "error": str(exc)},
                    exc_info=True,
                )
            if stop_event.wait(interval):
                return

    def loop_status(self) -> dict[str, Any]:
        thread = self._thread
        return {
            "running": bool(thread is not None and thread.is_alive()),
            "thread_name": thread.name if thread is not None else "",
            "stop_requested": self._stop_event.is_set(),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_tick_mono": self._last_tick_mono,
                "last_report": dict(self._last_report),
                "tick_count": self._tick_count,
                "coalesced_count": self._coalesced_count,
                "corrections_applied": self._corrections_applied,
                "loop": self.loop_status(),
            }


pipeline_watchdog_service = PipelineWatchdogService()
