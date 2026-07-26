from __future__ import annotations

import logging
import time
from dataclasses import replace

from services.image_pipeline.orchestrator import PipelineRun
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import ConversationRequest, collect_text

logger = logging.getLogger(__name__)

_ENHANCE_SYSTEM = {
    "en": (
        "You are a prompt engineer for image generation. "
        "Rewrite the user's prompt into a detailed English image-generation prompt. "
        "Keep the subject and intent; add lighting, composition, and style cues. "
        "Output only the enhanced prompt, no preamble."
    ),
    "same_as_user": (
        "你是图像生成提示词工程师。将用户提示扩写为更具体的生图描述，"
        "保持原意与语种，补充构图、光线与风格。只输出扩写后的提示词。"
    ),
}


def _enhance_locale(locale: str) -> str:
    text = str(locale or "en").strip().lower()
    if text in {"same_as_user", "same", "user"}:
        return "same_as_user"
    return "en"


def run_prompt_enhance(pipeline_run: PipelineRun, request: ConversationRequest) -> str:
    """Execute pS: acquire slot, text account, single-turn enhance. Fail-open to original prompt."""
    original = str(request.prompt or "").strip()
    if not original or not pipeline_run.needs_ps:
        return original

    locale = _enhance_locale(getattr(request, "prompt_enhance_locale", "en"))
    system_prompt = _ENHANCE_SYSTEM[locale]
    ps_slot, _queue_ms = pipeline_run.acquire_ps()
    started = time.monotonic()
    enhanced = original
    try:
        lease = pipeline_run.account_provider.acquire_for_ps(
            skip_global_limit=bool(request.queue_coordinated),
        )
        try:
            backend = OpenAIBackendAPI(access_token=lease.access_token)
            enhance_request = replace(
                request,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": original},
                ],
                prompt=original,
            )
            text = collect_text(backend, enhance_request).strip()
            if text and len(text) >= len(original) * 0.5:
                enhanced = text
                pipeline_run.state.enhanced_prompt = enhanced
                pipeline_run.state.ps_account_id = lease.account_id
                pipeline_run.state.ps_access_token = lease.access_token
                pipeline_run.note_ps_done()
                logger.info({
                    "event": "image_prompt_enhance_ok",
                    "task_key": pipeline_run.task_key,
                    "locale": locale,
                    "original_len": len(original),
                    "enhanced_len": len(enhanced),
                })
            else:
                logger.warning({
                    "event": "image_prompt_enhance_empty",
                    "task_key": pipeline_run.task_key,
                })
        finally:
            lease.release()
    except Exception as exc:
        logger.warning({
            "event": "image_prompt_enhance_failed",
            "task_key": pipeline_run.task_key,
            "error": str(exc)[:300],
        })
    finally:
        pipeline_run.release_ps(ps_slot)
        pipeline_run.state.timings.ps_ms += int((time.monotonic() - started) * 1000)
    return enhanced
