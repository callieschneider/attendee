"""Meeting occurrence and calendar tools."""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.meetings")


def _get_recent_occurrences(inp: dict, ctx: dict) -> dict:
    from agent.models import MeetingOccurrence, MeetingSeries

    series_id = inp.get("series_id") or ctx.get("series_id")
    limit = min(int(inp.get("limit", 5)), 20)

    qs = MeetingOccurrence.objects.exclude(summary="").order_by("-started_at")
    if series_id:
        qs = qs.filter(series_id=series_id)

    results = []
    for occ in qs[:limit]:
        results.append({
            "id": str(occ.id),
            "title": occ.title or f"Meeting {occ.started_at:%Y-%m-%d}" if occ.started_at else "Untitled",
            "summary": occ.summary[:500],
            "started_at": occ.started_at.isoformat() if occ.started_at else None,
            "attendees": occ.attendees,
        })
    return {"occurrences": results, "count": len(results)}


def _get_occurrence_transcript(inp: dict, ctx: dict) -> dict:
    from agent.models import MeetingOccurrence

    occurrence_id = inp.get("occurrence_id") or ctx.get("occurrence_id")
    if not occurrence_id:
        return {"error": "occurrence_id required"}

    try:
        occ = MeetingOccurrence.objects.get(id=occurrence_id)
    except MeetingOccurrence.DoesNotExist:
        return {"error": f"occurrence {occurrence_id} not found"}

    return {
        "id": str(occ.id),
        "title": occ.title,
        "transcript": occ.transcript_text[:8000],
        "summary": occ.summary,
        "started_at": occ.started_at.isoformat() if occ.started_at else None,
    }


def _get_meeting_notes(inp: dict, ctx: dict) -> dict:
    from agent.models import MeetingOccurrence, MeetingTask

    occurrence_id = inp.get("occurrence_id") or ctx.get("occurrence_id")
    if not occurrence_id:
        return {"error": "occurrence_id required"}

    try:
        occ = MeetingOccurrence.objects.get(id=occurrence_id)
    except MeetingOccurrence.DoesNotExist:
        return {"error": f"occurrence {occurrence_id} not found"}

    tasks = list(
        MeetingTask.objects.filter(occurrence=occ).values("title", "assignee", "status")
    )
    return {
        "id": str(occ.id),
        "title": occ.title,
        "summary": occ.summary,
        "tasks": tasks,
        "attendees": occ.attendees,
    }


def _list_upcoming_meetings(inp: dict, ctx: dict) -> dict:
    """List upcoming calendar events with Google Meet URLs."""
    from bots.models import CalendarEvent
    from agent.series_manager import assign_series
    from django.utils import timezone
    import datetime

    days = min(int(inp.get("days", 7)), 30)
    now = timezone.now()
    until = now + datetime.timedelta(days=days)

    events = (
        CalendarEvent.objects.filter(
            start_time__gte=now,
            start_time__lte=until,
            is_deleted=False,
        )
        .exclude(meeting_url__isnull=True)
        .exclude(meeting_url="")
        .order_by("start_time")[:20]
    )

    results = []
    for evt in events:
        try:
            series = assign_series(evt)
            series_name = series.title
            series_id = str(series.id)
        except Exception:
            series_name = "Inbox"
            series_id = ""
        results.append({
            "event_id": evt.object_id,
            "title": evt.name or "Untitled",
            "start_time": evt.start_time.isoformat(),
            "end_time": evt.end_time.isoformat() if evt.end_time else None,
            "meeting_url": evt.meeting_url,
            "series": series_name,
            "series_id": series_id,
            "attendees": evt.attendees or [],
        })

    return {"upcoming_meetings": results, "count": len(results), "days_ahead": days}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_recent_occurrences",
        description="Get recent meeting occurrences with their summaries. Useful for reviewing what happened in recent meetings.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "series_id": {"type": "string", "description": "Filter by meeting series UUID (optional)"},
                "limit": {"type": "integer", "description": "Max number of occurrences to return (default 5, max 20)"},
            },
        ),
        handler=_get_recent_occurrences,
    ),
    ToolDefinition(
        name="get_occurrence_transcript",
        description="Get the full transcript for a specific meeting occurrence.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "occurrence_id": {"type": "string", "description": "UUID of the MeetingOccurrence"},
            },
            required=["occurrence_id"],
        ),
        handler=_get_occurrence_transcript,
    ),
    ToolDefinition(
        name="get_meeting_notes",
        description="Get the summary and extracted action items for a specific meeting.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "occurrence_id": {"type": "string", "description": "UUID of the MeetingOccurrence"},
            },
            required=["occurrence_id"],
        ),
        handler=_get_meeting_notes,
    ),
    ToolDefinition(
        name="list_upcoming_meetings",
        description="List upcoming scheduled meetings (from Google Calendar) with their Meet URLs and assigned series. Use this to answer 'what meetings do I have?' or to help schedule bot attendance.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "days": {"type": "integer", "description": "How many days ahead to look (default 7, max 30)"},
            },
        ),
        handler=_list_upcoming_meetings,
    ),
]
