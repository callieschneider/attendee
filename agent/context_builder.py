"""
Legacy shim — delegates to agent/context_engine/builder.py.

Existing callers (views.live_voice_token, bridge.build_gemini_context,
tasks.process_finished_meeting if any) pass (series_id, occurrence_id, extra_context)
and expect a plain string back. We keep that signature while the underlying
engine does the real work.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("agent.context_builder")


def build_context(
    series_id: Optional[str] = None,
    occurrence_id: Optional[str] = None,
    extra_context: str = "",
) -> str:
    from .context_engine.builder import build_context as _builder

    try:
        result = _builder(
            series_id=series_id,
            occurrence_id=occurrence_id,
            task="initial_voice_setup",
        )
        prompt = result["prompt_markdown"]
    except Exception:
        log.exception("build_context: context_engine failed — returning minimal fallback")
        from .context_engine.formatter import BASE_SYSTEM_PROMPT

        prompt = BASE_SYSTEM_PROMPT

    if extra_context:
        prompt = f"{prompt}\n\n## Additional Context\n{extra_context}\n"
    return prompt.strip()
