"""
Post-meeting summarizer.

Phase 1 of the canvas-rebuild-and-one-brain plan deleted the real-time
"Turn Processor" that used to act as a parallel brain alongside Gemini Live.
Gemini Live is now the only conversational brain; nothing here runs while a
meeting is in progress.

What's left in this module is a single Celery task that runs ONCE when a
bot leaves a meeting (`bot.leave_requested` / state=ended). It is a thin
alias to `agent.tasks.process_finished_meeting`, which already implements
the full post-meeting summarization pipeline (transcript pull → summarizer
LLM → MeetingTask extraction → embeddings).

We keep this name (`summarize_meeting_after_leave`) because the plan calls
it out explicitly, and we re-export it via Celery so any historical signal
handler that imports `process_meeting_turn` from here gets a clear
"deprecated" error instead of silently re-firing the old loop.
"""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("agent.turn_processor")


@shared_task(
    name="agent.turn_processor.summarize_meeting_after_leave",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=300,
    soft_time_limit=270,
)
def summarize_meeting_after_leave(self, bot_id: str) -> dict:
    """
    Run the post-meeting summarization pipeline for a bot that just left.

    This is the ONLY background task this module is responsible for. The
    real-time agent loop is owned by Gemini Live in `agent/live_session/`.
    """
    if not bot_id:
        return {"skipped": "no bot_id"}
    try:
        from agent.tasks import process_finished_meeting

        return process_finished_meeting.run(bot_id=bot_id)
    except Exception as exc:
        log.exception("summarize_meeting_after_leave: failed bot=%s", bot_id)
        return {"error": f"{type(exc).__name__}: {exc}"}


def process_meeting_turn(*_args, **_kwargs):
    """
    Deprecated. Real-time Turn Processor was removed in the canvas-rebuild
    refactor. Calling this is a no-op so any stragglers don't crash, but
    we log a warning so we can find and remove them.
    """
    log.warning(
        "process_meeting_turn() called but is removed; ignoring. "
        "Gemini Live now owns the live loop."
    )
    return {"skipped": "deprecated"}


def release_inflight(_bot_id: str) -> None:
    """Deprecated. Was used by the old scheduler's single-flight lock."""
    return None
