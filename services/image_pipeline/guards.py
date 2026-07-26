from __future__ import annotations

from services.account_service import account_service
from services.image_pipeline.types import ImagePoolStarvedError


def ensure_dispatchable_pool(*, min_count: int = 2) -> None:
    """Reject enqueue only when the schedulable pool itself is depleted.

    ``dispatchable_candidate_count`` drops to 0 while global inflight is saturated;
    that is expected during conc10 and must not block new queue entries.
    """
    try:
        stats = account_service.get_image_candidate_runtime_stats()
        ready = int(stats.get("ready_candidate_count") or 0)
        schedulable = int(stats.get("schedulable_candidate_count") or 0)
        pool = max(ready, schedulable)
    except Exception:
        pool = 0
    if pool < max(1, int(min_count)):
        raise ImagePoolStarvedError(
            f"image pool starved: ready={pool} < {min_count}"
        )
