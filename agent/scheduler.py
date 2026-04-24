"""
Turn Scheduler — decides WHEN the Turn Processor should run for a bot.

Called synchronously from webhook ingestion. Cheap: does a couple of
indexed queries on MeetingCursor + TranscriptEvent, then enqueues the
(slow) Celery task when the gating conditions pass.

Gate conditions (any triggers a turn):
  - `priority="chat"` (always run) — chat mentions and wake phrases are immediate
  - Unprocessed gap since last cursor ≥ AGENT_TURN_WINDOW_SECONDS
  - Time since latest event ≥ AGENT_PAUSE_THRESHOLD_SECONDS AND any new content
  - Direct trigger: latest event contains agent name or @agent chat mention
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("agent.scheduler")

# Minimum seconds between turn invocations for the same bot (debounce).
# Raised to 15s in response to Gemini 2.5 Flash free-tier rate limit (20 req/min)
# being hit during early smoke tests. A chat priority signal always bypasses.
TURN_DEBOUNCE_SECONDS = 15.0


def maybe_schedule_turn(bot_id: str, priority: str = "normal") -> str:
    """
    Decide whether to fire a turn for this bot and enqueue it if so.
    Returns a string status: "scheduled" | "deferred_recent" | "no_new_events" |
    "waiting" | "skipped_budget".
    Never raises.
    """
    try:
        return _maybe_schedule(bot_id, priority)
    except Exception:
        log.exception("maybe_schedule_turn: failed bot=%s", bot_id)
        return "error"


def _maybe_schedule(bot_id: str, priority: str) -> str:
    from agent.ingestion import ensure_cursor
    from agent.models import MeetingCursor, TranscriptEvent

    if not bot_id:
        return "no_bot_id"

    cursor = ensure_cursor(bot_id)
    if cursor.budget_exceeded:
        return "skipped_budget"

    now = timezone.now()

    # Debounce: if a turn fired very recently, skip unless priority=chat
    if (
        priority != "chat"
        and cursor.last_turn_at
        and (now - cursor.last_turn_at).total_seconds() < TURN_DEBOUNCE_SECONDS
    ):
        return "deferred_recent"

    # Find new events since cursor
    qs = TranscriptEvent.objects.filter(bot_id=bot_id)
    if cursor.cursor_event_time:
        qs = qs.filter(event_time__gt=cursor.cursor_event_time)
    latest = qs.order_by("-event_time", "-created_at").first()
    if latest is None:
        return "no_new_events"

    # Trigger decision
    window = float(getattr(settings, "AGENT_TURN_WINDOW_SECONDS", 8))
    pause = float(getattr(settings, "AGENT_PAUSE_THRESHOLD_SECONDS", 2.0))
    # Minimum new content before pause-triggered turns fire. Prevents tiny
    # utterances from firing a turn every time the speaker pauses.
    min_content_for_pause = float(getattr(settings, "AGENT_PAUSE_MIN_CONTENT_SECONDS", 6.0))

    oldest = qs.order_by("event_time", "created_at").first() or latest
    gap = (latest.event_time - oldest.event_time).total_seconds()
    silence = (now - latest.event_time).total_seconds()

    # Pause trigger only fires AFTER enough content has accumulated
    pause_trigger = silence >= pause and gap >= min_content_for_pause

    should_run = (
        priority == "chat"
        or _is_wake_trigger(latest)
        or gap >= window
        or pause_trigger
    )

    if not should_run:
        return "waiting"

    # Enqueue the task (soft_time_limit in the task itself)
    try:
        from agent.turn_processor import process_meeting_turn

        cursor_iso = cursor.cursor_event_time.isoformat() if cursor.cursor_event_time else None
        process_meeting_turn.delay(
            bot_id=bot_id,
            cursor_event_time_iso=cursor_iso,
            priority=priority,
        )
        return "scheduled"
    except Exception:
        log.exception("_maybe_schedule: enqueue failed bot=%s", bot_id)
        return "enqueue_failed"


def _is_wake_trigger(event) -> bool:
    """
    Lightweight string check — does this event mention the agent by name
    or @-mention it in chat? Avoids LLM classifier cost for the fast path.
    """
    agent_name = getattr(settings, "AGENT_NAME", "Clever Star")
    text = (event.text or "").lower()
    name_lower = agent_name.lower()
    if not text:
        return False
    if name_lower in text:
        return True
    if event.kind == "chat" and "@agent" in text:
        return True
    return False
