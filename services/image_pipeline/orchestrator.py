from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from services.config import config
from services.image_pipeline.account_provider import StageAccountProvider
from services.image_pipeline.pools import PipelinePools
from services.image_pipeline.prompt import normalize_multi_image_mode, ps_rounds_for_request, should_need_ps
from services.image_pipeline.ready_buffer import ready_buffer_tracker
from services.image_pipeline import schedule_trace
from services.image_pipeline.slot_ledger import slot_ledger
from services.image_pipeline.types import MultiImageMode, PhaseTimingsMs, PipelinePhase, PipelineRunState, RetryPhaseCursor


class ImagePipelineScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pools: PipelinePools | None = None
        self._segments: list[dict[str, Any]] = []
        self._segments_lock = threading.Lock()

    def _record_segment(self, *, task_key: str, stage: str, slot: int | None, active: bool) -> None:
        with self._segments_lock:
            if active:
                self._segments.append({
                    "task_key": task_key,
                    "stage": stage,
                    "slot": slot,
                    "started_at": time.time(),
                })
            else:
                for item in reversed(self._segments):
                    if item.get("task_key") == task_key and item.get("stage") == stage and item.get("ended_at") is None:
                        item["ended_at"] = time.time()
                        break
            if len(self._segments) > 500:
                self._segments = self._segments[-500:]

    def _settings(self) -> dict[str, object]:
        return config.get_image_pipeline_settings()

    def enabled(self) -> bool:
        return bool(self._settings().get("enabled"))

    def _pools_locked(self) -> PipelinePools:
        settings = self._settings()
        prompt_slots = int(settings.get("prompt_slots") or 10)
        sse_slots = int(settings.get("sse_slots") or 10)
        download_concurrency = int(settings.get("download_concurrency") or 8)
        upload_concurrency = int(settings.get("asset_upload_concurrency") or 8)
        if (
            self._pools is None
            or self._pools.ps.slots != prompt_slots
            or self._pools.ss.slots != sse_slots
            or self._pools.download.limit != download_concurrency
            or self._pools.upload.limit != upload_concurrency
        ):
            self._pools = PipelinePools(
                prompt_slots=prompt_slots,
                sse_slots=sse_slots,
                download_concurrency=download_concurrency,
                upload_concurrency=upload_concurrency,
            )
        return self._pools

    def pools(self) -> PipelinePools:
        with self._lock:
            return self._pools_locked()

    def relaxed_per_user_running_max(self) -> int | None:
        if not self.enabled():
            return None
        if not bool(self._settings().get("relaxed_per_user_running", True)):
            return None
        return int(self._settings().get("sse_slots") or 10)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pools = self._pools_locked()
            base = pools.snapshot()
        with self._segments_lock:
            segments = [dict(item) for item in self._segments[-200:]]
        base["ready_buffer"] = ready_buffer_tracker.snapshot()
        base["segments"] = segments
        base["total_queue_depth"] = base.get("ss_queue_depth", 0) + base.get("ps_queue_depth", 0)
        ps = base.get("ps") or {}
        ss = base.get("ss") or {}
        base["slot_topology"] = {
            "ps_capacity": int(ps.get("limit") or 0),
            "ss_capacity": int(ss.get("limit") or 0),
            "ps_inflight": int(ps.get("active") or 0),
            "ss_inflight": int(ss.get("active") or 0),
            "ps_queued": int(ps.get("queued") or 0),
            "ss_queued": int(ss.get("queued") or 0),
            "overflow_pending": int(base.get("total_queue_depth") or 0),
            "pipeline_in_flight": int(base.get("in_flight") or 0),
        }
        return base

    def begin_run(
        self,
        *,
        task_key: str,
        mode: str,
        payload: dict[str, Any],
    ) -> PipelineRun:
        settings = self._settings()
        pools = self.pools()
        global_queue_max = int(settings.get("global_queue_max") or 200)
        pools.admit(global_queue_max)
        # admit() has exactly one counterpart in the repo: PipelineRun.finish() ->
        # pools.finish(). If anything below raises we never hand a PipelineRun back, so
        # the caller has nothing to finish() and the permit leaks forever. After
        # global_queue_max such leaks every task fails with "image pipeline global queue
        # is full" and nothing reconciles the counter (audit 28 §B9).
        try:
            if schedule_trace.enabled():
                schedule_trace.emit("pipeline_admit")
            prompt = str(payload.get("prompt") or "")
            prompt_enhance = bool(payload.get("prompt_enhance"))
            multi_image_mode = normalize_multi_image_mode(payload.get("multi_image_mode"))
            n = max(1, int(payload.get("n") or 1))
            needs_ps = should_need_ps(prompt_enhance=prompt_enhance, prompt=prompt)
            return PipelineRun(
                scheduler=self,
                state=PipelineRunState(task_key=task_key, mode=mode),
                pools=pools,
                settings=settings,
                payload=payload,
                needs_ps=needs_ps,
                multi_image_mode=multi_image_mode,
                n=n,
                account_provider=StageAccountProvider(),
                started_at=time.monotonic(),
            )
        except BaseException:
            pools.finish()
            raise


class PipelineRun:
    def __init__(
        self,
        *,
        scheduler: ImagePipelineScheduler,
        state: PipelineRunState,
        pools: PipelinePools,
        settings: dict[str, object],
        payload: dict[str, Any],
        needs_ps: bool,
        multi_image_mode: MultiImageMode,
        n: int,
        account_provider: StageAccountProvider,
        started_at: float,
    ) -> None:
        self._scheduler = scheduler
        self.state = state
        self._pools = pools
        self._settings = settings
        self.payload = payload
        self.needs_ps = needs_ps
        self.multi_image_mode = multi_image_mode
        self.n = n
        self.account_provider = account_provider
        self._started_at = started_at
        self._ps_rounds_remaining = ps_rounds_for_request(
            n=n,
            multi_image_mode=multi_image_mode,
            needs_ps=needs_ps,
        )
        self._ps_rounds_done = 0
        self._upload_holder = ""
        self._download_holder = ""
        # holder -> slot for the *live* sS acquisition. Presence here is the single
        # source of truth for "this index currently holds an sS slot", and therefore
        # the release-idempotency key (audit 28 §B2).
        self._ss_holders: dict[str, int] = {}
        # Indices whose current acquisition has already been released. Diagnostic /
        # audit trail only: it is re-armed (discarded) by acquire_ss, so it must never
        # be used to short-circuit a release for a *re-acquired* slot.
        self._ss_released_indices: set[int] = set()
        self.persist_hook: Any = None
        # Full sS-stage clock (acquire_ss -> release_ss). Feeds timings.ss_ms, which the
        # gantt view splits into sse_active / poll_resolve — must span the whole stage.
        self._ss_started_at: dict[int, float] = {}
        # Fast-fail wall clock, armed at acquire_ss and disarmed at the end of the SSE
        # streaming phase (note_ss_stream_phase_end). Deliberately *not* the same clock
        # as _ss_started_at: the wall must not cap the legitimate poll/download budgets.
        self._ss_wall_started_at: dict[int, float] = {}
        self._download_started_at: float | None = None
        self._account_wait_started_mono: float | None = None
        self._account_acquired_mono: float | None = None
        self._sse_stream_recorded = False
        self._account_access_token = ""
        self._account_slot_released_after_sse = False
        self._account_ledger_registered = False
        # One-shot guard for finish(): the global admission permit taken by
        # ImagePipelineScheduler.begin_run() must be handed back exactly once.
        self._finish_lock = threading.Lock()
        self._finished = False

    def _account_ledger_holder(self) -> str:
        return f"{self.state.task_key}:account"

    def bind_account_token(self, access_token: str) -> None:
        self._account_access_token = str(access_token or "").strip()
        if self._account_access_token and not self._account_ledger_registered:
            slot_ledger.try_acquire_account(
                self._account_ledger_holder(),
                self._account_access_token,
                # Watchdog forced-release deadline: must cover the whole lease, not just
                # the SSE phase, otherwise enabling force_release_expired would yank the
                # account slot out from under a legitimate long poll.
                deadline_secs=config.image_ss_slot_deadline_secs,
            )
            self._account_ledger_registered = True

    def _release_account_ledger(self) -> None:
        if not self._account_ledger_registered:
            return
        slot_ledger.release_account(self._account_ledger_holder())
        self._account_ledger_registered = False

    def assert_ss_wall_ok(self, *, image_index: int) -> None:
        """Fast-fail a stuck SSE stream.

        Scope is the SSE streaming phase only (armed by ``acquire_ss``, disarmed by
        ``note_ss_stream_phase_end``). Everything downstream of the stream —
        conversation polling (120/300/360s budgets), URL resolve, download, return
        window — carries its own budget. Letting this much shorter wall cover them
        threw away images that upstream had already generated, billed and that we had
        already downloaded (audit 28 §B1).
        """
        started = self._ss_wall_started_at.get(image_index)
        if started is None:
            return
        limit = config.image_ss_stage_wall_timeout_secs
        if time.monotonic() - started > limit:
            raise TimeoutError(f"sS stage wall timeout ({limit:.0f}s)")

    def note_ss_stream_phase_end(self, *, image_index: int) -> None:
        """Disarm the sS fast-fail wall: the SSE streaming phase is over.

        Idempotent, and safe to call for an index that never armed the wall.
        """
        self._ss_wall_started_at.pop(image_index, None)

    def _release_account_slot_after_sse(self) -> None:
        if self._account_slot_released_after_sse or not self._account_access_token:
            return
        if not config.image_release_account_after_sse:
            return
        from services.account_service import account_service

        account_service.release_image_slot(self._account_access_token)
        self._account_slot_released_after_sse = True
        self._release_account_ledger()

    @property
    def task_key(self) -> str:
        return self.state.task_key

    def _persist(self, **fields: Any) -> None:
        hook = self.persist_hook
        if callable(hook):
            try:
                hook(**fields)
            except Exception:
                pass

    def set_cursor(self, cursor: RetryPhaseCursor, **extra: Any) -> None:
        self.state.retry_phase_cursor = cursor
        self._persist(retry_phase_cursor=cursor.value, **extra)

    def note_ps_done(self) -> None:
        self.set_cursor(RetryPhaseCursor.PS_DONE, pipeline_phase=PipelinePhase.PS.value)

    def mark_account_wait_start(self) -> None:
        self._account_wait_started_mono = time.monotonic()
        schedule_trace.emit("account_wait_start")

    def mark_account_acquired(self) -> None:
        started = self._account_wait_started_mono
        if started is None:
            return
        self.state.timings.account_queue_ms += max(0, int((time.monotonic() - started) * 1000))
        self._account_acquired_mono = time.monotonic()
        self._account_wait_started_mono = None
        schedule_trace.emit("account_acquired")

    def mark_sse_stream_end(self) -> None:
        if self._sse_stream_recorded:
            return
        acquired = self._account_acquired_mono
        if acquired is not None:
            self.state.timings.sse_stream_ms += max(0, int((time.monotonic() - acquired) * 1000))
        self._sse_stream_recorded = True
        schedule_trace.emit("sse_stream_end")
        self._release_account_slot_after_sse()

    def mark_poll_resolve_end(self) -> None:
        schedule_trace.emit("poll_resolve_end")

    def on_sediment_captured(self, *, image_index: int, sediment_ids: list[str]) -> None:
        if not sediment_ids:
            return
        for sid in sediment_ids:
            if sid and sid not in self.state.sediment_ids:
                self.state.sediment_ids.append(sid)
        self.set_cursor(
            RetryPhaseCursor.SS_DONE,
            pipeline_phase=PipelinePhase.SSE.value,
            sediment_ids=list(self.state.sediment_ids),
        )
        ready_buffer_tracker.admit(self.task_key)
        holder = self._holder(f"ss-{image_index}")
        if holder not in self._ss_holders:
            # No live acquisition for this index: already released early, or never
            # acquired. Never fall back to state.ss_slot here — that would release a
            # sibling image's slot.
            return
        release_slot = self._ss_holders.get(holder)
        if release_slot is not None and release_slot >= 0:
            schedule_trace.emit("ss_slot_released", int(release_slot))
            slot_ledger.release_ss(holder)
            self._pools.ss.release(release_slot, holder)
            self._ss_holders.pop(holder, None)
            self._ss_released_indices.add(image_index)
            self._ss_wall_started_at.pop(image_index, None)
            started = self._ss_started_at.pop(image_index, None)
            if started is not None:
                self.state.timings.ss_ms += int((time.monotonic() - started) * 1000)
            self._scheduler._record_segment(
                task_key=self.task_key,
                stage="sse",
                slot=release_slot,
                active=False,
            )
        self._release_account_slot_after_sse()

    def _holder(self, suffix: str) -> str:
        return f"{self.state.task_key}:{suffix}"

    def acquire_upload(self) -> int:
        self.state.phase = PipelinePhase.QUEUE_UPLOAD
        holder = self._holder("upload")
        self._upload_holder = holder
        try:
            queue_ms = self._pools.upload.acquire(holder, timeout=config.image_pool_acquire_timeout_secs)
        except TimeoutError:
            # We never took a permit; clear the holder so a later release_upload cannot
            # decrement the semaphore we did not increment.
            self._upload_holder = ""
            raise
        self.state.timings.upload_queue_ms += int(queue_ms)
        self.state.phase = PipelinePhase.UPLOAD
        return int(queue_ms)

    def release_upload(self) -> None:
        if not self._upload_holder:
            return
        self._pools.upload.release(self._upload_holder)
        self._upload_holder = ""

    def acquire_ps(self) -> tuple[int, int]:
        if not self.needs_ps or self._ps_rounds_done >= self._ps_rounds_remaining:
            return -1, 0
        self.state.phase = PipelinePhase.QUEUE_PS
        holder = self._holder(f"ps-{self._ps_rounds_done}")
        slot, queue_ms = self._pools.ps.acquire(holder, timeout=config.image_pool_acquire_timeout_secs)
        self.state.timings.ps_queue_ms += int(queue_ms)
        self.state.phase = PipelinePhase.PS
        self.state.ps_slot = slot
        self._ps_rounds_done += 1
        self._scheduler._record_segment(task_key=self.task_key, stage="ps", slot=slot, active=True)
        return slot, int(queue_ms)

    def release_ps(self, slot: int | None = None) -> None:
        release_slot = slot if slot is not None else self.state.ps_slot
        if release_slot is None or release_slot < 0:
            return
        holder = self._holder(f"ps-{max(0, self._ps_rounds_done - 1)}")
        self._pools.ps.release(release_slot, holder)
        self.state.ps_slot = None
        self._scheduler._record_segment(task_key=self.task_key, stage="ps", slot=release_slot, active=False)

    def acquire_ss(self, *, image_index: int) -> tuple[int, int]:
        if schedule_trace.enabled():
            schedule_trace.emit("ready_buffer_wait_start")
        ready_buffer_tracker.wait_for_ss_slot()
        if schedule_trace.enabled():
            schedule_trace.emit("ready_buffer_wait_end")
            snap = self._pools.ss.snapshot()
            schedule_trace.emit(
                "ss_queue_enter",
                schedule_trace.pack_pool_aux(active=snap.active, queued=snap.queued),
            )
        self.state.phase = PipelinePhase.QUEUE_SS
        holder = self._holder(f"ss-{image_index}")
        # Watchdog forced-release deadline for the whole slot hold (SSE + poll + download),
        # not the SSE-only fast-fail wall.
        ss_deadline = config.image_ss_slot_deadline_secs
        slot_ledger.try_acquire_ss(holder, deadline_secs=ss_deadline)
        try:
            slot, queue_ms = self._pools.ss.acquire(holder, timeout=config.image_pool_acquire_timeout_secs)
        except TimeoutError:
            # Roll the ledger lease back: we are not holding an sS slot.
            slot_ledger.release_ss(holder)
            raise
        if schedule_trace.enabled():
            snap = self._pools.ss.snapshot()
            schedule_trace.emit(
                "ss_slot_acquired",
                schedule_trace.pack_pool_aux(active=snap.active, queued=snap.queued, slot=slot),
            )
        self._ss_holders[holder] = slot
        # Re-arm: this index holds a *new* acquisition, so a previous release must not
        # keep suppressing the next one (audit 28 §B2).
        self._ss_released_indices.discard(image_index)
        self.state.timings.ss_queue_ms += int(queue_ms)
        self.state.phase = PipelinePhase.SSE
        self.state.ss_slot = slot
        now = time.monotonic()
        self._ss_started_at[image_index] = now
        self._ss_wall_started_at[image_index] = now
        self._scheduler._record_segment(task_key=self.task_key, stage="sse", slot=slot, active=True)
        return slot, int(queue_ms)

    def release_ss(self, *, image_index: int, slot: int | None = None) -> None:
        """Release the sS slot held for ``image_index``.

        Idempotency is keyed on the *live acquisition* (``_ss_holders``), not on "this
        index was released at some point in the past". ``_generate_single_image`` is a
        ``while True:`` retry loop whose ``continue`` statements run this ``finally``,
        so the same index legitimately re-acquires the slot after a release. Gating on a
        monotonically-growing index set turned every retry's release into a no-op and
        leaked the slot plus its ledger entry permanently (audit 28 §B2).
        """
        holder = self._holder(f"ss-{image_index}")
        if holder not in self._ss_holders:
            # Already released (e.g. early release from on_sediment_captured) or never
            # acquired. Never fall back to state.ss_slot here — that would release a
            # sibling image's slot.
            return
        release_slot = slot if slot is not None else self._ss_holders.get(holder)
        if release_slot is None or release_slot < 0:
            return
        schedule_trace.emit("ss_slot_released", int(release_slot))
        slot_ledger.release_ss(holder)
        self._pools.ss.release(release_slot, holder)
        self._ss_holders.pop(holder, None)
        if self.state.ss_slot == release_slot:
            self.state.ss_slot = None
        self._ss_released_indices.add(image_index)
        self._ss_wall_started_at.pop(image_index, None)
        started = self._ss_started_at.pop(image_index, None)
        if started is not None:
            self.state.timings.ss_ms += int((time.monotonic() - started) * 1000)
        self._scheduler._record_segment(task_key=self.task_key, stage="sse", slot=release_slot, active=False)

    def acquire_download(self) -> int:
        schedule_trace.emit("download_start")
        self._download_started_at = time.monotonic()
        self.state.phase = PipelinePhase.QUEUE_DOWNLOAD
        holder = self._holder("download")
        self._download_holder = holder
        try:
            queue_ms = self._pools.download.acquire(holder, timeout=config.image_pool_acquire_timeout_secs)
        except TimeoutError:
            # We never took a permit; clear the holder so the caller's finally-block
            # release_download() cannot decrement the semaphore we did not increment.
            self._download_holder = ""
            self._download_started_at = None
            raise
        self.state.timings.download_queue_ms += int(queue_ms)
        self.state.phase = PipelinePhase.DOWNLOAD
        return int(queue_ms)

    def release_download(self) -> None:
        if not self._download_holder:
            return
        if self._download_started_at is not None:
            self.state.timings.download_ms += int((time.monotonic() - self._download_started_at) * 1000)
            self._download_started_at = None
        self._pools.download.release(self._download_holder)
        self._download_holder = ""
        schedule_trace.emit("download_end")
        self.set_cursor(RetryPhaseCursor.DL_DONE, pipeline_phase=PipelinePhase.DOWNLOAD.value)
        ready_buffer_tracker.release(self.task_key)

    @contextmanager
    def hold_ss_slot(self, image_index: int) -> Iterator[int]:
        slot, _queue_ms = self.acquire_ss(image_index=image_index)
        try:
            yield slot
        finally:
            self.release_ss(image_index=image_index, slot=slot)

    def sweep_leaked_slots(self) -> None:
        """Last-resort release of anything still held as the run terminalises.

        Every normal path releases its own slot; this only fires when an exception
        skipped a release. Concretely: a pool acquire timeout inside ``acquire_ss``
        aborts before ``mark_sse_stream_end``, so the account ledger lease taken by
        ``bind_account_token`` would otherwise never be given back. With ``sse_slots=10``
        a handful of such leaks wedges the whole pipeline (audit 28 §B2).

        Safe to call unconditionally: every release below is idempotent.
        """
        prefix = f"{self.state.task_key}:ss-"
        for holder in list(self._ss_holders.keys()):
            if not holder.startswith(prefix):
                continue
            try:
                image_index = int(holder[len(prefix):])
            except ValueError:
                continue
            try:
                self.release_ss(image_index=image_index)
            except Exception:
                pass
        try:
            self.release_upload()
        except Exception:
            pass
        try:
            self.release_download()
        except Exception:
            pass
        try:
            self._release_account_ledger()
        except Exception:
            pass

    def finish(self) -> PhaseTimingsMs:
        """Terminalise the run and give the global admission permit back.

        ``PipelinePools._in_flight`` is incremented once per run by ``begin_run`` and has
        exactly one decrement site — ``self._pools.finish()`` below. The sole caller of
        this method (``ImageTaskService._finalize_pipeline_run``) wraps it in a bare
        ``except Exception: pass``, so anything raising before that line used to leak an
        admission permit *silently* and permanently; after ``global_queue_max`` such
        leaks every task fails with "image pipeline global queue is full" and no
        watchdog reconciles the counter (audit 28 §B9). Hence:

        * ``try/finally`` — the decrement happens even if ``schedule_trace``,
          ``sweep_leaked_slots`` or the persist hook blows up.
        * one-shot guard — a second call must not decrement a *live* run's permit. Only
          one caller exists today, but the reaper in ``image_task_service`` may also
          finish an abandoned run, so idempotency is load-bearing.
        """
        with self._finish_lock:
            if self._finished:
                return self.state.timings
            self._finished = True
        try:
            schedule_trace.emit("pipeline_finish")
            self.sweep_leaked_slots()
            self.state.phase = PipelinePhase.DELIVERED
            self.state.timings.wall_clock_ms = int((time.monotonic() - self._started_at) * 1000)
            ready_buffer_tracker.release(self.task_key)
            self.set_cursor(RetryPhaseCursor.DL_DONE, pipeline_phase=PipelinePhase.DELIVERED.value)
            self._persist(phase_timings_ms=self.state.timings.to_dict())
            return self.state.timings
        finally:
            self._pools.finish()


image_pipeline_scheduler = ImagePipelineScheduler()
