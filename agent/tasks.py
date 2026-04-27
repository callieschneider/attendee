"""
Agent Celery tasks.
- process_finished_meeting: triggered when a bot reaches state=ended
- embed_entity_async: background embedding for any entity
- summarize_meeting_after_leave: post-meeting summarizer (re-exported so
  Celery's autodiscover picks it up via the `agent.tasks` module path).
"""
import logging
import uuid

from celery import shared_task
from django.utils import timezone

# Re-export so Celery autodiscover finds them.
from agent.turn_processor import summarize_meeting_after_leave  # noqa: F401
from agent.canvas.pump import push_canvas_images  # noqa: F401

log = logging.getLogger("agent.tasks")


@shared_task(name="agent.tasks.process_finished_meeting", bind=True, max_retries=3, default_retry_delay=30)
def process_finished_meeting(self, bot_id: str) -> dict:
    """
    Called when a bot reaches state=ended.
    1. Pull transcript from bots.Utterance (same DB)
    2. Get-or-create MeetingOccurrence
    3. Run Gemini Flash summarizer
    4. Persist summary + MeetingTask rows
    5. Trigger async embedding
    """
    from bots.models import Bot, Utterance, Participant, CalendarEvent
    from .models import MeetingOccurrence, MeetingTask, MeetingSeries
    from .pipelines.summarizer import summarize_transcript

    try:
        bot = Bot.objects.select_related("project", "calendar_event").get(object_id=bot_id)
    except Bot.DoesNotExist:
        log.warning("process_finished_meeting: bot %s not found", bot_id)
        return {"ok": False, "reason": "bot not found"}

    # Determine series:
    # 1. series_id in bot metadata (set at bot creation by create_meeting_bot or auto_create_bot_for_event)
    # 2. Assigned from CalendarEvent via series_manager
    # 3. Inbox fallback
    series = None
    metadata = bot.metadata or {}
    series_id_from_metadata = metadata.get("series_id")

    if series_id_from_metadata:
        try:
            series = MeetingSeries.objects.get(id=series_id_from_metadata)
        except MeetingSeries.DoesNotExist:
            log.warning("process_finished_meeting: series_id %s from metadata not found", series_id_from_metadata)

    if not series and bot.calendar_event:
        from .series_manager import assign_series
        series = assign_series(bot.calendar_event)

    if not series:
        series, _ = MeetingSeries.objects.get_or_create(
            title="Inbox",
            defaults={"description": "Auto-created for unassigned meeting occurrences"},
        )

    # Build initial occurrence defaults, linking to calendar event if available
    occ_defaults = {
        "series": series,
        "started_at": bot.created_at,
        "ended_at": timezone.now(),
    }
    if bot.calendar_event:
        cal_event = bot.calendar_event
        occ_defaults["calendar_event_object_id"] = cal_event.object_id
        occ_defaults["google_event_id"] = cal_event.platform_uuid or ""
        raw = cal_event.raw or {}
        occ_defaults["google_recurring_event_id"] = raw.get("recurringEventId", "")
        if cal_event.name:
            occ_defaults["title"] = cal_event.name[:255]

    occ, created = MeetingOccurrence.objects.get_or_create(
        bot=bot,
        defaults=occ_defaults,
    )

    # Update series if it was defaulted to something else and we now know better
    if not created and occ.series.title == "Inbox" and series.title != "Inbox":
        occ.series = series
        occ.save(update_fields=["series"])

    # Idempotency: if already processed, skip
    if not created and occ.summary:
        log.info("process_finished_meeting: already processed bot %s", bot_id)
        return {"ok": True, "idempotent": True, "occurrence_id": str(occ.id)}

    # Pull transcript text from Utterance table
    # FK chain: Utterance → Recording → Bot
    utterances = (
        Utterance.objects.filter(
            recording__bot=bot,
            transcription__isnull=False,
        )
        .select_related("participant")
        .order_by("timestamp_ms")
    )

    lines = []
    attendees = set()
    for u in utterances:
        speaker = ""
        if u.participant:
            speaker = u.participant.full_name or u.participant.uuid or ""
            if speaker:
                attendees.add(speaker)
        text = ""
        if isinstance(u.transcription, dict):
            text = u.transcription.get("transcript", "")
        elif isinstance(u.transcription, str):
            text = u.transcription
        if text.strip():
            prefix = f"{speaker}: " if speaker else ""
            lines.append(f"{prefix}{text.strip()}")

    transcript_text = "\n".join(lines)
    occ.transcript_text = transcript_text
    occ.attendees = list(attendees)

    # Run summarizer
    result = summarize_transcript(transcript_text)
    occ.summary = result.get("summary", "")

    # Title from meeting URL if available
    if not occ.title and bot.meeting_url:
        occ.title = bot.meeting_url[:128]

    occ.save()

    # Create MeetingTask rows for extracted action items
    task_count = 0
    for t in result.get("tasks", [])[:50]:
        title = t.get("title", "").strip()[:512]
        if not title:
            continue
        _, was_created = MeetingTask.objects.get_or_create(
            occurrence=occ,
            title=title,
            defaults={
                "assignee": t.get("assignee", "")[:255],
                "status": "pending",
            },
        )
        if was_created:
            task_count += 1

    # Trigger async embedding for this occurrence
    if occ.summary:
        embed_text = f"{occ.title}\n\n{occ.summary}"
        embed_entity_async.delay(
            entity_table="agent_meeting_occurrence",
            entity_id=str(occ.id),
            text=embed_text,
        )

    log.info(
        "process_finished_meeting: bot=%s occurrence=%s tasks=%d summary_len=%d",
        bot_id, occ.id, task_count, len(occ.summary),
    )

    return {
        "ok": True,
        "occurrence_id": str(occ.id),
        "task_count": task_count,
        "summary_length": len(occ.summary),
        "attendees": list(attendees),
    }


@shared_task(name="agent.tasks.embed_entity_async", max_retries=3, default_retry_delay=15)
def embed_entity_async(entity_table: str, entity_id: str, text: str) -> int:
    """
    Background task to chunk, embed, and store an entity's text.
    Fire-and-forget from other tasks — failures logged but don't break callers.
    """
    from .embeddings import store_entity_embedding

    try:
        count = store_entity_embedding(entity_table, uuid.UUID(entity_id), text)
        log.info("embed_entity_async: %s/%s → %d chunks", entity_table, entity_id, count)
        return count
    except Exception:
        log.exception("embed_entity_async: failed for %s/%s", entity_table, entity_id)
        return 0


@shared_task(name="agent.tasks.process_calendar_event", max_retries=3, default_retry_delay=30)
def process_calendar_event(calendar_event_object_id: str) -> dict:
    """
    Fired when a CalendarEvent is created/updated (via calendar.events_update webhook).
    Schedules a bot creation task timed 2 min before the event starts.
    """
    import datetime
    from bots.models import CalendarEvent
    from django.utils import timezone as tz

    try:
        event = CalendarEvent.objects.get(object_id=calendar_event_object_id)
    except CalendarEvent.DoesNotExist:
        log.warning("process_calendar_event: event %s not found", calendar_event_object_id)
        return {"ok": False, "reason": "event not found"}

    if event.is_deleted or not event.meeting_url:
        return {"ok": False, "reason": "deleted or no meeting_url"}

    now = tz.now()
    start = event.start_time
    trigger_at = start - datetime.timedelta(minutes=2)

    if trigger_at <= now:
        # Event starts very soon or already started — fire immediately
        auto_create_bot_for_event.delay(calendar_event_object_id)
        log.info("process_calendar_event: immediate dispatch for %s", calendar_event_object_id)
        return {"ok": True, "scheduled": "immediate"}

    countdown = int((trigger_at - now).total_seconds())
    auto_create_bot_for_event.apply_async(
        kwargs={"calendar_event_object_id": calendar_event_object_id},
        eta=trigger_at,
    )
    log.info("process_calendar_event: scheduled bot for %s in %ds at %s",
             calendar_event_object_id, countdown, trigger_at.isoformat())
    return {"ok": True, "scheduled_at": trigger_at.isoformat(), "countdown_seconds": countdown}


@shared_task(name="agent.tasks.auto_create_bot_for_event", max_retries=2, default_retry_delay=60)
def auto_create_bot_for_event(calendar_event_object_id: str) -> dict:
    """
    Creates an Attendee bot for a calendar event, wired to the audio bridge.
    Called ~2 min before event start_time.
    """
    import uuid as _uuid
    import requests as req
    from bots.models import CalendarEvent
    from django.conf import settings
    from django.utils import timezone as tz
    from .series_manager import assign_series

    try:
        event = CalendarEvent.objects.get(object_id=calendar_event_object_id)
    except CalendarEvent.DoesNotExist:
        log.warning("auto_create_bot_for_event: event %s not found", calendar_event_object_id)
        return {"ok": False, "reason": "event not found"}

    if event.is_deleted or not event.meeting_url:
        return {"ok": False, "reason": "deleted or no meeting_url"}

    # Idempotency: check for existing non-ended bot for this event
    active_states = [
        "ready", "joining", "waiting_room",
        "joined_not_recording", "joined_recording",
        "scheduled", "staged",
    ]
    if event.bots.filter(state__in=active_states).exists():
        log.info("auto_create_bot_for_event: bot already active for event %s", calendar_event_object_id)
        return {"ok": True, "skipped": "bot already active"}

    series = assign_series(event)
    bridge_domain = getattr(settings, "BRIDGE_DOMAIN", "")
    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    agent_app_url = getattr(settings, "AGENT_APP_URL", "")

    session_id = str(_uuid.uuid4())
    ws_url = f"wss://{bridge_domain}/audio/{session_id}" if bridge_domain else None

    bot_payload = {
        "meeting_url": event.meeting_url,
        "bot_name": "Meeting Agent",
        "google_meet_settings": {"use_login": True},
        "metadata": {
            "series_id": str(series.id),
            "calendar_event_object_id": calendar_event_object_id,
            "session_id": session_id,
            "event_title": event.name or "",
        },
    }
    if ws_url:
        bot_payload["websocket_settings"] = {
            "audio": {"url": ws_url, "sample_rate": 16000}
        }

    try:
        resp = req.post(
            f"{agent_app_url}/api/v1/bots",
            json=bot_payload,
            headers={"Authorization": f"Token {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        bot_data = resp.json()
        log.info("auto_create_bot_for_event: created bot %s for event '%s' series '%s'",
                 bot_data.get("id"), event.name, series.title)
        return {"ok": True, "bot_id": bot_data.get("id"), "series": series.title, "session_id": session_id}
    except Exception:
        log.exception("auto_create_bot_for_event: failed for event %s", calendar_event_object_id)
        return {"ok": False, "reason": "bot creation failed"}


@shared_task(name="agent.tasks.sync_upcoming_calendar_events", max_retries=1)
def sync_upcoming_calendar_events() -> dict:
    """
    Periodic task (run every 15 min) — finds all upcoming calendar events
    with Meet URLs and ensures bots are scheduled for them.
    """
    from bots.models import Calendar, CalendarPlatform
    from agent.series_manager import schedule_bot_for_upcoming_events

    calendars = Calendar.objects.filter(platform=CalendarPlatform.GOOGLE)
    total_scheduled = []
    for cal in calendars:
        scheduled = schedule_bot_for_upcoming_events(cal.object_id)
        total_scheduled.extend(scheduled)

    log.info("sync_upcoming_calendar_events: scheduled %d bots", len(total_scheduled))
    return {"ok": True, "scheduled_count": len(total_scheduled)}
