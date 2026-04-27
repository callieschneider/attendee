"""
State helpers for the canvas_v2 app.

These functions are the agent's only interface to canvas state. They:
- Mutate `agent.models.CanvasState` rows
- Publish a Redis pubsub event so connected clients see the change live
- Are idempotent / safe under concurrent calls
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

log = logging.getLogger("agent.canvas_v2.state")


VALID_TABS = ("dashboard", "notes", "tasks", "focus", "debug")


# ── Redis client ──────────────────────────────────────────────────────────────


_REDIS = None


def _redis():
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    try:
        import redis
    except Exception:
        log.exception("canvas_v2: redis package missing")
        return None
    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    try:
        _REDIS = redis.from_url(url, decode_responses=True)
        return _REDIS
    except Exception:
        log.exception("canvas_v2: redis.from_url failed")
        return None


def publish_state_event(bot_id: str, event: dict) -> None:
    """Publish a state-change notification on the per-bot Redis channel."""
    r = _redis()
    if r is None:
        return
    try:
        r.publish(f"canvas:state:{bot_id}", json.dumps(event))
    except Exception:
        log.exception("canvas_v2: publish_state_event failed bot=%s", bot_id)


def publish_stream_chunk(bot_id: str, tab: str, payload: dict) -> None:
    """Publish a streaming-text chunk on the per-bot/per-tab channel."""
    r = _redis()
    if r is None:
        return
    try:
        r.publish(f"canvas:stream:{bot_id}:{tab}", json.dumps(payload))
    except Exception:
        log.exception("canvas_v2: publish_stream_chunk failed bot=%s tab=%s", bot_id, tab)


# ── DB mutation helpers ───────────────────────────────────────────────────────


def _get_or_create_state(bot_id: str):
    from agent.models import CanvasState
    from bots.models import Bot

    obj = CanvasState.objects.filter(bot_id=bot_id).first()
    if obj is not None:
        return obj
    bot = Bot.objects.filter(object_id=bot_id).only("object_id").first()
    if bot is None:
        return None
    return CanvasState.objects.create(bot=bot)


def navigate(bot_id: str, tab: str, *, source: str = "agent") -> dict:
    if tab not in VALID_TABS:
        return {"error": f"invalid tab: {tab}"}
    with transaction.atomic():
        state = _get_or_create_state(bot_id)
        if state is None:
            return {"error": "no bot for that id"}
        if state.user_driving and source == "agent":
            log.info(
                "canvas_v2: ignoring agent navigate while user driving bot=%s",
                bot_id,
            )
            return {"ok": True, "ignored": True, "reason": "user_driving"}
        state.active_tab = tab
        state.save(update_fields=["active_tab", "updated_at"])
    publish_state_event(bot_id, {"event": "navigate", "tab": tab, "source": source})
    return {"ok": True, "tab": tab}


def update_notes(bot_id: str, *, text: str, operation: str = "append") -> dict:
    if not text:
        return {"error": "text required"}
    operation = (operation or "append").lower()
    if operation not in ("append", "replace"):
        return {"error": "operation must be 'append' or 'replace'"}
    with transaction.atomic():
        state = _get_or_create_state(bot_id)
        if state is None:
            return {"error": "no bot for that id"}
        if operation == "replace":
            state.notes_md = text
        else:
            sep = "\n\n" if state.notes_md else ""
            state.notes_md = (state.notes_md + sep + text)[:200_000]  # ~200KB cap
        state.save(update_fields=["notes_md", "updated_at"])
    publish_state_event(bot_id, {
        "event": "notes",
        "operation": operation,
        "text": text,
    })
    return {"ok": True, "len": len(state.notes_md), "operation": operation}


def update_dashboard(bot_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"error": "payload must be an object"}
    with transaction.atomic():
        state = _get_or_create_state(bot_id)
        if state is None:
            return {"error": "no bot for that id"}
        merged = dict(state.dashboard_payload or {})
        merged.update(payload)
        state.dashboard_payload = merged
        state.save(update_fields=["dashboard_payload", "updated_at"])
    publish_state_event(bot_id, {"event": "dashboard", "payload": merged})
    return {"ok": True, "payload": merged}


def update_focus(
    bot_id: str,
    *,
    session_id: str,
    text: str,
    done: bool,
) -> None:
    """Called by think_deep on every chunk + on completion."""
    state = _get_or_create_state(bot_id)
    if state is None:
        return
    state.focus_session_id = session_id
    state.focus_text = text
    state.focus_done = bool(done)
    if state.active_tab not in ("focus",) and not state.user_driving:
        state.active_tab = "focus"
    state.save(update_fields=[
        "focus_session_id", "focus_text", "focus_done", "active_tab",
        "updated_at",
    ])
    publish_state_event(bot_id, {
        "event": "focus",
        "session_id": session_id,
        "done": done,
        "tab": state.active_tab,
    })


def set_user_driving(bot_id: str, driving: bool) -> None:
    state = _get_or_create_state(bot_id)
    if state is None:
        return
    state.user_driving = bool(driving)
    state.user_driving_since = timezone.now() if driving else None
    state.save(update_fields=["user_driving", "user_driving_since", "updated_at"])
    publish_state_event(bot_id, {"event": "user_driving", "driving": bool(driving)})


# ── Snapshot serialization ────────────────────────────────────────────────────


def snapshot(bot_id: str) -> dict:
    """
    Serialize the full canvas state for a freshly-joined client. Pulls in
    related read-only data (open tasks, recent transcript, action log) so
    the canvas can render without a flurry of follow-up requests.
    """
    from agent.models import (
        ActionLogEntry, CanvasState, MeetingCursor, MeetingTask,
        MeetingOccurrence, TranscriptEvent, Task,
    )

    state = CanvasState.objects.filter(bot_id=bot_id).first()
    cursor = MeetingCursor.objects.filter(bot_id=bot_id).first()

    occ = (
        MeetingOccurrence.objects.filter(bot__object_id=bot_id)
        .select_related("series")
        .order_by("-created_at")
        .first()
    )
    series = occ.series if occ else None

    open_statuses = ["backlog", "todo", "in_progress", "in_review"]
    if series is not None:
        tasks_qs = (
            Task.objects.filter(series=series, status__in=open_statuses)
            .order_by("-updated_at")[:25]
        )
    else:
        tasks_qs = (
            Task.objects.filter(status__in=open_statuses)
            .order_by("-updated_at")[:25]
        )
    open_tasks = [
        {
            "id": str(t.id),
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "owner": t.owner or "",
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in tasks_qs
    ]

    meeting_tasks = []
    if occ is not None:
        for t in MeetingTask.objects.filter(occurrence=occ).order_by("-id")[:20]:
            meeting_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "assignee": t.assignee or "",
                "status": t.status,
            })

    events = list(
        TranscriptEvent.objects.filter(bot_id=bot_id)
        .order_by("-event_time")[:60]
    )
    events.reverse()
    transcript = [
        {
            "t": e.event_time.isoformat() if e.event_time else None,
            "kind": e.kind,
            "speaker": e.speaker or "",
            "text": (e.text or "")[:600],
            "is_self": bool((e.raw or {}).get("self_utterance")),
        }
        for e in events
    ]

    actions = list(
        ActionLogEntry.objects.filter(bot_id=bot_id)
        .order_by("-created_at")[:25]
    )
    actions.reverse()
    action_log = [
        {
            "t": a.created_at.isoformat(),
            "tool": a.tool_name,
            "status": a.status,
            "latency_ms": a.latency_ms,
            "error": (a.error_message or "")[:200],
        }
        for a in actions
    ]

    voice = _voice_state(bot_id, cursor)

    return {
        "bot_id": bot_id,
        "now": timezone.now().isoformat(),
        "active_tab": (state.active_tab if state else "dashboard"),
        "user_driving": bool(state and state.user_driving),
        "notes_md": (state.notes_md if state else ""),
        "focus": {
            "session_id": (state.focus_session_id if state else ""),
            "text": (state.focus_text if state else ""),
            "done": bool(state.focus_done if state else True),
        },
        "dashboard": (state.dashboard_payload if state else {}) or {},
        "tasks": open_tasks,
        "meeting_tasks": meeting_tasks,
        "transcript": transcript,
        "action_log": action_log,
        "voice": voice,
        "agent_name": _agent_name(),
        "series": (
            {"id": str(series.id), "title": series.title} if series else None
        ),
    }


def _agent_name() -> str:
    try:
        from django.conf import settings
        return getattr(settings, "AGENT_NAME", "Clever Star") or "Clever Star"
    except Exception:
        return "Clever Star"


def _voice_state(bot_id: str, cursor) -> dict:
    suspended = False
    try:
        from agent.live_session.signals import is_voice_suspended
        suspended = is_voice_suspended(bot_id)
    except Exception:
        suspended = False
    gate_open = bool(cursor and cursor.audio_gate_open)
    if suspended:
        label = "asleep"
    elif gate_open:
        label = "listening"
    else:
        label = "idle"
    return {"label": label, "suspended": suspended, "gate_open": gate_open}
