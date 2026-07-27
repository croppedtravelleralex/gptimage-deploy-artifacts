"""A1-5 / A1-6 regression tests.

A1-5: `process_one` used to `popleft` the work item *before* validating it, and
every validation failure (closed schedule gate, daily cap, upstream 5xx) bubbled
to the worker loop's bare `except` — 616 items destroyed in 24h against a 720/24h
ceiling. The queue must now hold the item until it is explicitly committed,
requeued with backoff, or retired to a dead-letter terminus.

A1-6: `business_hours` / `extended_business` only filled Mon–Fri and the gate was
a strict `>` against 0.15, so every weekend was a total blackout for every
binding (and `business_hours` is the default preset). `minimal` and `sg_remote`
could never pass the gate at any time.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.ip_nurture_schedule import (
    SLOT_ALLOW_THRESHOLD,
    SLOTS_PER_DAY,
    current_slot_weight,
    get_preset,
    list_presets,
    resolve_binding_matrix,
    slot_allowed,
)
from services.text_nurture_service import (
    LOOP_BACKOFF_MAX_SEC,
    TerminalNurtureError,
    TextNurtureService,
)
from services.text_task_queue import (
    LEASE_TIMEOUT_SEC,
    MAX_ATTEMPTS,
    RETRY_BACKOFF_MAX_SEC,
    TextTaskQueue,
    retry_delay_sec,
)

# Explicit UTC instants (SGT = UTC+8) so nothing depends on the real clock.
MON_10_SGT = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)  # Mon 2026-07-20 10:00 SGT
SAT_14_SGT = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)  # Sat 2026-07-25 14:00 SGT
SUN_11_SGT = datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc)  # Sun 2026-07-26 11:00 SGT
SAT_03_SGT = datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc)  # Sat 2026-07-25 03:00 SGT
SUN_03_SGT = datetime(2026, 7, 25, 19, 0, tzinfo=timezone.utc)  # Sun 2026-07-26 03:00 SGT

ACCOUNT = {
    "access_token": "tok-a1",
    "email": "queue@example.com",
    "status": "正常",
    "proxy_binding_hash": "bind-a1",
    "chat_persist_history": True,
}

PAYLOAD = {
    "prompt": "Explain HTTP 429 in plain language for an ops engineer.",
    "access_token": "tok-a1",
    "email": "queue@example.com",
    "source": "auto",
    "model": "auto",
}


def _make_settings(**overrides: object) -> dict:
    base = {
        "enabled": True,
        "worker_enabled": True,
        "poll_interval_sec": 3.0,
        "max_per_hour": 0,
        "max_per_account_per_day": 6,
        "daily_reset_tz": "Asia/Singapore",
        "turns_per_session": 1,
        "turn_gap_sec": 0.0,
        "require_persist_history": False,
        "auto_enqueue": False,
        "auto_enqueue_every_sec": 600.0,
        "auto_enqueue_rotate_accounts": False,
        "count_manual_toward_daily_limit": True,
        "prompts": ["hello"],
        "session_follow_up_prompts": ["more"],
        "model": "auto",
    }
    base.update(overrides)
    return base


@contextmanager
def _patched(
    svc: TextNurtureService,
    queue: TextTaskQueue,
    settings: dict,
    *,
    slot_open: bool = True,
    collect_result: object = "ok",
):
    """Wire the service to an isolated queue; never touch the network."""
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("services.text_nurture_service.text_task_queue", queue))
        enter(patch("services.text_nurture_service._settings", return_value=settings))
        enter(patch("services.text_nurture_service.account_service.get_account", return_value=dict(ACCOUNT)))
        enter(patch("services.text_nurture_service.log_llm_ops"))
        enter(patch("services.text_nurture_service.OpenAIBackendAPI", return_value=MagicMock(account=dict(ACCOUNT))))
        if isinstance(collect_result, BaseException):
            enter(patch("services.text_nurture_service.collect_text", side_effect=collect_result))
        else:
            enter(patch("services.text_nurture_service.collect_text", return_value=collect_result))
        enter(patch.object(svc, "_slot_allowed", return_value=slot_open))
        yield


# --------------------------------------------------------------------------- #
# A1-5 — non-destructive dequeue
# --------------------------------------------------------------------------- #


def test_closed_slot_gate_keeps_work_item_in_queue() -> None:
    """The 339-hits/24h case: gate closed must requeue, not destroy."""
    svc = TextNurtureService()
    queue = TextTaskQueue()
    item = queue.enqueue(dict(PAYLOAD))
    with _patched(svc, queue, _make_settings(), slot_open=False):
        with pytest.raises(RuntimeError, match="slot not allowed"):
            svc.process_one()

    assert queue.depth() == 1, "a retryable failure must not destroy the work item"
    assert queue.inflight_depth() == 0, "the lease must be resolved, not leaked"
    assert queue.dead_letter_depth() == 0
    snapshot = queue.snapshot()
    assert snapshot["requeued_total"] == 1
    assert snapshot["delayed_depth"] == 1, "requeue must back off, not retry immediately"
    assert snapshot["due_depth"] == 0
    assert queue._items[0].item_id == item.item_id
    assert queue._items[0].attempts == 1


def test_daily_cap_keeps_work_item_in_queue() -> None:
    """The 242-hits/24h case: per-account daily cap is retryable, not fatal."""
    svc = TextNurtureService()
    queue = TextTaskQueue()
    queue.enqueue(dict(PAYLOAD))
    settings = _make_settings(max_per_account_per_day=1)
    svc._increment_daily_count(str(ACCOUNT["email"]), settings, amount=1)
    with _patched(svc, queue, settings):
        with pytest.raises(RuntimeError, match="daily account cap"):
            svc.process_one()

    assert queue.depth() == 1
    assert queue.dead_letter_depth() == 0


def test_upstream_failure_keeps_work_item_in_queue() -> None:
    """The 35-hits/24h case: an in-flight upstream 503 is retryable."""
    svc = TextNurtureService()
    queue = TextTaskQueue()
    queue.enqueue(dict(PAYLOAD))
    with _patched(svc, queue, _make_settings(), collect_result=RuntimeError("HTTP 503 upstream")):
        with pytest.raises(RuntimeError, match="503"):
            svc.process_one()

    assert queue.depth() == 1
    assert queue.snapshot()["requeued_total"] == 1
    assert "503" in queue._items[0].last_error


def test_successful_run_commits_the_lease() -> None:
    svc = TextNurtureService()
    queue = TextTaskQueue()
    queue.enqueue(dict(PAYLOAD))
    with _patched(svc, queue, _make_settings()):
        out = svc.process_one()

    assert out["ok"] is True
    assert queue.depth() == 0
    assert queue.inflight_depth() == 0, "a committed lease must not linger in flight"
    assert queue.dead_letter_depth() == 0


def test_repeated_retryable_failures_escalate_then_dead_letter() -> None:
    """A permanently-closed gate must back off and terminate, never loop forever."""
    svc = TextNurtureService()
    queue = TextTaskQueue()
    item = queue.enqueue(dict(PAYLOAD))
    delays: list[float] = []
    with _patched(svc, queue, _make_settings(), slot_open=False):
        for _ in range(MAX_ATTEMPTS):
            assert queue.depth() == 1
            with pytest.raises(RuntimeError):
                svc.process_one()
            if queue.depth():
                pending = queue._items[0]
                delays.append(pending.not_before - time.time())
                pending.not_before = 0.0  # simulate the backoff window elapsing

    assert queue.depth() == 0
    assert queue.inflight_depth() == 0
    assert queue.dead_letter_depth() == 1, "the attempt budget must terminate in a dead letter"

    entry = queue.dead_letters()[0]
    assert entry["item_id"] == item.item_id
    assert entry["attempts"] == MAX_ATTEMPTS
    assert entry["terminal"] is False
    assert "max_attempts_exhausted" in entry["reason"]
    assert "slot not allowed" in entry["reason"]
    assert "access_token" not in entry["payload"], "dead letters must not keep the token"
    assert entry["payload"]["has_access_token"] is True

    assert len(delays) == MAX_ATTEMPTS - 1
    assert all(d > 0 for d in delays), "every retry must wait"
    assert delays[0] < delays[1] < delays[2], "backoff must escalate"
    assert delays == sorted(delays)


def test_retry_delay_escalates_and_caps() -> None:
    previous = 0.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = retry_delay_sec(attempt, jitter=False)
        assert delay >= previous
        assert delay <= RETRY_BACKOFF_MAX_SEC
        previous = delay
    assert retry_delay_sec(1, jitter=False) < retry_delay_sec(3, jitter=False)
    assert retry_delay_sec(99, jitter=False) == pytest.approx(RETRY_BACKOFF_MAX_SEC)


def test_terminal_payload_dead_letters_without_requeue() -> None:
    svc = TextNurtureService()
    queue = TextTaskQueue()
    queue.enqueue({**PAYLOAD, "prompt": "please generate an image of a cat"})
    with _patched(svc, queue, _make_settings()):
        with pytest.raises(TerminalNurtureError):
            svc.process_one()

        assert queue.depth() == 0
        assert queue.inflight_depth() == 0
        assert queue.dead_letter_depth() == 1
        entry = queue.dead_letters()[0]
        assert entry["terminal"] is True
        assert entry["attempts"] == 1

        # It must not resurrect — no infinite requeue of a hopeless payload.
        with pytest.raises(RuntimeError, match="queue empty"):
            svc.process_one()

    assert queue.snapshot()["requeued_total"] == 0
    assert queue.dead_letter_depth() == 1


def test_malformed_payload_field_is_terminal() -> None:
    svc = TextNurtureService()
    queue = TextTaskQueue()
    queue.enqueue({**PAYLOAD, "email": 12345})
    with _patched(svc, queue, _make_settings()):
        with pytest.raises(TerminalNurtureError, match="must be a string"):
            svc.process_one()

    assert queue.depth() == 0
    assert queue.dead_letter_depth() == 1
    assert queue.snapshot()["requeued_total"] == 0


def test_process_one_reports_backoff_instead_of_leasing_early() -> None:
    svc = TextNurtureService()
    queue = TextTaskQueue()
    queue.enqueue(dict(PAYLOAD))
    with _patched(svc, queue, _make_settings(), slot_open=False):
        with pytest.raises(RuntimeError, match="slot not allowed"):
            svc.process_one()
        # Item is pending but not due: the caller is told to wait, not handed it.
        with pytest.raises(RuntimeError, match="backing off"):
            svc.process_one()

    assert queue.depth() == 1
    assert queue.due_depth() == 0


def test_lease_is_exclusive_and_honours_backoff() -> None:
    queue = TextTaskQueue()
    queue.enqueue({"prompt": "a"})

    leased = queue.lease()
    assert leased is not None
    assert queue.lease() is None, "a leased item must be invisible to other leasers"
    assert queue.depth() == 0
    assert queue.inflight_depth() == 1

    outcome = queue.requeue(leased.item_id, reason="upstream 503")
    assert outcome["dead_lettered"] is False
    assert outcome["retry_in_sec"] > 0
    assert queue.depth() == 1
    assert queue.due_depth() == 0
    assert queue.lease() is None

    later = time.time() + float(outcome["retry_in_sec"]) + 1.0
    assert queue.due_depth(now=later) == 1
    again = queue.lease(now=later)
    assert again is not None
    assert again.item_id == leased.item_id
    assert again.attempts == 1


def test_lease_skips_delayed_items_and_serves_fresh_ones() -> None:
    queue = TextTaskQueue()
    stale = queue.enqueue({"prompt": "stale"})
    leased = queue.lease()
    assert leased is not None and leased.item_id == stale.item_id
    queue.requeue(leased.item_id, reason="closed gate")

    fresh = queue.enqueue({"prompt": "fresh"})
    picked = queue.lease()
    assert picked is not None
    assert picked.item_id == fresh.item_id, "a backing-off item must not block the queue head"
    assert queue.depth() == 1


def test_expired_lease_is_reclaimed_not_lost() -> None:
    queue = TextTaskQueue()
    queue.enqueue({"prompt": "a"})
    leased = queue.lease()
    assert leased is not None
    assert queue.depth() == 0

    reclaimed = queue.reclaim_expired(now=leased.leased_at + LEASE_TIMEOUT_SEC + 1.0)
    assert reclaimed == 1
    assert queue.depth() == 1
    assert queue.inflight_depth() == 0
    assert queue.snapshot()["reclaimed_total"] == 1


def test_unknown_lease_resolution_is_reported_not_crashing() -> None:
    queue = TextTaskQueue()
    assert queue.commit("nope") is False
    assert queue.requeue("nope")["ok"] is False
    assert queue.dead_letter("nope")["ok"] is False


def test_loop_wait_escalates_on_consecutive_errors() -> None:
    svc = TextNurtureService()
    settings = _make_settings(poll_interval_sec=3.0)
    assert svc._loop_wait_sec(settings) == 3.0

    waits: list[float] = []
    for _ in range(6):
        svc._note_tick_error()
        waits.append(svc._loop_wait_sec(settings))

    assert waits[0] > 3.0, "a failing tick must not keep the fixed poll cadence"
    assert waits == sorted(waits)
    assert waits[-1] == LOOP_BACKOFF_MAX_SEC
    assert max(waits) <= LOOP_BACKOFF_MAX_SEC

    with patch("services.text_nurture_service.text_task_queue", TextTaskQueue()):
        svc._note_tick_ok(worked=False)
    assert svc._loop_wait_sec(settings) == 3.0
    assert svc.status()["consecutive_errors"] == 0


def test_queue_snapshot_exposes_durability_counters() -> None:
    queue = TextTaskQueue()
    snapshot = queue.snapshot()
    for key in (
        "depth",
        "oldest_age_sec",
        "due_depth",
        "delayed_depth",
        "inflight_depth",
        "dead_letter_depth",
        "dead_letter_total",
        "requeued_total",
        "max_attempts",
    ):
        assert key in snapshot, f"operators need {key} to see loss instead of guessing"


# --------------------------------------------------------------------------- #
# A1-6 — weekend blackout
# --------------------------------------------------------------------------- #


def test_business_hours_allows_weekend_daytime() -> None:
    weights = get_preset("business_hours")["weights"]
    assert slot_allowed(weights, now_utc=SAT_14_SGT), "Saturday daytime must not be a blackout"
    assert slot_allowed(weights, now_utc=SUN_11_SGT), "Sunday daytime must not be a blackout"


def test_business_hours_still_blocks_deep_night() -> None:
    weights = get_preset("business_hours")["weights"]
    assert not slot_allowed(weights, now_utc=SAT_03_SGT)
    assert not slot_allowed(weights, now_utc=SUN_03_SGT)


def test_business_hours_weekend_weight_below_weekday_office() -> None:
    weights = get_preset("business_hours")["weights"]
    weekday = current_slot_weight(weights, now_utc=MON_10_SGT)
    saturday = current_slot_weight(weights, now_utc=SAT_14_SGT)
    sunday = current_slot_weight(weights, now_utc=SUN_11_SGT)
    assert weekday >= SLOT_ALLOW_THRESHOLD
    assert SLOT_ALLOW_THRESHOLD <= saturday < weekday, "weekend must be reduced, not equal to weekdays"
    assert SLOT_ALLOW_THRESHOLD <= sunday < weekday


def test_extended_business_weekend_reduced_not_blacked_out() -> None:
    weights = get_preset("extended_business")["weights"]
    weekday = current_slot_weight(weights, now_utc=MON_10_SGT)
    saturday = current_slot_weight(weights, now_utc=SAT_14_SGT)
    assert slot_allowed(weights, now_utc=SAT_14_SGT)
    assert slot_allowed(weights, now_utc=SUN_11_SGT)
    assert saturday < weekday
    assert not slot_allowed(weights, now_utc=SUN_03_SGT)


def test_minimal_and_sg_remote_can_pass_the_gate() -> None:
    minimal = get_preset("minimal")["weights"]
    assert slot_allowed(minimal, now_utc=MON_10_SGT), "minimal means low frequency, not never"
    assert slot_allowed(minimal, now_utc=SUN_11_SGT)

    sg_remote = get_preset("sg_remote")["weights"]
    assert slot_allowed(sg_remote, now_utc=MON_10_SGT), "weekday extended hours are its peak"
    assert slot_allowed(sg_remote, now_utc=SAT_14_SGT), "a floor equal to the threshold means allowed"


def test_gate_treats_weight_equal_to_threshold_as_allowed() -> None:
    at_threshold = [[SLOT_ALLOW_THRESHOLD for _ in range(SLOTS_PER_DAY)] for _ in range(7)]
    below = [[SLOT_ALLOW_THRESHOLD - 0.01 for _ in range(SLOTS_PER_DAY)] for _ in range(7)]
    assert slot_allowed(at_threshold, now_utc=MON_10_SGT)
    assert not slot_allowed(below, now_utc=MON_10_SGT)


def test_no_preset_is_a_permanent_blackout() -> None:
    for preset in list_presets():
        weights = get_preset(str(preset["id"]))["weights"]
        peak = max(value for row in weights for value in row)
        assert peak >= SLOT_ALLOW_THRESHOLD, f"{preset['id']} can never pass the gate at any time"


def test_weekend_rest_presets_keep_their_blackout_intent() -> None:
    for preset_id in ("weekday_only", "rest_weekend"):
        weights = get_preset(preset_id)["weights"]
        assert slot_allowed(weights, now_utc=MON_10_SGT)
        assert not slot_allowed(weights, now_utc=SAT_14_SGT), f"{preset_id} must stay weekday-only"
        assert not slot_allowed(weights, now_utc=SUN_11_SGT)

    rest_sunday = get_preset("rest_day_sun")["weights"]
    assert slot_allowed(rest_sunday, now_utc=SAT_14_SGT)
    assert not slot_allowed(rest_sunday, now_utc=SUN_11_SGT)


def test_staggered_presets_stay_complementary() -> None:
    a = get_preset("staggered_a")["weights"]
    b = get_preset("staggered_b")["weights"]
    for day in range(7):
        for slot in range(SLOTS_PER_DAY):
            open_a = a[day][slot] >= SLOT_ALLOW_THRESHOLD
            open_b = b[day][slot] >= SLOT_ALLOW_THRESHOLD
            assert open_a != open_b, f"stagger broken at day={day} slot={slot}"


def test_default_preset_covers_the_weekend() -> None:
    """business_hours is default_preset_id — unbound accounts must not go dark all weekend."""
    weights = resolve_binding_matrix("", default_preset_id="business_hours")
    assert slot_allowed(weights, now_utc=SAT_14_SGT)
    assert slot_allowed(weights, now_utc=SUN_11_SGT)


def test_hash_fallback_preset_is_weekend_safe() -> None:
    """A1-6: hash fallback must not pick weekday_only / rest_weekend presets."""
    weights = resolve_binding_matrix("unconfigured-binding-hash-key-12345")
    assert slot_allowed(weights, now_utc=SAT_14_SGT)
    assert slot_allowed(weights, now_utc=SUN_11_SGT)
