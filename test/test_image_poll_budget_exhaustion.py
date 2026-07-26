"""Poll GET budget vs wall budget (audit 28 §B6 / fix A4-4).

The hidden default of 24 conversation GETs cut every poll at roughly 82~120s of
wall clock, so the configured 300s (edit) and 360s (multi-reference) budgets never
bound anything, and the timeout error blamed `image_poll_timeout_secs` — a key the
queue modes never read.

No real sleeping: `time.sleep` is stubbed and the wall deadline is driven by a fake
`time.time` where a deadline needs to be crossed.
"""

from __future__ import annotations

from unittest import mock

import pytest

import services.image_poll_budget as budget_module
from services.config import config
from services.image_poll_budget import ImagePollBudget, derive_max_conversation_gets
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI


class FakeBackend(OpenAIBackendAPI):
    """Minimal poll-loop host: conversation GETs always come back empty."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancel_event = None

    def _get_conversation(self, conversation_id: str) -> dict:
        self.calls += 1
        return {"mapping": {}}

    def _query_backend_tasks(self, conversation_id: str = "", task_id: str = "", timeout_secs: float = 30.0):
        return []

    def _extract_image_tool_records(self, conversation):
        return []

    def _find_content_policy_error_in_conversation(self, conversation):
        return ""


def _poll_config(**overrides) -> dict[str, object]:
    base = {
        "image_poll_initial_wait_secs": 0,
        "image_poll_interval_secs": 3.0,
        "image_poll_max_tasks_gets": 0,
        "image_poll_tasks_every_n_attempts": 99,
        "image_settle_enabled": False,
        "image_check_before_hit_enabled": False,
    }
    base.update(overrides)
    return base


class VirtualWall:
    """Advances `time.time()` by `interval` per sleep so no test really waits."""

    def __init__(self, interval: float) -> None:
        self.now = 1_700_000_000.0
        self.interval = interval
        self.sleeps = 0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += max(float(seconds), self.interval)


# --- derivation -------------------------------------------------------------


def test_derived_budget_covers_the_configured_wall():
    # 360s / 3s = 120 nominal attempts; the cap must be well above that so the
    # wall is what binds, not the GET count.
    assert derive_max_conversation_gets(timeout_secs=360.0, poll_interval_secs=3.0) == 248
    assert derive_max_conversation_gets(timeout_secs=300.0, poll_interval_secs=3.0) == 208
    assert derive_max_conversation_gets(timeout_secs=120.0, poll_interval_secs=3.0) == 88


def test_derived_budget_tolerates_absent_or_zero_interval():
    assert derive_max_conversation_gets(timeout_secs=360.0, poll_interval_secs=None) == 248
    assert derive_max_conversation_gets(timeout_secs=360.0, poll_interval_secs=0) == 248
    assert derive_max_conversation_gets(timeout_secs=360.0, poll_interval_secs=-5) == 248
    # Sub-floor intervals clamp at 0.5s rather than exploding the cap.
    assert derive_max_conversation_gets(timeout_secs=10.0, poll_interval_secs=0.01) == 48


def test_missing_config_key_derives_and_explicit_key_is_kept():
    # patch.dict restores deleted keys on exit, so popping inside is safe.
    with mock.patch.dict(config.data, {}):
        config.data.pop("image_poll_max_upstream_gets", None)
        assert config.image_poll_max_upstream_gets_explicit is None
        assert OpenAIBackendAPI._resolve_poll_max_upstream_gets() is None
    with mock.patch.dict(config.data, {"image_poll_max_upstream_gets": 24}):
        assert config.image_poll_max_upstream_gets_explicit == 24
        assert OpenAIBackendAPI._resolve_poll_max_upstream_gets() == 24


# --- A4-4 regression --------------------------------------------------------


def test_360s_poll_is_not_cut_short_at_24_gets():
    """Regression: with a 360s wall and no explicit key the loop must not stop at 24.

    Against the old code `backend.calls` was exactly 24 and the exhaustion reason
    was `conversation_get_budget` with ~270s of wall budget still unspent.
    """
    wall = VirtualWall(interval=3.0)
    with (
        mock.patch.dict(config.data, _poll_config()),
        mock.patch.object(budget_module.time, "time", wall.time),
        mock.patch("services.openai_backend_api.time.sleep", wall.sleep),
        pytest.raises(ImagePollTimeoutError) as caught,
    ):
        config.data.pop("image_poll_max_upstream_gets", None)
        FakeBackend()._poll_image_results("conv-360", timeout_secs=360.0)

    snapshot = getattr(caught.value, "poll_budget", {})
    assert snapshot["get_budget_source"] == "derived_from_wall"
    assert snapshot["max_conversation_gets"] == 248
    assert snapshot["conversation_gets"] > 24
    # 360s wall / 3s interval ≈ 120 GETs before the wall deadline lands.
    assert snapshot["conversation_gets"] >= 100
    assert snapshot["exhausted_reason"] == "wall_time"


def test_explicit_get_budget_is_still_honoured():
    wall = VirtualWall(interval=3.0)
    backend = FakeBackend()
    with (
        mock.patch.dict(config.data, _poll_config(image_poll_max_upstream_gets=24)),
        mock.patch.object(budget_module.time, "time", wall.time),
        mock.patch("services.openai_backend_api.time.sleep", wall.sleep),
        pytest.raises(ImagePollTimeoutError) as caught,
    ):
        backend._poll_image_results("conv-explicit", timeout_secs=360.0)

    assert backend.calls == 24
    snapshot = getattr(caught.value, "poll_budget", {})
    assert snapshot["get_budget_source"] == "explicit"
    assert snapshot["max_conversation_gets"] == 24
    assert snapshot["exhausted_reason"] == "conversation_get_budget"


# --- error text -------------------------------------------------------------


def test_wall_exhaustion_message_reports_elapsed_reason_and_mode_key():
    wall = VirtualWall(interval=3.0)
    timeout = float(config.image_multi_reference_poll_timeout_secs)
    with (
        mock.patch.dict(config.data, _poll_config()),
        mock.patch.object(budget_module.time, "time", wall.time),
        mock.patch("services.openai_backend_api.time.sleep", wall.sleep),
        pytest.raises(ImagePollTimeoutError) as caught,
    ):
        config.data.pop("image_poll_max_upstream_gets", None)
        FakeBackend()._poll_image_results("conv-msg", timeout_secs=timeout)

    message = str(caught.value)
    snapshot = getattr(caught.value, "poll_budget", {})
    assert snapshot["mode"] == "multi_reference"
    assert "image_task_queue.multi_reference_poll_timeout_secs" in message
    # The dead top-level key must not be advertised any more.
    assert "调大 config.json 的 image_poll_timeout_secs" not in message
    assert "wall_time" in message
    assert f"实际已等待 {snapshot['elapsed_wall_secs']:.1f} 秒" in message
    # image_task_service._looks_like_timeout / resume_poll match on this substring.
    assert "超时" in message


def test_get_budget_exhaustion_message_names_the_get_knob():
    budget = ImagePollBudget.create(
        timeout_secs=360.0,
        max_conversation_gets=2,
        max_tasks_gets=0,
        tasks_every_n_attempts=4,
        poll_interval_secs=3.0,
        mode="multi_reference",
        timeout_config_key="image_task_queue.multi_reference_poll_timeout_secs",
    )
    assert budget.begin_attempt() is True
    budget.record_conversation_get()
    assert budget.begin_attempt() is True
    budget.record_conversation_get()
    assert budget.begin_attempt() is False

    message = budget.exhaustion_message()
    assert budget.exhausted_reason == "conversation_get_budget"
    assert "conversation_get_budget" in message
    assert "image_poll_max_upstream_gets" in message
    assert "2/2" in message
    # Wall budget was nowhere near exhausted, so it must not be blamed.
    assert "image_task_queue.multi_reference_poll_timeout_secs" not in message
    assert "超时" in message


def test_wall_and_get_exhaustion_stay_distinguishable():
    wall_budget = ImagePollBudget.create(
        timeout_secs=0.1,
        max_conversation_gets=None,
        max_tasks_gets=0,
        tasks_every_n_attempts=4,
        poll_interval_secs=3.0,
        mode="edit",
        timeout_config_key="image_task_queue.edit_poll_timeout_secs",
    )
    with mock.patch.object(
        budget_module.time, "time", lambda: wall_budget.wall_deadline + 1.0
    ):
        assert wall_budget.begin_attempt() is False
        assert wall_budget.exhausted_reason == "wall_time"
        assert "image_task_queue.edit_poll_timeout_secs" in wall_budget.exhaustion_message()

    get_budget = ImagePollBudget.create(
        timeout_secs=360.0,
        max_conversation_gets=1,
        max_tasks_gets=0,
        tasks_every_n_attempts=4,
    )
    assert get_budget.begin_attempt() is True
    get_budget.record_conversation_get()
    assert get_budget.begin_attempt() is False
    assert get_budget.exhausted_reason == "conversation_get_budget"


def test_reason_falls_back_to_retry_exhausted_when_loop_breaks_early():
    """A loop that `break`s on upstream errors spent no budget — say so."""
    budget = ImagePollBudget.create(
        timeout_secs=360.0,
        max_conversation_gets=None,
        max_tasks_gets=0,
        tasks_every_n_attempts=4,
        poll_interval_secs=3.0,
        mode="generation",
        timeout_config_key="image_task_queue.generation_poll_timeout_secs",
    )
    assert budget.begin_attempt() is True
    budget.record_conversation_get()
    assert budget.exhausted_reason == ""
    assert budget.effective_exhausted_reason() == "retry_exhausted"
    message = budget.exhaustion_message()
    assert "retry_exhausted" in message
    assert "image_poll_retry" in message


# --- mode / key mapping -----------------------------------------------------


@pytest.mark.parametrize(
    "timeout_attr, expected_mode, expected_key",
    [
        ("image_multi_reference_poll_timeout_secs", "multi_reference", "image_task_queue.multi_reference_poll_timeout_secs"),
        ("image_edit_poll_timeout_secs", "edit", "image_task_queue.edit_poll_timeout_secs"),
        ("image_generation_poll_timeout_secs", "generation", "image_task_queue.generation_poll_timeout_secs"),
    ],
)
def test_timeout_maps_to_the_key_that_actually_wins(timeout_attr, expected_mode, expected_key):
    timeout = float(getattr(config, timeout_attr))
    mode, key = OpenAIBackendAPI._resolve_poll_timeout_config_key(timeout)
    assert (mode, key) == (expected_mode, expected_key)


def test_sub_generation_timeout_maps_to_base_key():
    mode, key = OpenAIBackendAPI._resolve_poll_timeout_config_key(1.0)
    assert mode == "base"
    assert key == "image_poll_timeout_secs"
