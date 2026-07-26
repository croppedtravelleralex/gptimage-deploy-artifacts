from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PipelinePhase(str, Enum):
    ADMIT = "admit"
    QUEUE_UPLOAD = "queue_upload"
    UPLOAD = "upload"
    QUEUE_PS = "queue_ps"
    PS = "ps"
    QUEUE_SS = "queue_ss"
    SSE = "sse"
    QUEUE_DOWNLOAD = "queue_download"
    DOWNLOAD = "download"
    DELIVERED = "delivered"


class RetryPhaseCursor(str, Enum):
    QUEUED = "QUEUED"
    PS_DONE = "PS_DONE"
    SS_DONE = "SS_DONE"
    DL_DONE = "DL_DONE"


class MultiImageMode(str, Enum):
    FAST = "fast"
    DIVERSE = "diverse"


class ImagePoolStarvedError(RuntimeError):
    """Raised when dispatchable account pool is too small to admit new image work."""


@dataclass
class PhaseTimingsMs:
    admit_queue_ms: int = 0
    upload_queue_ms: int = 0
    ps_queue_ms: int = 0
    ss_queue_ms: int = 0
    account_queue_ms: int = 0
    download_queue_ms: int = 0
    upload_ms: int = 0
    ps_ms: int = 0
    ss_ms: int = 0
    sse_stream_ms: int = 0
    download_ms: int = 0
    wall_clock_ms: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "admit_queue_ms": int(self.admit_queue_ms),
            "upload_queue_ms": int(self.upload_queue_ms),
            "ps_queue_ms": int(self.ps_queue_ms),
            "ss_queue_ms": int(self.ss_queue_ms),
            "account_queue_ms": int(self.account_queue_ms),
            "download_queue_ms": int(self.download_queue_ms),
            "upload_ms": int(self.upload_ms),
            "ps_ms": int(self.ps_ms),
            "ss_ms": int(self.ss_ms),
            "sse_stream_ms": int(self.sse_stream_ms),
            "download_ms": int(self.download_ms),
            "wall_clock_ms": int(self.wall_clock_ms),
        }

    @classmethod
    def from_dict(cls, value: object) -> PhaseTimingsMs:
        if not isinstance(value, dict):
            return cls()
        return cls(
            admit_queue_ms=int(value.get("admit_queue_ms") or 0),
            upload_queue_ms=int(value.get("upload_queue_ms") or 0),
            ps_queue_ms=int(value.get("ps_queue_ms") or 0),
            ss_queue_ms=int(value.get("ss_queue_ms") or 0),
            account_queue_ms=int(value.get("account_queue_ms") or 0),
            download_queue_ms=int(value.get("download_queue_ms") or 0),
            upload_ms=int(value.get("upload_ms") or 0),
            ps_ms=int(value.get("ps_ms") or 0),
            ss_ms=int(value.get("ss_ms") or 0),
            sse_stream_ms=int(value.get("sse_stream_ms") or 0),
            download_ms=int(value.get("download_ms") or 0),
            wall_clock_ms=int(value.get("wall_clock_ms") or 0),
        )


@dataclass
class PipelineRunState:
    task_key: str
    mode: str = "generate"
    phase: PipelinePhase = PipelinePhase.ADMIT
    retry_phase_cursor: RetryPhaseCursor = RetryPhaseCursor.QUEUED
    timings: PhaseTimingsMs = field(default_factory=PhaseTimingsMs)
    ps_account_id: str = ""
    ps_access_token: str = ""
    ps_slot: int | None = None
    ss_slot: int | None = None
    enhanced_prompt: str = ""
    sediment_ids: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
