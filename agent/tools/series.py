"""Meeting series / project tools."""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.series")


def _get_series_context_bundle(inp: dict, ctx: dict) -> dict:
    """Returns a comprehensive context bundle for a series: recent meetings, open tasks, pinned context."""
    from agent.models import MeetingSeries, MeetingOccurrence, Task, ContextItem

    series_id = inp.get("series_id") or ctx.get("series_id")
    if not series_id:
        return {"error": "series_id required"}

    try:
        series = MeetingSeries.objects.get(id=series_id)
    except MeetingSeries.DoesNotExist:
        return {"error": f"series {series_id} not found"}

    recent = list(
        MeetingOccurrence.objects.filter(series=series)
        .exclude(summary="")
        .order_by("-started_at")[:5]
        .values("id", "title", "summary", "started_at")
    )
    for r in recent:
        r["id"] = str(r["id"])
        if r["started_at"]:
            r["started_at"] = r["started_at"].isoformat()
        r["summary"] = r["summary"][:400]

    open_tasks = list(
        Task.objects.filter(series=series)
        .exclude(status__in=["done", "cancelled"])
        .order_by("priority", "due_date")[:15]
        .values("id", "title", "status", "priority", "due_date", "owner")
    )
    for t in open_tasks:
        t["id"] = str(t["id"])
        if t["due_date"]:
            t["due_date"] = t["due_date"].isoformat()

    pins = list(
        ContextItem.objects.filter(series=series, is_pinned=True)
        .order_by("order")[:10]
        .values("label", "content")
    )

    return {
        "series": {
            "id": str(series.id),
            "title": series.title,
            "description": series.description,
            "tags": series.tags,
        },
        "recent_meetings": recent,
        "open_tasks": open_tasks,
        "pinned_context": pins,
    }


def _list_series(inp: dict, ctx: dict) -> dict:
    from agent.models import MeetingSeries

    qs = MeetingSeries.objects.filter(is_active=True).order_by("-updated_at")[:20]
    return {
        "series": [
            {"id": str(s.id), "title": s.title, "description": s.description[:200]}
            for s in qs
        ]
    }


def _assign_meeting_to_series(inp: dict, ctx: dict) -> dict:
    """Move a MeetingOccurrence to a different MeetingSeries."""
    from agent.models import MeetingOccurrence, MeetingSeries

    occurrence_id = inp.get("occurrence_id")
    series_id = inp.get("series_id")
    if not occurrence_id or not series_id:
        return {"error": "occurrence_id and series_id required"}

    try:
        occ = MeetingOccurrence.objects.get(id=occurrence_id)
    except MeetingOccurrence.DoesNotExist:
        return {"error": f"occurrence {occurrence_id} not found"}

    try:
        series = MeetingSeries.objects.get(id=series_id)
    except MeetingSeries.DoesNotExist:
        return {"error": f"series {series_id} not found"}

    old_series = occ.series.title
    occ.series = series
    occ.save(update_fields=["series"])

    return {"updated": True, "occurrence_id": occurrence_id, "old_series": old_series, "new_series": series.title}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_series_context_bundle",
        description="Get a comprehensive context bundle for a meeting series including recent meeting summaries, open tasks, and pinned context items. Use this to quickly understand the current state of a project or meeting series.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "series_id": {"type": "string", "description": "UUID of the MeetingSeries"},
            },
            required=["series_id"],
        ),
        handler=_get_series_context_bundle,
    ),
    ToolDefinition(
        name="list_series",
        description="List all active meeting series. Use this to find the series_id for a specific project.",
        input_schema=ToolSchema(
            type="object",
            properties={},
        ),
        handler=_list_series,
    ),
    ToolDefinition(
        name="assign_meeting_to_series",
        description="Move a MeetingOccurrence to a different MeetingSeries. Use this when a meeting was automatically assigned to the wrong series or to Inbox.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "occurrence_id": {"type": "string", "description": "UUID of the MeetingOccurrence to move"},
                "series_id": {"type": "string", "description": "UUID of the target MeetingSeries"},
            },
            required=["occurrence_id", "series_id"],
        ),
        handler=_assign_meeting_to_series,
    ),
]
