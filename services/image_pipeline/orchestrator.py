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
        self._ss_holders: dict[str, int] = {}
        self._ss_released_indices: set[int] = set()
        self.persist_hook: Any = None
        self._ss_started_at: dict[int, float] = {}
        self._download_started_at: float | None = None
        self._account_wait_started_mono: float | None = None
        self._account_acquired_mono: float | None = None
        self._sse_stream_recorded = False
        self._account_access_token = ""
        self._account_slot_released_after_sse = False

    def bind_account_token(self, access_token: str) -> None:
        self._account_access_token = str(access_token or "").strip()

    def _release_account_slot_after_sse(self) -> None:
        if self._account_slot_released_after_sse or not self._account_access_token:
            return
        if not config.image_release_account_after_sse:
            return
        from services.account_service import account_service

        account_service.release_image_slot(self._account_access_token)
        self._account_slot_released_after_sse = True

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
        if image_index in self._ss_released_indices:
            return
        holder = self._holder(f"ss-{image_index}")
        release_slot = self._ss_holders.get(holder, self.state.ss_slot)
        if release_slot is not None and release_slot >= 0:
            schedule_trace.emit("ss_slot_released", int(release_slot))
            self._pools.ss.release(release_slot, holder)
            self._ss_holders.pop(holder, None)
            self._ss_released_indices.add(image_index)
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
        queue_ms = self._pools.upload.acquire(holder)
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
        slot, queue_ms = self._pools.ps.acquire(holder)
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
        slot, queue_ms = self._pools.ss.acquire(holder)
        if schedule_trace.enabled():
            snap = self._pools.ss.snapshot()
            schedule_trace.emit(
                "ss_slot_acquired",
                schedule_trace.pack_pool_aux(active=snap.active, queued=snap.queued, slot=slot),
            )
        self._ss_holders[holder] = slot
        self.state.timings.ss_queue_ms += int(queue_ms)
        self.state.phase = PipelinePhase.SSE
        self.state.ss_slot = slot
        self._ss_started_at[image_index] = time.monotonic()
        self._scheduler._record_segment(task_key=self.task_key, stage="sse", slot=slot, active=True)
        return slot, int(queue_ms)

    def release_ss(self, *, image_index: int, slot: int | None = None) -> None:
        if image_index in self._ss_released_indices:
            return
        holder = self._holder(f"ss-{image_index}")
        release_slot = slot if slot is not None else self._ss_holders.get(holder, self.state.ss_slot)
        if release_slot is None or release_slot < 0:
            return
        schedule_trace.emit("ss_slot_released", int(release_slot))
        self._pools.ss.release(release_slot, holder)
        self._ss_holders.pop(holder, None)
        if self.state.ss_slot == release_slot:
            self.state.ss_slot = None
        self._ss_released_indices.add(image_index)
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
        queue_ms = self._pools.download.acquire(holder)
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

    def finish(self) -> PhaseTimingsMs:
        schedule_trace.emit("pipeline_finish")
        self.state.phase = PipelinePhase.DELIVERED
        self.state.timings.wall_clock_ms = int((time.monotonic() - self._started_at) * 1000)
        self._pools.finish()
        ready_buffer_tracker.release(self.task_key)
        self.set_cursor(RetryPhaseCursor.DL_DONE, pipeline_phase=PipelinePhase.DELIVERED.value)
        self._persist(phase_timings_ms=self.state.timings.to_dict())
        return self.state.timings


image_pipeline_scheduler = ImagePipelineScheduler()
