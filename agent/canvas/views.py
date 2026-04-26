"""
Canvas views — live bot video-feed page.

Routes (all public but keyed by bot_id as a capability token):
  GET /agent/canvas/<bot_id>/            — HTML page (rendered as bot's webcam)
  GET /agent/canvas/<bot_id>/stream      — SSE event stream for live updates
  GET /agent/canvas/<bot_id>/state.json  — Polling fallback
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone

log = logging.getLogger("agent.canvas")


def _resolve_bot_id(session_id: str) -> str | None:
    """Lookup real bot_id by metadata.bridge_session_id."""
    try:
        from bots.models import Bot

        bot = Bot.objects.filter(
            metadata__bridge_session_id=session_id
        ).only("object_id").first()
        return bot.object_id if bot else None
    except Exception:
        log.exception("_resolve_bot_id: lookup failed session=%s", session_id)
        return None


def session_canvas_view(request, session_id: str):
    bot_id = _resolve_bot_id(session_id) or session_id
    return render(request, "agent/canvas.html", {"bot_id": bot_id})


def session_canvas_state(request, session_id: str):
    bot_id = _resolve_bot_id(session_id)
    if not bot_id:
        # Render empty-but-valid state so the bot page still shows something
        return JsonResponse({"bot_id": session_id, "cursor": {"present": False}, "events": [], "actions": [], "thinking": False, "visual": None, "now": timezone.now().isoformat()}, safe=False)
    return JsonResponse(_snapshot_state(bot_id), safe=False)


def session_canvas_stream(request, session_id: str):
    bot_id = _resolve_bot_id(session_id) or session_id
    response = StreamingHttpResponse(
        _canvas_sse(bot_id),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def canvas_view(request, bot_id: str):
    """Render the canvas HTML shell. Client-side JS pulls live updates."""
    return render(request, "agent/canvas.html", {"bot_id": bot_id})


def canvas_state(request, bot_id: str):
    """Polling fallback — returns the current canvas state as JSON."""
    return JsonResponse(_snapshot_state(bot_id), safe=False)


def canvas_stream(request, bot_id: str):
    response = StreamingHttpResponse(
        _canvas_sse(bot_id),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ── Internal ─────────────────────────────────────────────────────────────────


def _snapshot_state(bot_id: str) -> dict:
    """
    Build the full snapshot shown on the canvas. Cheap enough to poll on
    every SSE tick (~1s).
    """
    from agent.models import ActionLogEntry, MeetingCursor, TranscriptEvent

    cursor = MeetingCursor.objects.filter(bot_id=bot_id).first()
    events = list(
        TranscriptEvent.objects.filter(bot_id=bot_id)
        .order_by("-event_time")[:25]
    )
    events.reverse()
    actions = list(
        ActionLogEntry.objects.filter(bot_id=bot_id)
        .order_by("-created_at")[:15]
    )
    actions.reverse()

    # Any in-flight action across the whole recent log is "thinking"
    thinking = any(a.status == "pending" for a in actions)

    # Latest chart artifact for the bot's series, if any
    visual = _latest_visual_for_bot(bot_id)

    voice_state = _voice_state(bot_id, cursor)

    return {
        "bot_id": bot_id,
        "now": timezone.now().isoformat(),
        "cursor": _cursor_payload(cursor),
        "voice_state": voice_state,
        "events": [
            {
                "t": e.event_time.isoformat() if e.event_time else None,
                "kind": e.kind,
                "speaker": e.speaker,
                "text": (e.text or "")[:280],
            }
            for e in events
        ],
        "actions": [
            {
                "t": a.created_at.isoformat(),
                "tool": a.tool_name,
                "status": a.status,
                "latency_ms": a.latency_ms,
                "error": (a.error_message or "")[:200],
            }
            for a in actions
        ],
        "thinking": thinking,
        "visual": visual,
    }


def _voice_state(bot_id: str, cursor) -> dict:
    """
    Compute a single high-level voice state for the indicator on the canvas.
    Order of precedence:
      - "asleep" — user said sleep phrase (sticky Redis flag)
      - "listening" — audio gate currently open
      - "idle" — gate closed, no sleep flag (default-pre-session-start)
    """
    suspended = False
    try:
        from agent.live_session.signals import is_voice_suspended
        suspended = is_voice_suspended(bot_id)
    except Exception:
        suspended = False
    gate_open = bool(cursor and cursor.audio_gate_open)
    if suspended:
        label, color = "ASLEEP", "red"
    elif gate_open:
        label, color = "LISTENING", "green"
    else:
        label, color = "IDLE", "gray"
    return {
        "label": label,
        "color": color,
        "suspended": suspended,
        "gate_open": gate_open,
        "reason": (cursor.audio_gate_reason if cursor else "") or "",
    }


def _cursor_payload(cursor) -> dict:
    if cursor is None:
        return {"present": False}
    return {
        "present": True,
        "cursor_event_time": cursor.cursor_event_time.isoformat() if cursor.cursor_event_time else None,
        "last_turn_at": cursor.last_turn_at.isoformat() if cursor.last_turn_at else None,
        "audio_gate_open": bool(cursor.audio_gate_open),
        "audio_gate_reason": cursor.audio_gate_reason or "",
        "total_cost_usd": str(cursor.total_cost_usd or 0),
        "budget_cap_usd": str(cursor.budget_cap_usd or 0),
        "budget_exceeded": bool(cursor.budget_exceeded),
    }


def _latest_visual_for_bot(bot_id: str) -> dict | None:
    from agent.models import Artifact, MeetingOccurrence

    try:
        occ = (
            MeetingOccurrence.objects.filter(bot__object_id=bot_id)
            .only("series_id")
            .order_by("-created_at")
            .first()
        )
        series_id = str(occ.series_id) if occ else None
        qs = Artifact.objects.filter(type="chart", is_deleted=False)
        if series_id:
            qs = qs.filter(series_id=series_id)
        art = qs.order_by("-updated_at").first()
        if not art:
            return None
        spec = None
        try:
            spec = json.loads(art.content or "{}")
        except Exception:
            spec = None
        return {
            "artifact_id": str(art.id),
            "title": art.title,
            "spec": spec,
        }
    except Exception:
        log.exception("_latest_visual_for_bot: failed bot=%s", bot_id)
        return None


def _canvas_sse(bot_id: str):
    """Poll-based SSE — 0.4s tick so the web canvas updates feel ~real-time."""
    yield _sse("hello", {"bot_id": bot_id, "at": timezone.now().isoformat()})
    last_heartbeat = time.monotonic()
    while True:
        try:
            state = _snapshot_state(bot_id)
            yield _sse("state", state)
        except Exception:
            log.exception("_canvas_sse: snapshot failed bot=%s", bot_id)
        if time.monotonic() - last_heartbeat >= 15:
            yield _sse("heartbeat", {"t": timezone.now().isoformat()})
            last_heartbeat = time.monotonic()
        time.sleep(0.4)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
