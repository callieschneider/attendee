"""
Agent Celery tasks.
- process_finished_meeting: triggered when a bot reaches state=ended
- embed_entity_async: background embedding for any entity
"""
import logging
import uuid

from celery import shared_task
from django.utils import timezone

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
    from bots.models import Bot, Utterance, Participant
    from .models import MeetingOccurrence, MeetingTask, MeetingSeries
    from .pipelines.summarizer import summarize_transcript

    try:
        bot = Bot.objects.select_related("project").get(object_id=bot_id)
    except Bot.DoesNotExist:
        log.warning("process_finished_meeting: bot %s not found", bot_id)
        return {"ok": False, "reason": "bot not found"}

    # Default to catch-all "Inbox" series for unassigned bots
    series, _ = MeetingSeries.objects.get_or_create(
        title="Inbox",
        defaults={"description": "Auto-created for unassigned meeting occurrences"},
    )

    occ, created = MeetingOccurrence.objects.get_or_create(
        bot=bot,
        defaults={
            "series": series,
            "started_at": bot.created_at,
            "ended_at": timezone.now(),
        },
    )

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
