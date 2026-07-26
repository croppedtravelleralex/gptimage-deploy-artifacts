"""TEXT-NURTURE: real text queue driven by explicit prompts — never fake chat.

Default OFF. When enabled, a background worker drains Qtext with humanlike
intervals and independent text conversation ids (PROTO-ALIGN persist path).
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.account_service import account_service
from services.config import config
from services.humanlike_scheduler import resolve_tz_name
from services.ip_nurture_schedule import resolve_binding_matrix, slot_allowed
from services.log_service import log_llm_ops
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import ConversationRequest, collect_text
from services.text_task_queue import TextQueueItem, text_task_queue
from utils.log import logger

# A1-5 — worker loop backoff. The old loop always waited exactly poll_interval_sec
# with no consecutive-failure counter, so a permanently failing tick hammered the
# upstream / the log at a fixed 3s cadence.
LOOP_BACKOFF_FACTOR = 2.0
LOOP_BACKOFF_MAX_SEC = 120.0
LOOP_BACKOFF_MAX_EXP = 8

# A1-5 — retryable vs terminal. Terminal means "retrying this exact payload can
# never succeed" (malformed payload / forbidden prompt / caller bug), so it goes
# straight to the dead-letter ring instead of consuming the retry budget.
# Everything else (closed schedule gate, daily cap, upstream 503/401/500,
# transport errors) is retryable and gets requeued with backoff.
TERMINAL_ERROR_TYPES: tuple[type[BaseException], ...] = (
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
)

FORBIDDEN_PROMPT_MARKERS: tuple[str, ...] = ("picture_v2", "generate an image")
PAYLOAD_STR_FIELDS: tuple[str, ...] = ("prompt", "access_token", "email", "source", "model")


class TerminalNurtureError(ValueError):
    """Payload can never succeed as-is — retire it, do not retry."""


def is_terminal_nurture_error(exc: BaseException) -> bool:
    return isinstance(exc, TERMINAL_ERROR_TYPES)


# Short, mundane prompts — real lightweight nurture, not image-tied spam.
DEFAULT_NURTURE_PROMPTS: tuple[str, ...] = (
    "Summarize in one sentence what a sticky proxy is used for.",
    "Give three short tips for writing clearer commit messages.",
    "What is the difference between UTC and local time zones in one paragraph?",
    "Explain HTTP 429 in plain language for an ops engineer.",
    "List two safe ways to rotate API credentials without downtime.",
)

DEFAULT_SESSION_FOLLOW_UP_PROMPTS: tuple[str, ...] = (
    "Can you add one more practical detail?",
    "Give a shorter summary in one sentence.",
    "What would you do differently next time?",
)


def _settings() -> dict[str, Any]:
    raw = config.get_text_nurture_settings() if hasattr(config, "get_text_nurture_settings") else {}
    if not isinstance(raw, dict):
        raw = {}
    prompts = raw.get("prompts")
    if isinstance(prompts, list) and prompts:
        prompt_list = [str(p).strip() for p in prompts if str(p).strip()]
    else:
        prompt_list = list(DEFAULT_NURTURE_PROMPTS)
    follow_ups = raw.get("session_follow_up_prompts")
    if isinstance(follow_ups, list) and follow_ups:
        follow_up_list = [str(p).strip() for p in follow_ups if str(p).strip()]
    else:
        follow_up_list = list(DEFAULT_SESSION_FOLLOW_UP_PROMPTS)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "worker_enabled": bool(raw.get("worker_enabled", True)),
        "poll_interval_sec": max(2.0, float(raw.get("poll_interval_sec", 5.0) or 5.0)),
        "max_per_hour": max(0, int(raw.get("max_per_hour", 0) or 0)),
        "max_per_account_per_day": max(1, int(raw.get("max_per_account_per_day", 6) or 6)),
        "daily_reset_tz": str(raw.get("daily_reset_tz") or "Asia/Singapore").strip() or "Asia/Singapore",
        "turns_per_session": max(1, int(raw.get("turns_per_session", 2) or 2)),
        "turn_gap_sec": max(0.0, float(raw.get("turn_gap_sec", 8.0) or 8.0)),
        "require_persist_history": bool(raw.get("require_persist_history", True)),
        "auto_enqueue": bool(raw.get("auto_enqueue", False)),
        "auto_enqueue_every_sec": max(60.0, float(raw.get("auto_enqueue_every_sec", 600.0) or 600.0)),
        "auto_enqueue_rotate_accounts": bool(raw.get("auto_enqueue_rotate_accounts", True)),
        "count_manual_toward_daily_limit": bool(raw.get("count_manual_toward_daily_limit", True)),
        "prompts": prompt_list,
        "session_follow_up_prompts": follow_up_list,
        "model": str(raw.get("model") or "auto").strip() or "auto",
    }


class TextNurtureService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._completed_hour: str = ""
        self._completed_in_hour = 0
        self._daily_counts: dict[tuple[str, str], int] = {}
        self._rotate_index = 0
        self._last_auto_enqueue_at = 0.0
        self._last_error = ""
        self._last_ok_at = 0.0
        self._running = False
        self._consecutive_errors = 0

    def _daily_key(self, settings: dict[str, Any]) -> str:
        tz = ZoneInfo(resolve_tz_name(str(settings.get("daily_reset_tz") or "Asia/Singapore")))
        return datetime.now(timezone.utc).astimezone(tz).date().isoformat()

    def _account_daily_count(self, email: str, settings: dict[str, Any]) -> int:
        key = (str(email or "").strip().lower(), self._daily_key(settings))
        with self._lock:
            return int(self._daily_counts.get(key) or 0)

    def _increment_daily_count(self, email: str, settings: dict[str, Any], *, amount: int = 1) -> None:
        norm = str(email or "").strip().lower()
        if not norm:
            return
        day = self._daily_key(settings)
        with self._lock:
            key = (norm, day)
            self._daily_counts[key] = int(self._daily_counts.get(key) or 0) + max(1, int(amount))

    def _at_daily_cap(self, email: str, settings: dict[str, Any]) -> bool:
        norm = str(email or "").strip().lower()
        if not norm:
            return False
        limit = int(settings.get("max_per_account_per_day") or 0)
        if limit <= 0:
            return False
        return self._account_daily_count(norm, settings) >= limit

    def _accounts_at_cap(self, settings: dict[str, Any]) -> int:
        limit = int(settings.get("max_per_account_per_day") or 0)
        if limit <= 0:
            return 0
        day = self._daily_key(settings)
        with self._lock:
            return sum(1 for (_, d), count in self._daily_counts.items() if d == day and int(count) >= limit)

    def _today_completed_total(self, settings: dict[str, Any]) -> int:
        day = self._daily_key(settings)
        with self._lock:
            return sum(int(count) for (_, d), count in self._daily_counts.items() if d == day)

    def _binding_key_for_account(self, account: dict[str, Any]) -> str:
        binding = str(account.get("proxy_binding_hash") or "").strip()
        if binding:
            return binding
        return str(account.get("proxy_egress_ip") or account.get("proxy_egress_hash") or "").strip()

    def _slot_allowed(self, account: dict[str, Any], settings: dict[str, Any]) -> bool:
        binding_key = self._binding_key_for_account(account)
        matrix = resolve_binding_matrix(binding_key)
        return slot_allowed(matrix, tz_name=str(settings.get("daily_reset_tz") or "Asia/Singapore"))

    def status(self) -> dict[str, Any]:
        settings = _settings()
        with self._lock:
            day = self._daily_key(settings)
            limit = int(settings.get("max_per_account_per_day") or 0)
            today_total = sum(int(count) for (_, d), count in self._daily_counts.items() if d == day)
            at_cap = (
                sum(1 for (_, d), count in self._daily_counts.items() if d == day and int(count) >= limit)
                if limit > 0
                else 0
            )
            return {
                "enabled": settings["enabled"],
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "running": self._running,
                "queue": text_task_queue.snapshot(),
                "completed_in_hour": self._completed_in_hour,
                "max_per_hour": settings["max_per_hour"],
                "max_per_account_per_day": settings["max_per_account_per_day"],
                "turns_per_session": settings["turns_per_session"],
                "today_completed_total": today_total,
                "accounts_at_cap": at_cap,
                "last_error": self._last_error,
                "last_ok_at": self._last_ok_at or None,
                "consecutive_errors": self._consecutive_errors,
                "require_persist_history": settings["require_persist_history"],
                "auto_enqueue": settings["auto_enqueue"],
                "prompt_count": len(settings["prompts"]),
            }

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="text-nurture", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        data = dict(getattr(config, "data", {}) or {})
        nurture = dict(data.get("text_nurture") or {}) if isinstance(data.get("text_nurture"), dict) else {}
        nurture["enabled"] = bool(enabled)
        data["text_nurture"] = nurture
        config.update({"text_nurture": nurture})
        return self.status()

    def enqueue(
        self,
        *,
        prompt: str = "",
        access_token: str = "",
        email: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        settings = _settings()
        text = str(prompt or "").strip()
        if not text:
            text = random.choice(settings["prompts"])
        lowered = text.lower()
        if "picture_v2" in lowered or "generate an image" in lowered:
            raise ValueError("nurture prompts must not request image generation")
        payload = {
            "prompt": text,
            "access_token": str(access_token or "").strip(),
            "email": str(email or "").strip(),
            "source": str(source or "manual").strip() or "manual",
            "model": settings["model"],
        }
        item = text_task_queue.enqueue(payload)
        return {"item_id": item.item_id, "queue": text_task_queue.snapshot(), "prompt_chars": len(text)}

    def _hour_key(self) -> str:
        return time.strftime("%Y%m%d%H", time.gmtime())

    def _budget_ok(self, settings: dict[str, Any]) -> bool:
        key = self._hour_key()
        with self._lock:
            if self._completed_hour != key:
                self._completed_hour = key
                self._completed_in_hour = 0
            limit = int(settings["max_per_hour"] or 0)
            if limit <= 0:
                return True
            return self._completed_in_hour < limit

    def _mark_completed(self, *, turns: int = 1) -> None:
        key = self._hour_key()
        with self._lock:
            if self._completed_hour != key:
                self._completed_hour = key
                self._completed_in_hour = 0
            self._completed_in_hour += max(1, int(turns))
            self._last_ok_at = time.time()
            self._last_error = ""

    def _maybe_auto_enqueue(self, settings: dict[str, Any]) -> None:
        if not settings["auto_enqueue"] or not settings["enabled"]:
            return
        now = time.time()
        if now - self._last_auto_enqueue_at < float(settings["auto_enqueue_every_sec"]):
            return
        # Pending includes items sitting out a retry backoff, and in-flight leases
        # are real work too — don't pile duplicates on top of either.
        if text_task_queue.depth() > 0 or text_task_queue.inflight_depth() > 0:
            return
        if not self._budget_ok(settings):
            return
        self._last_auto_enqueue_at = now
        try:
            email = ""
            if settings["auto_enqueue_rotate_accounts"]:
                picked = self._pick_nurture_token(settings, excluded_tokens=set())
                if picked:
                    account = account_service.get_account(picked) or {}
                    email = str(account.get("email") or "")
            self.enqueue(source="auto", email=email)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:240]

    def _eligible_accounts(
        self,
        settings: dict[str, Any],
        *,
        excluded_tokens: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        excluded = set(excluded_tokens or set())
        out: list[dict[str, Any]] = []
        for account in account_service.list_accounts():
            if not isinstance(account, dict):
                continue
            token = str(account.get("access_token") or "")
            if not token or token in excluded:
                continue
            if account.get("status") in {"禁用", "异常"}:
                continue
            email = str(account.get("email") or "").strip().lower()
            if not email:
                continue
            if self._at_daily_cap(email, settings):
                continue
            if not self._slot_allowed(account, settings):
                continue
            if settings["require_persist_history"]:
                persist = bool(account.get("chat_persist_history")) or bool(
                    getattr(config, "text_chat_persist_history", False)
                )
                if not persist:
                    continue
            out.append(account)
        return out

    def _pick_nurture_token(
        self,
        settings: dict[str, Any],
        *,
        excluded_tokens: set[str] | None = None,
        preferred_email: str = "",
    ) -> str:
        prefer = str(preferred_email or "").strip().lower()
        if prefer:
            for account in self._eligible_accounts(settings, excluded_tokens=excluded_tokens):
                if str(account.get("email") or "").strip().lower() == prefer:
                    return str(account.get("access_token") or "")
            return ""
        candidates = self._eligible_accounts(settings, excluded_tokens=excluded_tokens)
        if not candidates:
            return account_service.get_text_access_token(excluded_tokens=excluded_tokens)
        with self._lock:
            start = self._rotate_index % len(candidates)
            self._rotate_index += 1
        account = candidates[start]
        token = str(account.get("access_token") or "")
        return account_service.refresh_access_token(token, event="text_nurture_pick") or token

    def _resolve_token(self, payload: dict[str, Any], settings: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        token = str(payload.get("access_token") or "").strip()
        email = str(payload.get("email") or "").strip().lower()
        account: dict[str, Any] = {}
        if token:
            account = account_service.get_account(token) or {}
        elif email:
            for item in account_service.list_accounts():
                if str(item.get("email") or "").strip().lower() == email:
                    account = item
                    token = str(item.get("access_token") or "")
                    break
        else:
            token = self._pick_nurture_token(settings)
            account = account_service.get_account(token) or {} if token else {}
        if not token:
            raise RuntimeError("no available text account for nurture")
        norm_email = str(account.get("email") or email or "").strip().lower()
        if norm_email and self._at_daily_cap(norm_email, settings):
            raise RuntimeError("text_nurture daily account cap reached")
        if account and not self._slot_allowed(account, settings):
            raise RuntimeError("text_nurture slot not allowed for account binding schedule")
        if settings["require_persist_history"]:
            persist = bool(account.get("chat_persist_history")) or bool(
                getattr(config, "text_chat_persist_history", False)
            )
            if not persist:
                if str(payload.get("source") or "") in {"accounts_ui", "ops_ui_force"}:
                    return token, account
                raise RuntimeError(
                    "nurture requires chat_persist_history on account or text_chat_persist_history config"
                )
        return token, account

    def _should_count_toward_daily(self, payload: dict[str, Any], settings: dict[str, Any]) -> bool:
        source = str(payload.get("source") or "").strip().lower()
        if source in {"accounts_ui", "ops_ui_force", "manual"} and not settings["count_manual_toward_daily_limit"]:
            return False
        return True

    def _turn_prompts(self, payload: dict[str, Any], settings: dict[str, Any]) -> list[str]:
        base = str(payload.get("prompt") or "").strip()
        if not base:
            base = random.choice(settings["prompts"])
        turns = max(1, int(settings["turns_per_session"] or 1))
        prompts = [base]
        follow_ups = list(settings.get("session_follow_up_prompts") or [])
        while len(prompts) < turns:
            prompts.append(random.choice(follow_ups) if follow_ups else base)
        return prompts[:turns]

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        """Terminal-error gate: reject payloads no retry could ever rescue."""
        for key in PAYLOAD_STR_FIELDS:
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise TerminalNurtureError(f"nurture payload field {key} must be a string")
        lowered = str(payload.get("prompt") or "").lower()
        if any(marker in lowered for marker in FORBIDDEN_PROMPT_MARKERS):
            raise TerminalNurtureError("nurture prompts must not request image generation")

    def process_one(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = _settings()
        data = dict(payload or {})
        force_source = str(data.get("source") or "")
        if not settings["enabled"] and force_source not in {"accounts_ui", "ops_ui_force"}:
            raise RuntimeError("text_nurture is disabled（请到 运维→养号 开启，或用号池「立即对话」强制执行一条）")
        if not self._budget_ok(settings):
            raise RuntimeError("text_nurture hourly budget exhausted")
        if not data.get("prompt") and not data.get("email") and not data.get("access_token"):
            # Queue-driven path: lease instead of destroying, so a failed
            # validation returns the work item to the queue (A1-5).
            return self._process_leased(settings)
        if not data.get("prompt"):
            try:
                data["prompt"] = random.choice(settings["prompts"])
            except Exception:
                data["prompt"] = "hi"
        # Directed call (API / accounts UI): nothing was queued, so the error
        # surfaces to the caller instead of being requeued.
        return self._run_payload(data, settings)

    def _process_leased(self, settings: dict[str, Any]) -> dict[str, Any]:
        leased = text_task_queue.lease()
        if leased is None:
            snapshot = text_task_queue.snapshot()
            delayed = int(snapshot.get("delayed_depth") or 0)
            if delayed > 0:
                raise RuntimeError(f"text nurture queue backing off (delayed={delayed})")
            raise RuntimeError("text nurture queue empty")
        try:
            result = self._run_payload(dict(leased.payload), settings)
        except BaseException as exc:
            self._retire_lease(leased, exc)
            raise
        text_task_queue.commit(leased.item_id)
        return result

    def _retire_lease(self, leased: TextQueueItem, exc: BaseException) -> dict[str, Any]:
        terminal = is_terminal_nurture_error(exc)
        reason = f"{type(exc).__name__}: {exc}"[:240]
        if terminal:
            outcome = text_task_queue.dead_letter(leased.item_id, reason=reason, terminal=True)
        else:
            outcome = text_task_queue.requeue(leased.item_id, reason=reason)
        # The lease is already resolved above; emitting the audit line must never
        # be able to mask the real failure that is about to be re-raised.
        try:
            logger.warning(
                {
                    "event": "text_nurture_item_retired",
                    "item_id": leased.item_id,
                    "terminal": terminal,
                    "attempts": outcome.get("attempts"),
                    "dead_lettered": bool(outcome.get("dead_lettered")),
                    "retry_in_sec": outcome.get("retry_in_sec"),
                    "error": reason[:200],
                }
            )
        except Exception:
            pass
        return outcome

    def _run_payload(self, data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        self._validate_payload(data)
        token, account = self._resolve_token(data, settings)
        email = str(account.get("email") or data.get("email") or "").strip().lower()
        prompts = self._turn_prompts(data, settings)
        model = str(data.get("model") or settings["model"]).strip() or "auto"
        started = time.monotonic()
        backend = OpenAIBackendAPI(access_token=token)
        total_chars = 0
        completed_turns = 0
        try:
            for idx, prompt in enumerate(prompts):
                if idx > 0:
                    if float(settings["turn_gap_sec"]) > 0:
                        time.sleep(float(settings["turn_gap_sec"]))
                    # collect_text/stream_text_deltas close the backend after each turn.
                    backend = OpenAIBackendAPI(access_token=token)
                if isinstance(backend.account, dict):
                    backend.account = {
                        **backend.account,
                        "chat_persist_history": True,
                        "chat_reuse_conversation": True,
                    }
                text = collect_text(
                    backend,
                    ConversationRequest(model=model, prompt=prompt, messages=[{"role": "user", "content": prompt}]),
                )
                total_chars += len(text or "")
                completed_turns += 1
            self._mark_completed(turns=completed_turns)
            if email and self._should_count_toward_daily(data, settings):
                self._increment_daily_count(email, settings, amount=completed_turns)
            log_llm_ops(
                source="L0",
                kind="nurture",
                access_token=token,
                latency_ms=int((time.monotonic() - started) * 1000),
                outcome="ok",
                prompt_shape={
                    "chars": len(prompts[0]),
                    "has_images": False,
                    "model": model,
                    "nurture": True,
                    "turns": completed_turns,
                },
            )
            return {
                "ok": True,
                "chars_out": total_chars,
                "prompt_chars": len(prompts[0]),
                "turns": completed_turns,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:240]
            log_llm_ops(
                source="L0",
                kind="nurture",
                access_token=token,
                latency_ms=int((time.monotonic() - started) * 1000),
                outcome="error",
                outcome_code=type(exc).__name__,
                prompt_shape={
                    "chars": len(prompts[0]) if prompts else 0,
                    "has_images": False,
                    "model": model,
                    "nurture": True,
                    "turns": completed_turns,
                },
            )
            raise
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _note_tick_ok(self, *, worked: bool) -> None:
        """Reset the failure streak on real progress, or when the queue went quiet."""
        quiet = text_task_queue.depth() == 0 and text_task_queue.inflight_depth() == 0
        if not worked and not quiet:
            return
        with self._lock:
            self._consecutive_errors = 0

    def _note_tick_error(self) -> int:
        with self._lock:
            self._consecutive_errors += 1
            return self._consecutive_errors

    def _loop_wait_sec(self, settings: dict[str, Any]) -> float:
        base = max(2.0, float(settings.get("poll_interval_sec") or 3.0))
        with self._lock:
            streak = int(self._consecutive_errors)
        if streak <= 0:
            return base
        wait = base * (LOOP_BACKOFF_FACTOR ** min(streak, LOOP_BACKOFF_MAX_EXP))
        return float(min(wait, LOOP_BACKOFF_MAX_SEC))

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = _settings()
            self._running = bool(settings["enabled"] and settings["worker_enabled"])
            worked = False
            try:
                self._maybe_auto_enqueue(settings)
                # due_depth skips items still sitting out their retry backoff.
                if self._running and self._budget_ok(settings) and text_task_queue.due_depth() > 0:
                    worked = True
                    self.process_one()
                self._note_tick_ok(worked=worked)
            except Exception as exc:
                streak = self._note_tick_error()
                logger.warning(
                    {
                        "event": "text_nurture_tick_error",
                        "error": str(exc)[:200],
                        "consecutive_errors": streak,
                        "queue": text_task_queue.snapshot(),
                    }
                )
            self._stop.wait(timeout=self._loop_wait_sec(settings))
        self._running = False


text_nurture_service = TextNurtureService()
