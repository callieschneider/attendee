"""
Turn Scheduler — decides WHEN the Turn Processor should run for a bot.

Called synchronously from webhook ingestion. Cheap: does a couple of
indexed queries on MeetingCursor + TranscriptEvent, then enqueues the
(slow) Celery task when the gating conditions pass.

Gate conditions (any triggers a turn):
  - `priority="chat"` (always run) — chat mentions and wake phrases are immediate
  - Unprocessed gap since last cursor ≥ AGENT_TURN_WINDOW_SECONDS
  - Time since latest event ≥ AGENT_PAUSE_THRESHOLD_SECONDS AND enough content
  - Direct trigger: latest event contains agent name or @agent chat mention

Inflight protection: a Redis SETNX lock keyed on `agent:turn:inflight:<bot_id>`
prevents parallel turns for the same bot when webhooks arrive in rapid bursts
before `MeetingCursor.last_turn_at` has been updated.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("agent.scheduler")

# Minimum seconds between turn invocations for the same bot.
# Low value because Turn Processor is now the conversational brain — it
# needs to fire promptly when the user finishes a thought.
TURN_DEBOUNCE_SECONDS = 0.5

# Redis-backed single-flight TTL (secs). Slightly longer than the Turn Processor's
# soft_time_limit (80s) so the lock clears naturally if the task crashes.
INFLIGHT_LOCK_TTL = 95


_REDIS = None


def _get_redis():
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    try:
        import redis

        url = (
            os.getenv("REDIS_URL")
            or os.getenv("CELERY_BROKER_URL")
            or "redis://localhost:6379/0"
        )
        _REDIS = redis.from_url(url, decode_responses=True)
        return _REDIS
    except Exception:
        log.exception("scheduler: redis unavailable")
        return None


def _inflight_key(bot_id: str) -> str:
    return f"agent:turn:inflight:{bot_id}"


def _try_acquire_inflight(bot_id: str) -> bool:
    """SETNX-style lock. Returns True iff we acquired it."""
    r = _get_redis()
    if r is None:
        return True  # fail-open: scheduler isn't the last line of defense
    try:
        return bool(r.set(_inflight_key(bot_id), "1", ex=INFLIGHT_LOCK_TTL, nx=True))
    except Exception:
        log.exception("scheduler: inflight SETNX failed bot=%s", bot_id)
        return True


def release_inflight(bot_id: str) -> None:
    """Called by the Turn Processor after it finishes to free the lock early."""
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(_inflight_key(bot_id))
    except Exception:
        log.exception("scheduler: inflight DEL failed bot=%s", bot_id)


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
        log.warning("DBG68285d B0 early_return=skipped_budget bot=%s", bot_id)
        return "skipped_budget"

    now = timezone.now()

    # Debounce: if a turn fired very recently, skip unless priority=chat
    if (
        priority != "chat"
        and cursor.last_turn_at
        and (now - cursor.last_turn_at).total_seconds() < TURN_DEBOUNCE_SECONDS
    ):
        log.warning("DBG68285d B0 early_return=deferred_recent bot=%s last_turn_at=%s", bot_id, cursor.last_turn_at)
        return "deferred_recent"

    # Find new events since cursor — exclude:
    #   - self-utterances (the bot's own TTS played back through mixed audio,
    #     OR Gemini Live's outputTranscription)
    #   - in-flight gemini_live fragments (only finalized utterances trigger
    #     a turn; partials are display-only)
    # Attendee/Deepgram rows have no `raw.source` set; Gemini Live rows are
    # tagged `raw.source="gemini_live"` and only finalized when
    # `raw.finished == True`.
    from django.db.models import Q
    qs = (
        TranscriptEvent.objects.filter(bot_id=bot_id)
        .filter(Q(raw__self_utterance__isnull=True) | Q(raw__self_utterance=False))
        .filter(
            Q(raw__source__isnull=True)
            | ~Q(raw__source="gemini_live")
            | Q(raw__finished=True)
        )
    )
    if cursor.cursor_event_time:
        qs = qs.filter(event_time__gt=cursor.cursor_event_time)
    latest = qs.order_by("-event_time", "-created_at").first()
    if latest is None:
        total = TranscriptEvent.objects.filter(bot_id=bot_id).count()
        log.warning("DBG68285d B0 early_return=no_new_events bot=%s cursor_event_time=%s total_events=%d", bot_id, cursor.cursor_event_time, total)
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

    # region agent log
    log.warning("DBG68285d B should_run=%s bot=%s priority=%s gap=%.1f silence=%.1f window=%.1f pause_trigger=%s is_wake=%s text=%r",
        should_run, bot_id, priority, gap, silence, window, pause_trigger, _is_wake_trigger(latest), (latest.text or "")[:60])
    # endregion

    if not should_run:
        log.warning("DBG68285d B waiting bot=%s gap=%.1f silence=%.1f window=%.1f pause_min=%.1f is_wake=%s text=%r",
            bot_id, gap, silence, window, min_content_for_pause, _is_wake_trigger(latest), (latest.text or "")[:60])
        return "waiting"

    # Single-flight lock — prevents N webhooks-in-a-burst from enqueuing N turns
    # before any of them has a chance to update cursor.last_turn_at.
    if not _try_acquire_inflight(bot_id):
        log.warning("DBG68285d B deferred_inflight bot=%s", bot_id)
        return "deferred_inflight"

    # Enqueue the task (soft_time_limit in the task itself)
    try:
        from agent.turn_processor import process_meeting_turn

        cursor_iso = cursor.cursor_event_time.isoformat() if cursor.cursor_event_time else None
        result = process_meeting_turn.delay(
            bot_id=bot_id,
            cursor_event_time_iso=cursor_iso,
            priority=priority,
        )
        log.warning("DBG68285d B enqueued bot=%s task_id=%s", bot_id, result.id)
        return "scheduled"
    except Exception:
        # If enqueue failed, release the lock so we don't block future turns
        release_inflight(bot_id)
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
