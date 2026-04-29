"""
Bot lifecycle tools — leave the meeting / respawn into a fresh bot.

leave_meeting:
    Politely exit the call. POSTs to Attendee's existing
    /api/v1/bots/<id>/leave endpoint.

respawn_bot:
    Spawn a new bot in the same meeting URL while preserving the
    canvas state (notes_md, dashboard_payload, focus_text, browser
    URL, active_tab) from the current bot. The user gets a NEW
    canvas URL since the bot_id is new, but the contents persist.
    Useful when the agent feels stuck (Gemini session degraded,
    the bot got muted, etc.) and a fresh start helps.
"""
from __future__ import annotations

import logging

from django.conf import settings

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.bot_lifecycle")


def _api_creds() -> tuple[str, str] | dict:
    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    app_url = getattr(settings, "AGENT_APP_URL", "")
    if not api_key or not app_url:
        return {"error": "ATTENDEE_API_KEY / AGENT_APP_URL not configured"}
    return (api_key, app_url.rstrip("/"))


def _leave_meeting(inp: dict, ctx: dict) -> dict:
    import requests as req

    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    creds = _api_creds()
    if isinstance(creds, dict):
        return creds
    api_key, app_url = creds

    # Mark this exit as intentional in Redis so the auto-respawn
    # supervisor doesn't re-spawn us when the bot ends.
    _set_intentional_leave(bot_id)

    try:
        resp = req.post(
            f"{app_url}/api/v1/bots/{bot_id}/leave",
            headers={"Authorization": f"Token {api_key}"},
            timeout=15,
        )
    except Exception as exc:
        log.exception("leave_meeting: request failed bot=%s", bot_id)
        return {"error": f"request failed: {exc}"}
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    return {"ok": True, "bot_id": bot_id}


# ── Intentional-leave marker (used by auto-respawn supervisor) ─────────────


def _intent_redis():
    import os
    try:
        import redis
    except Exception:
        return None
    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    try:
        return redis.from_url(url, decode_responses=True)
    except Exception:
        return None


def _set_intentional_leave(bot_id: str, ttl_seconds: int = 300) -> None:
    """leave_meeting and respawn_bot mark themselves so we don't auto-respawn."""
    r = _intent_redis()
    if r is None:
        return
    try:
        r.set(f"agent:bot:intentional_leave:{bot_id}", "1", ex=ttl_seconds)
    except Exception:
        pass


def is_intentional_leave(bot_id: str) -> bool:
    r = _intent_redis()
    if r is None:
        return False
    try:
        return bool(r.get(f"agent:bot:intentional_leave:{bot_id}"))
    except Exception:
        return False


def _respawn_bot(inp: dict, ctx: dict) -> dict:
    """
    Create a new bot in the same meeting and copy the current bot's
    canvas state across. Best-effort — failures at any step return a
    descriptive error rather than silently leaving the user with two
    bots in the call.
    """
    import requests as req

    old_bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not old_bot_id:
        return {"error": "bot_id required"}
    creds = _api_creds()
    if isinstance(creds, dict):
        return creds
    api_key, app_url = creds

    # Look up the meeting URL from the current bot.
    try:
        from bots.models import Bot
        old_bot = Bot.objects.filter(object_id=old_bot_id).first()
    except Exception as exc:
        return {"error": f"DB lookup failed: {exc}"}
    if old_bot is None:
        return {"error": f"current bot {old_bot_id} not found"}
    meeting_url = old_bot.meeting_url
    if not meeting_url:
        return {"error": "current bot has no meeting_url"}

    bot_name = (inp.get("bot_name") or "").strip() or getattr(
        settings, "AGENT_NAME", "Clever Star"
    )

    # Spawn the new bot using our internal create_meeting_bot endpoint
    # (it sets bridge_session_id correctly).
    try:
        spawn_resp = req.post(
            f"{app_url}/agent/api/create-meeting-bot",
            json={"meeting_url": meeting_url, "bot_name": bot_name},
            timeout=30,
        )
    except Exception as exc:
        log.exception("respawn_bot: spawn failed")
        return {"error": f"spawn failed: {exc}"}
    if spawn_resp.status_code >= 400:
        return {"error": f"spawn HTTP {spawn_resp.status_code}: {spawn_resp.text[:200]}"}
    new = spawn_resp.json()
    new_bot_id = new.get("id")
    if not new_bot_id:
        return {"error": "spawn response had no bot id"}

    # Copy CanvasState across so notes / dashboard / focus / browser
    # URL all persist into the new session. Must happen BEFORE the new
    # bot's CanvasState is auto-created with defaults — race window is
    # fine because we're inside one DB transaction and the new bot
    # hasn't finished joining yet.
    try:
        from django.db import transaction
        from agent.models import CanvasState

        with transaction.atomic():
            old_state = CanvasState.objects.filter(bot_id=old_bot_id).first()
            new_state, _ = CanvasState.objects.get_or_create(bot_id=new_bot_id)
            if old_state:
                new_state.active_tab = old_state.active_tab
                new_state.notes_md = old_state.notes_md
                new_state.focus_session_id = old_state.focus_session_id
                new_state.focus_text = old_state.focus_text
                new_state.focus_done = old_state.focus_done
                new_state.dashboard_payload = old_state.dashboard_payload
                new_state.browser_url = old_state.browser_url
                new_state.browser_title = old_state.browser_title
                new_state.save()
    except Exception:
        log.exception("respawn_bot: canvas-state copy failed old=%s new=%s", old_bot_id, new_bot_id)
        # Continue — better to have a respawned bot with empty canvas
        # than to abort and end up with two bots in the meeting.

    # Tell the old bot to leave AFTER the new spawn is in flight, so
    # there's continuity. Best-effort. Mark as intentional so the
    # auto-respawn supervisor doesn't fire ANOTHER respawn for us.
    _set_intentional_leave(old_bot_id)
    try:
        req.post(
            f"{app_url}/api/v1/bots/{old_bot_id}/leave",
            headers={"Authorization": f"Token {api_key}"},
            timeout=10,
        )
    except Exception:
        log.exception("respawn_bot: old-bot leave failed (non-fatal) bot=%s", old_bot_id)

    # Publish a `moved` event on the OLD bot's canvas channel so any
    # browser tab still sitting on /agent/canvas/v2/<old>/ auto-
    # migrates to the new URL without the user touching anything. The
    # React app listens for this and does window.location.replace(...).
    try:
        from agent.canvas_v2 import state as _cstate
        new_canvas_url = f"{app_url}/agent/canvas/v2/{new_bot_id}/"
        _cstate.publish_state_event(old_bot_id, {
            "event": "moved",
            "new_bot_id": new_bot_id,
            "new_url": new_canvas_url,
        })
    except Exception:
        log.exception("respawn_bot: moved-event publish failed bot=%s", old_bot_id)

    new_canvas_url = f"{app_url}/agent/canvas/v2/{new_bot_id}/"

    # Build a stable per-meeting URL too — survives future respawns
    # without the agent having to call respawn_bot again. Extract the
    # last path component of the meeting URL.
    stable_url = ""
    try:
        from urllib.parse import urlparse
        path = urlparse(meeting_url).path.strip("/").split("/")[-1]
        if path:
            stable_url = f"{app_url}/agent/canvas/v2/m/{path}/"
    except Exception:
        pass

    return {
        "ok": True,
        "old_bot_id": old_bot_id,
        "new_bot_id": new_bot_id,
        "canvas_url": new_canvas_url,
        "stable_canvas_url": stable_url,
        "meeting_url": meeting_url,
        "note": (
            "The old canvas tab will auto-redirect to the new bot. "
            "Use stable_canvas_url for a link that survives future "
            "respawns. Speak/post the actual URL value, NOT the "
            "literal text 'canvas_url' or '[link]'."
        ),
    }


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="leave_meeting",
        description=(
            "Leave the call. Use when the user says 'you can go', 'thanks "
            "you can leave now', 'meeting's over', or similar. The bot "
            "exits gracefully — no goodbye speech needed beyond a short "
            "verbal acknowledgement before calling this tool."
        ),
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_leave_meeting,
    ),
    ToolDefinition(
        name="respawn_bot",
        description=(
            "Replace yourself with a fresh bot in the same meeting, "
            "carrying over the canvas state (notes, dashboard, focus, "
            "browser URL, active tab). Use when the user says 'reload "
            "yourself', 'restart', 'reset and try again' OR when you "
            "(via get_diagnostics) see your own session is degraded "
            "(many recent failures, browser crashed, gate stuck). "
            "Returns the NEW canvas URL — read it back to the user and "
            "post it in the meeting chat so they can switch tabs. The "
            "old bot leaves automatically right after the new one spawns."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "bot_name": {
                    "type": "string",
                    "description": "Optional name for the new bot (default uses AGENT_NAME).",
                },
            },
            required=[],
        ),
        handler=_respawn_bot,
    ),
]
