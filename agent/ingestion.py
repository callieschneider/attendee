"""
Transcript / chat event ingestion.

Turns Attendee webhook payloads into durable TranscriptEvent rows
and ensures a MeetingCursor exists for every active bot.

Called synchronously from `views.attendee_webhook`. Kept fast —
no LLM calls, no expensive work. The Turn Scheduler
(`agent.scheduler.maybe_schedule_turn`) decides when to actually process.
"""
from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

log = logging.getLogger("agent.ingestion")


# ── MeetingCursor bootstrap ────────────────────────────────────────────────────


def _resolve_series_budget(bot_id: str) -> Decimal:
    """
    Resolve the per-meeting budget cap for a given bot. Preference order:
      1. Bot.metadata["series_id"] → MeetingSeries.max_cost_usd_per_meeting
      2. Latest MeetingOccurrence for the bot → series override
      3. settings.AGENT_MAX_TURN_BUDGET_USD
    """
    from agent.models import MeetingOccurrence, MeetingSeries
    from bots.models import Bot

    default = Decimal(str(getattr(settings, "AGENT_MAX_TURN_BUDGET_USD", 10.0)))
    try:
        bot = Bot.objects.filter(object_id=bot_id).only("metadata").first()
        series_id: Optional[str] = None
        if bot and bot.metadata:
            series_id = bot.metadata.get("series_id")
        if not series_id:
            occ = (
                MeetingOccurrence.objects.filter(bot__object_id=bot_id)
                .only("series_id")
                .order_by("-created_at")
                .first()
            )
            if occ:
                series_id = str(occ.series_id)
        if series_id:
            series = MeetingSeries.objects.filter(id=series_id).only(
                "max_cost_usd_per_meeting"
            ).first()
            if series and series.max_cost_usd_per_meeting is not None:
                return series.max_cost_usd_per_meeting
    except Exception:
        log.exception("_resolve_series_budget: failed for bot %s", bot_id)
    return default


def ensure_cursor(bot_id: str):
    """
    Get-or-create a MeetingCursor for the given bot, wiring in the
    resolved per-meeting budget cap on first creation.
    """
    from agent.models import MeetingCursor

    cursor, created = MeetingCursor.objects.get_or_create(bot_id=bot_id)
    if created:
        cursor.budget_cap_usd = _resolve_series_budget(bot_id)
        cursor.save(update_fields=["budget_cap_usd"])
        log.info(
            "ingestion: MeetingCursor created for bot=%s budget=$%s",
            bot_id,
            cursor.budget_cap_usd,
        )
    return cursor


# ── Occurrence binding ─────────────────────────────────────────────────────────


def _find_occurrence_for_bot(bot_id: str):
    """
    Locate the live MeetingOccurrence for this bot, if one exists.
    Returns MeetingOccurrence | None.
    """
    from agent.models import MeetingOccurrence

    try:
        return (
            MeetingOccurrence.objects.filter(bot__object_id=bot_id)
            .order_by("-created_at")
            .first()
        )
    except Exception:
        log.exception("_find_occurrence_for_bot: lookup failed for %s", bot_id)
        return None


# ── transcript.update ingestion ────────────────────────────────────────────────


def _timestamp_ms_to_dt(timestamp_ms: Any) -> datetime.datetime:
    try:
        ms = int(timestamp_ms)
        # Attendee's `timestamp_ms` is an utterance-relative offset in ms.
        # For recent utterances (sub-minute values) we fall back to wall clock;
        # for realistic unix-epoch-ms values (>10^12) we parse directly.
        if ms > 10**11:
            return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    except (TypeError, ValueError):
        pass
    return timezone.now()


def ingest_transcript_update(bot_id: str, data: dict) -> dict:
    """
    Handle an Attendee `transcript.update` webhook.

    Payload shape (from bots/webhook_payloads.py):
        {
            "speaker_name": str,
            "speaker_uuid": str,
            "speaker_user_uuid": str | null,
            "speaker_is_host": bool,
            "timestamp_ms": int,
            "duration_ms": int,
            "transcription": {"transcript": str} | null,
        }

    There is no utterance_id on the wire (Attendee fires one webhook per
    final utterance), so we synthesize a stable ref from speaker + timestamp
    for dedup purposes.
    """
    from agent.models import TranscriptEvent

    if not bot_id:
        return {"ignored": "no bot_id"}

    text = ""
    transcription = data.get("transcription") or {}
    if isinstance(transcription, dict):
        text = (transcription.get("transcript") or "").strip()
    if not text:
        return {"ignored": "empty transcript"}

    speaker = (data.get("speaker_name") or "").strip()
    speaker_uuid = (data.get("speaker_uuid") or "").strip()
    timestamp_ms = data.get("timestamp_ms", 0)
    event_time = _timestamp_ms_to_dt(timestamp_ms)

    # Tag self-utterances so the scheduler/classifier can ignore them, but
    # keep them in the DB so they show in the canvas debug UI.
    is_self = _is_self_speech(bot_id, speaker)

    # Synthesize a stable dedup key: speaker_uuid + start ms works because
    # Attendee fires exactly one webhook per finalized utterance.
    utterance_ref = f"{speaker_uuid or 'anon'}:{timestamp_ms}"

    ensure_cursor(bot_id)
    occurrence = _find_occurrence_for_bot(bot_id)

    try:
        with transaction.atomic():
            event, created = TranscriptEvent.objects.update_or_create(
                bot_id=bot_id,
                utterance_ref=utterance_ref,
                defaults={
                    "kind": "speech",
                    "event_time": event_time,
                    "speaker": speaker,
                    "speaker_uuid": speaker_uuid,
                    "text": text,
                    "raw": data,
                    "occurrence": occurrence,
                },
            )
    except Exception:
        log.exception("ingest_transcript_update: failed bot=%s ref=%s", bot_id, utterance_ref)
        return {"error": "persist failed"}

    # Voice-state intent (sleep / wake) is decided by the LLMs via the
    # `voice_sleep` / `voice_wake` tools — Gemini Live calls them when it
    # hears the user, and the Turn Processor calls them based on webhook
    # transcripts (the only path that still reaches us while voice is
    # suspended and the gate is closed).

    # Any speech event extends the audio gate if it's already open —
    # this is how conversations feel natural (user doesn't have to re-trigger
    # the agent's name on every turn). Direct-address detection below can
    # still OPEN a closed gate.
    _extend_gate_safely(bot_id, ttl_seconds=120)

    # Direct-address detection → open the audio gate AND replay the trigger
    # utterance to Live as text so it can answer the FIRST utterance (no
    # "say my name twice" lag).
    _maybe_open_gate_on_address(bot_id, text, speaker=speaker)

    # Fire-and-forget schedule. Gemini Live handles live conversation
    # natively when the gate is open (low latency, no round-trip through
    # the Turn Processor). The Turn Processor still runs for actions
    # (tasks, artifacts, URLs) — but not just to produce a spoken reply
    # during an active conversation.
    _schedule_turn_safely(bot_id)

    return {
        "kind": "speech",
        "event_id": str(event.id),
        "created": created,
    }


# ── chat_messages.update ingestion ─────────────────────────────────────────────


def ingest_chat_message(bot_id: str, data: dict) -> dict:
    """
    Handle an Attendee `chat_messages.update` webhook.

    Payload shape (ChatMessageSerializer):
        {
            "id": "msg_xxx",
            "text": str,
            "timestamp_ms": int,
            "timestamp": int,
            "to": "everyone" | "only_bot",
            "sender_name": str,
            "sender_uuid": str,
            "sender_user_uuid": str | null,
            "additional_data": {...},
        }
    """
    from agent.models import TranscriptEvent

    if not bot_id:
        return {"ignored": "no bot_id"}

    text = (data.get("text") or "").strip()
    if not text:
        return {"ignored": "empty chat"}

    message_id = (data.get("id") or "").strip()
    speaker = (data.get("sender_name") or "").strip()
    speaker_uuid = (data.get("sender_uuid") or "").strip()
    timestamp_ms = data.get("timestamp_ms", 0)
    event_time = _timestamp_ms_to_dt(timestamp_ms)
    utterance_ref = f"chat:{message_id or speaker_uuid}:{timestamp_ms}"

    ensure_cursor(bot_id)
    occurrence = _find_occurrence_for_bot(bot_id)

    is_self = _is_self_speech(bot_id, speaker)
    raw_with_tag = dict(data) if isinstance(data, dict) else {"raw": data}
    raw_with_tag["chat_message"] = True
    if is_self:
        raw_with_tag["self_utterance"] = True

    try:
        with transaction.atomic():
            event, created = TranscriptEvent.objects.update_or_create(
                bot_id=bot_id,
                utterance_ref=utterance_ref,
                defaults={
                    "kind": "chat",
                    "event_time": event_time,
                    "speaker": speaker,
                    "speaker_uuid": speaker_uuid,
                    "text": text,
                    "raw": raw_with_tag,
                    "occurrence": occurrence,
                },
            )
    except Exception:
        log.exception("ingest_chat_message: failed bot=%s ref=%s", bot_id, utterance_ref)
        return {"error": "persist failed"}

    # Skip classifier + scheduler entirely for self-utterances.
    if is_self:
        return {
            "kind": "chat",
            "event_id": str(event.id),
            "created": created,
            "self_utterance": True,
        }

    # NOTE: Turn Processor / scheduler is intentionally NOT triggered here.
    # Gemini Live is the conversational primary and handles all tool calls
    # itself (with `call_model` to escalate to Haiku when needed).
    # Running a parallel Haiku loop on every transcript event added noise
    # and cost without value.

    return {
        "kind": "speech",
        "event_id": str(event.id),
        "created": created,
    }


# ── Turn Scheduler hook ────────────────────────────────────────────────────────


def _is_self_speech(bot_id: str, speaker_name: str) -> bool:
    """
    Return True if the speaker is the bot itself.

    The bot's display name in Meet comes from the Google Workspace profile
    when the bot is signed in via SSO, NOT from `bot_name` we pass at create
    time. Common defaults: "Meeting Agent". We also include any per-bot
    configured names as a belt-and-suspenders check.
    """
    if not speaker_name:
        return False
    name_lower = speaker_name.strip().lower()
    if not name_lower:
        return False

    self_names = {"meeting agent"}  # Workspace seat default
    try:
        from django.conf import settings

        agent_name = (getattr(settings, "AGENT_NAME", "") or "").strip().lower()
        if agent_name:
            self_names.add(agent_name)
    except Exception:
        pass
    try:
        from bots.models import Bot

        bot = Bot.objects.filter(object_id=bot_id).only("name").first()
        if bot and bot.name:
            self_names.add(bot.name.strip().lower())
    except Exception:
        pass

    return name_lower in self_names


def _maybe_open_gate_on_address(bot_id: str, text: str, speaker: str = "") -> bool:
    """
    If the utterance directly addresses the agent, open the audio gate
    AND replay the utterance to Live as text so it can answer the very
    first utterance instead of waiting for a second one. Without this
    text-replay, Live never heard the audio for the wake utterance
    (gate was closed during that audio) and silently waits for the
    next utterance — the "say my name twice" UX bug.
    """
    try:
        from agent.classifiers import is_addressed

        if is_addressed(text):
            # Clear any sticky sleep flag — the user is talking to us again
            try:
                from agent.live_session.signals import set_voice_suspended
                set_voice_suspended(bot_id, False)
            except Exception:
                pass
            _open_gate_safely(bot_id, reason="direct_address", ttl_seconds=180)
            # Replay the trigger utterance to Live so it has the question
            # in its context window even though the audio was missed.
            try:
                from agent.live_session.signals import publish_voice_context

                replay = f"{speaker or 'User'} just said: {text}"
                publish_voice_context(bot_id, replay)
            except Exception:
                log.exception("_maybe_open_gate_on_address: voice_context replay failed")
            return True
    except Exception:
        log.exception("_maybe_open_gate_on_address: classifier failed bot=%s", bot_id)
    return False


def _kick_canvas_safely(bot_id: str) -> None:
    """Trigger an immediate canvas refresh so voice-state changes appear ASAP."""
    try:
        from agent.canvas.pump import push_canvas_images_for_bot
        push_canvas_images_for_bot(bot_id)
    except Exception:
        log.exception("_kick_canvas_safely: failed bot=%s", bot_id)


def _open_gate_safely(bot_id: str, reason: str, ttl_seconds: int = 30) -> None:
    try:
        from agent.live_session.signals import publish_gate_open

        publish_gate_open(bot_id, reason=reason, ttl_seconds=ttl_seconds)
    except ImportError:
        pass
    except Exception:
        log.exception("_open_gate_safely: publish failed bot=%s", bot_id)


def _extend_gate_safely(bot_id: str, ttl_seconds: int = 30) -> None:
    """
    Publish a gate-extend signal. The bridge will only act if the gate is
    already open. Used on every speech event to keep conversations alive.
    """
    try:
        from agent.live_session.signals import publish_gate_extend

        publish_gate_extend(bot_id, ttl_seconds=ttl_seconds)
    except ImportError:
        pass
    except Exception:
        log.exception("_extend_gate_safely: publish failed bot=%s", bot_id)


def _schedule_turn_safely(bot_id: str, priority: str = "normal") -> None:
    """
    Best-effort hook to notify the Turn Scheduler that new events exist.
    Import-lazily so missing pieces during partial rollout don't break ingestion.
    """
    try:
        from agent.scheduler import maybe_schedule_turn

        maybe_schedule_turn(bot_id, priority=priority)
    except ImportError:
        # Scheduler not yet loaded — webhook receivers are still useful on their own.
        pass
    except Exception:
        log.exception("_schedule_turn_safely: failed for bot=%s", bot_id)
