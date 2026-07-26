from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any

from services.config import config

EPSILON_DEFAULT = 0.05
EXPLORE_BONUS = 15.0


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text[:26], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _aci_quota_eligible(account: dict[str, Any]) -> bool:
    try:
        from services.account_service import AccountService

        return AccountService._has_confirmed_image_quota(account)
    except Exception:
        return int(account.get("quota") or 0) > 0


def aci_score(account: dict[str, Any]) -> float:
    """Account Conversation Index 0–100 for sS scheduling."""
    score = 50.0
    try:
        from services.account_warmup_service import account_warmup_service

        email = str(account.get("email") or "").strip().lower()
        if email and account_warmup_service.is_hot(email):
            score += 12.0
    except Exception:
        pass

    last_chat = _parse_time(account.get("last_chat_at") or account.get("last_used_at"))
    if last_chat is not None:
        hours = max(0.0, (datetime.now(timezone.utc) - last_chat).total_seconds() / 3600.0)
        if hours <= 24:
            score += max(0.0, 10.0 - hours * 0.25)
        elif hours > 168:
            score -= 5.0

    success = int(account.get("success") or 0)
    fail = int(account.get("fail") or 0)
    total = success + fail
    if total > 0:
        rate = success / total
        score += (rate - 0.5) * 20.0

    streak = int(account.get("image_fail_streak") or 0)
    score -= min(25.0, streak * 4.0)

    quota = int(account.get("quota") or 0)
    if not _aci_quota_eligible(account):
        return 0.0
    score += min(15.0, quota * 0.5)

    next_ok = float(account.get("image_next_ok_ts") or 0.0)
    if next_ok > time.time():
        score -= 20.0

    if str(account.get("panda_probe_last_error") or "").strip():
        score -= 30.0

    return max(0.0, min(100.0, score))


def sort_tokens_by_aci(get_account, tokens: list[str]) -> list[str]:
    eligible = [token for token in tokens if _aci_quota_eligible(get_account(token) or {})]
    if not eligible:
        return []

    def key(token: str) -> tuple[float, str]:
        account = get_account(token) or {}
        return (-aci_score(account), token)

    return sorted(eligible, key=key)


def pick_token(tokens: list[str], *, get_account, explore: bool | None = None) -> str:
    if not tokens:
        raise RuntimeError("no candidate tokens for ACI pick")
    ranked = sort_tokens_by_aci(get_account, tokens)
    settings = config.get_image_pipeline_settings()
    epsilon = float(settings.get("aci_epsilon") or EPSILON_DEFAULT)
    do_explore = explore if explore is not None else random.random() < epsilon
    if do_explore and len(ranked) > 1:
        bottom_count = max(1, len(ranked) * 30 // 100)
        pool = ranked[-bottom_count:]
        return random.choice(pool)
    return ranked[0]


def apply_explore_success_bonus(account: dict[str, Any]) -> None:
    current = float(account.get("aci_score") or aci_score(account))
    account["aci_score"] = min(100.0, current + EXPLORE_BONUS)
