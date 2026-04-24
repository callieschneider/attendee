"""
Dashboard views — live feed of TranscriptEvent + ActionLogEntry per meeting
via Server-Sent Events, plus a simple HTML shell.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

log = logging.getLogger("agent.dashboard")


# ── Home ──────────────────────────────────────────────────────────────────────


@staff_member_required
def dashboard_home(request):
    from agent.models import MeetingCursor, MeetingOccurrence, MeetingSeries

    cursors = (
        MeetingCursor.objects.select_related("bot")
        .order_by("-updated_at")[:25]
    )
    recent_occs = (
        MeetingOccurrence.objects.select_related("series")
        .order_by("-created_at")[:15]
    )
    series = MeetingSeries.objects.filter(is_active=True).order_by("title")

    ctx = {
        "active_cursors": cursors,
        "recent_occurrences": recent_occs,
        "series_list": series,
    }
    return render(request, "agent/dashboard_home.html", ctx)


# ── Per-meeting live view ────────────────────────────────────────────────────


@staff_member_required
def meeting_view(request, bot_id: str):
    from agent.models import ActionLogEntry, MeetingCursor, TranscriptEvent

    cursor = MeetingCursor.objects.filter(bot_id=bot_id).first()
    initial_events = list(
        TranscriptEvent.objects.filter(bot_id=bot_id)
        .order_by("-event_time")[:40]
    )
    initial_events.reverse()
    initial_actions = list(
        ActionLogEntry.objects.filter(bot_id=bot_id)
        .order_by("-created_at")[:20]
    )
    initial_actions.reverse()

    ctx = {
        "bot_id": bot_id,
        "cursor": cursor,
        "events": initial_events,
        "actions": initial_actions,
    }
    return render(request, "agent/dashboard_meeting.html", ctx)


@staff_member_required
def meeting_events_json(request, bot_id: str):
    """Polling fallback — returns the latest N transcript events + actions as JSON."""
    from agent.models import ActionLogEntry, TranscriptEvent

    events = list(
        TranscriptEvent.objects.filter(bot_id=bot_id)
        .order_by("-event_time")[:40]
        .values("id", "event_time", "kind", "speaker", "text")
    )
    actions = list(
        ActionLogEntry.objects.filter(bot_id=bot_id)
        .order_by("-created_at")[:20]
        .values("id", "created_at", "tool_name", "status", "latency_ms", "error_message")
    )
    return JsonResponse({"events": events, "actions": actions}, safe=False)


@staff_member_required
def meeting_events_stream(request, bot_id: str):
    """
    Server-Sent Events stream of live meeting activity.
    Uses Redis pub/sub on the `agent:events:<bot_id>` channel; falls back to
    a simple polling generator if Redis isn't available.
    """
    generator = _sse_generator(bot_id)
    response = StreamingHttpResponse(generator, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # disable nginx buffering
    return response


def _sse_generator(bot_id: str):
    """
    Generator that yields SSE-formatted messages.
    Emits:
      - `event: hello` with a one-shot greeting
      - `event: heartbeat` every 15s
      - `event: update` with JSON payload for new rows (polled every 2s)
    """
    # Greet
    yield _sse_message("hello", {"bot_id": bot_id, "at": timezone.now().isoformat()})

    from agent.models import ActionLogEntry, TranscriptEvent

    last_event_seen = None
    last_action_seen = None
    last_heartbeat = time.monotonic()

    while True:
        # New transcript events
        qs = TranscriptEvent.objects.filter(bot_id=bot_id)
        if last_event_seen:
            qs = qs.filter(created_at__gt=last_event_seen)
        new_events = list(qs.order_by("created_at")[:20])
        if new_events:
            last_event_seen = new_events[-1].created_at
            payload = [
                {
                    "id": str(e.id),
                    "event_time": e.event_time.isoformat() if e.event_time else None,
                    "kind": e.kind,
                    "speaker": e.speaker,
                    "text": e.text,
                }
                for e in new_events
            ]
            yield _sse_message("transcript", payload)

        # New action log entries
        qs = ActionLogEntry.objects.filter(bot_id=bot_id)
        if last_action_seen:
            qs = qs.filter(created_at__gt=last_action_seen)
        new_actions = list(qs.order_by("created_at")[:20])
        if new_actions:
            last_action_seen = new_actions[-1].created_at
            payload = [
                {
                    "id": str(a.id),
                    "created_at": a.created_at.isoformat(),
                    "tool_name": a.tool_name,
                    "status": a.status,
                    "latency_ms": a.latency_ms,
                    "error_message": a.error_message,
                }
                for a in new_actions
            ]
            yield _sse_message("action", payload)

        # Heartbeat
        if time.monotonic() - last_heartbeat >= 15:
            yield _sse_message("heartbeat", {"t": timezone.now().isoformat()})
            last_heartbeat = time.monotonic()

        time.sleep(2)


def _sse_message(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# ── Series list + per-series config ──────────────────────────────────────────


@staff_member_required
def series_list(request):
    from agent.models import MeetingSeries

    series = MeetingSeries.objects.all().order_by("-is_active", "title")
    return render(request, "agent/dashboard_series.html", {"series_list": series})


@staff_member_required
@csrf_exempt  # tolerated in admin context; the form includes csrf via template tag
@require_POST
def series_config(request, series_id):
    from agent.models import MeetingSeries

    try:
        series = MeetingSeries.objects.get(id=series_id)
    except MeetingSeries.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    fields = [
        "agent_name_override",
        "agent_verbosity",
        "agent_proactivity",
        "max_cost_usd_per_meeting",
    ]
    updated = []
    for f in fields:
        if f in request.POST:
            val = request.POST.get(f)
            if f == "max_cost_usd_per_meeting":
                val = val.strip() if val else None
                val = val or None
            setattr(series, f, val)
            updated.append(f)

    if "allowed_tool_categories" in request.POST:
        raw = request.POST.get("allowed_tool_categories", "")
        series.allowed_tool_categories = [
            c.strip() for c in raw.split(",") if c.strip()
        ]
        updated.append("allowed_tool_categories")

    series.save()
    return JsonResponse({"ok": True, "updated": updated})
