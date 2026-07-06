from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL

STATIC_MODEL_IDS = [
    "auto",
    "gpt-5",
    "gpt-5-1",
    "gpt-5-2",
    "gpt-5-3",
    "gpt-5-3-mini",
    "gpt-5-mini",
]


def _model_item(model: str) -> dict[str, Any]:
    return {
        "id": model,
        "object": "model",
        "created": 0,
        "owned_by": "chatgpt2api",
        "permission": [],
        "root": model,
        "parent": None,
    }


def list_models() -> dict[str, Any]:
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
    ]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if image_accounts:
        dynamic_models.add("gpt-image-2")
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    models = list(dict.fromkeys([*STATIC_MODEL_IDS, *sorted(dynamic_models)]))
    return {"object": "list", "data": [_model_item(model) for model in models]}
