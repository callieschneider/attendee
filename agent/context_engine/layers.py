"""
Retrieval layers — pull the raw material for a context build.

Each layer returns a plain list of dicts (simple serializable shapes),
so downstream formatters/budget pass only see dicts and the builder
itself can glue them together without ORM coupling.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.utils import timezone

log = logging.getLogger("agent.context_engine.layers")


def _normalize_dt(dt):
    """Return a tz-aware datetime, defaulting to UTC for naive values."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.utc)
    return dt


# ── Series / meeting facts ─────────────────────────────────────────────────────


def get_series_config(series_id: Optional[str]) -> Optional[dict]:
    """Return the core MeetingSeries config as a dict, or None if missing."""
    if not series_id:
        return None
    from agent.models import MeetingSeries

    try:
        series = MeetingSeries.objects.get(id=series_id)
    except MeetingSeries.DoesNotExist:
        log.warning("get_series_config: series %s not found", series_id)
        return None
    return {
        "id": str(series.id),
        "title": series.title,
        "description": series.description,
        "tags": list(series.tags or []),
        "agent_name_override": series.agent_name_override,
        "agent_verbosity": series.agent_verbosity,
        "agent_proactivity": series.agent_proactivity,
        "allowed_tool_categories": list(series.allowed_tool_categories or []),
    }


def get_pinned_context_items(series_id: Optional[str], limit: int = 10) -> list[dict]:
    if not series_id:
        return []
    from agent.models import ContextItem

    qs = (
        ContextItem.objects.filter(series_id=series_id, is_pinned=True)
        .order_by("order")[:limit]
    )
    return [
        {"label": item.label, "content": item.content, "order": item.order}
        for item in qs
    ]


def get_recent_occurrence_summaries(series_id: Optional[str], limit: int = 5) -> list[dict]:
    if not series_id:
        return []
    from agent.models import MeetingOccurrence

    qs = (
        MeetingOccurrence.objects.filter(series_id=series_id)
        .exclude(summary="")
        .order_by("-started_at")[:limit]
    )
    out = []
    for occ in qs:
        out.append(
            {
                "id": str(occ.id),
                "title": occ.title
                or (f"Meeting {occ.started_at:%Y-%m-%d}" if occ.started_at else "Meeting"),
                "summary": occ.summary[:600],
                "started_at": _normalize_dt(occ.started_at),
                "attendees": list(occ.attendees or []),
            }
        )
    return out


def get_open_tasks(series_id: Optional[str], limit: int = 10) -> list[dict]:
    if not series_id:
        return []
    from agent.models import Task

    # Priority sort: critical→high→medium→low. The stored column is a string —
    # we use a Case/When to get a stable ordering.
    from django.db.models import Case, IntegerField, Value, When

    priority_order = Case(
        When(priority="critical", then=Value(0)),
        When(priority="high", then=Value(1)),
        When(priority="medium", then=Value(2)),
        When(priority="low", then=Value(3)),
        default=Value(4),
        output_field=IntegerField(),
    )
    qs = (
        Task.objects.filter(series_id=series_id)
        .exclude(status__in=["done", "cancelled"])
        .annotate(priority_rank=priority_order)
        .order_by("priority_rank", "due_date", "-updated_at")[:limit]
    )
    return [
        {
            "id": str(t.id),
            "title": t.title,
            "description": (t.description or "")[:200],
            "priority": t.priority,
            "status": t.status,
            "owner": t.owner,
            "due_date": t.due_date.isoformat() if t.due_date else None,
        }
        for t in qs
    ]


def get_relevant_artifacts(
    query: Optional[str], series_id: Optional[str], limit: int = 5
) -> list[dict]:
    """
    Semantic search against agent_artifact. If query is empty, returns the
    most recently updated artifacts for the series.
    """
    from agent.models import Artifact

    if not query:
        qs = Artifact.objects.filter(is_deleted=False)
        if series_id:
            qs = qs.filter(series_id=series_id)
        qs = qs.order_by("-updated_at")[:limit]
        return [
            {
                "id": str(a.id),
                "title": a.title,
                "type": a.type,
                "content": (a.content or "")[:500],
                "url": a.url or "",
                "tags": list(a.tags or []),
                "updated_at": _normalize_dt(a.updated_at),
                "similarity": None,
            }
            for a in qs
        ]

    # Semantic path
    try:
        from agent.embeddings import generate_embedding, vector_search_chunked
    except Exception:
        log.exception("get_relevant_artifacts: embeddings module unavailable")
        return []

    try:
        emb = generate_embedding(query)
        hits = vector_search_chunked("agent_artifact", emb, limit=limit * 2, threshold=0.25)
    except Exception:
        log.exception("get_relevant_artifacts: vector search failed")
        return []

    if not hits:
        return []

    ids = [h["entity_id"] for h in hits]
    qs = Artifact.objects.filter(id__in=ids, is_deleted=False)
    if series_id:
        qs = qs.filter(series_id=series_id)
    by_id = {str(a.id): a for a in qs}

    out = []
    for h in hits:
        a = by_id.get(str(h["entity_id"]))
        if not a:
            continue
        out.append(
            {
                "id": str(a.id),
                "title": a.title,
                "type": a.type,
                "content": (a.content or "")[:500],
                "url": a.url or "",
                "tags": list(a.tags or []),
                "updated_at": _normalize_dt(a.updated_at),
                "similarity": float(h.get("similarity", 0.0)),
            }
        )
        if len(out) >= limit:
            break
    return out


# ── Live-meeting state ────────────────────────────────────────────────────────


def get_recent_transcript(bot_id: str, last_n_events: int = 50) -> list[dict]:
    """Pull the most recent transcript events for a bot.

    Default is 50 but callers (live_turn, initial_voice_setup) override with a
    much larger value so the conversation history can span the full meeting.
    Live STT rows (raw.source='gemini_live') are filtered out — Attendee is the
    canonical transcript source.
    """
    from agent.models import TranscriptEvent
    from django.db.models import Q

    if not bot_id:
        return []
    qs = (
        TranscriptEvent.objects.filter(bot_id=bot_id)
        .filter(Q(raw__source__isnull=True) | ~Q(raw__source="gemini_live"))
        .order_by("-event_time", "-created_at")[:last_n_events]
    )
    events = list(qs)
    events.reverse()  # chronological
    return [
        {
            "kind": e.kind,
            "event_time": _normalize_dt(e.event_time),
            "speaker": e.speaker,
            "text": e.text,
        }
        for e in events
    ]


def get_recent_action_log(bot_id: str, limit: int = 20) -> list[dict]:
    from agent.models import ActionLogEntry

    if not bot_id:
        return []
    qs = (
        ActionLogEntry.objects.filter(bot_id=bot_id)
        .order_by("-created_at")[:limit]
    )
    entries = list(qs)
    entries.reverse()
    return [
        {
            "id": str(e.id),
            "turn_id": str(e.turn_id),
            "tool_name": e.tool_name,
            "tool_input": e.tool_input,
            "tool_result": e.tool_result,
            "status": e.status,
            "error_message": e.error_message,
            "is_archived": e.is_archived,
            "created_at": _normalize_dt(e.created_at),
        }
        for e in entries
    ]


# ── Occurrence details ────────────────────────────────────────────────────────


def get_current_occurrence(occurrence_id: Optional[str]) -> Optional[dict]:
    if not occurrence_id:
        return None
    from agent.models import MeetingOccurrence

    try:
        occ = MeetingOccurrence.objects.get(id=occurrence_id)
    except (MeetingOccurrence.DoesNotExist, ValueError):
        return None
    return {
        "id": str(occ.id),
        "title": occ.title,
        "summary": occ.summary,
        "started_at": _normalize_dt(occ.started_at),
        "attendees": list(occ.attendees or []),
    }


# ── Utility: lookup series_id from bot_id ─────────────────────────────────────


def resolve_series_id_for_bot(bot_id: Optional[str]) -> Optional[str]:
    """Resolve the series_id for a given bot via metadata or latest occurrence."""
    if not bot_id:
        return None
    try:
        from agent.models import MeetingOccurrence
        from bots.models import Bot

        bot = Bot.objects.filter(object_id=bot_id).only("metadata").first()
        if bot and bot.metadata and bot.metadata.get("series_id"):
            return str(bot.metadata["series_id"])
        occ = (
            MeetingOccurrence.objects.filter(bot__object_id=bot_id)
            .only("series_id")
            .order_by("-created_at")
            .first()
        )
        if occ:
            return str(occ.series_id)
    except Exception:
        log.exception("resolve_series_id_for_bot: lookup failed bot=%s", bot_id)
    return None


def resolve_occurrence_id_for_bot(bot_id: Optional[str]) -> Optional[str]:
    if not bot_id:
        return None
    try:
        from agent.models import MeetingOccurrence

        occ = (
            MeetingOccurrence.objects.filter(bot__object_id=bot_id)
            .only("id")
            .order_by("-created_at")
            .first()
        )
        if occ:
            return str(occ.id)
    except Exception:
        log.exception("resolve_occurrence_id_for_bot: failed bot=%s", bot_id)
    return None
