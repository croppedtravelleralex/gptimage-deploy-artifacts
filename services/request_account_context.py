"""Request-scoped preferred account for text chat / search."""

from __future__ import annotations

from contextvars import ContextVar

preferred_account_email: ContextVar[str] = ContextVar("preferred_account_email", default="")


def set_preferred_account_email(email: str | None) -> None:
    preferred_account_email.set(str(email or "").strip())


def get_preferred_account_email() -> str:
    return str(preferred_account_email.get() or "").strip()
