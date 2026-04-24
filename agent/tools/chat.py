"""
Chat tools — send/read Google Meet chat messages via Attendee's bot chat API.
"""
from __future__ import annotations

import logging

from django.conf import settings

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.chat")


def _send_chat_message(inp: dict, ctx: dict) -> dict:
    import requests

    bot_id = ctx.get("bot_id")
    text = (inp.get("text") or "").strip()
    to = (inp.get("to") or "everyone").strip()
    if not bot_id:
        return {"error": "no bot_id (not in a live meeting)"}
    if not text:
        return {"error": "text required"}
    if to not in {"everyone", "specific_user", "everyone_but_host"}:
        return {"error": f"invalid 'to' value: {to}"}

    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    app_url = getattr(settings, "AGENT_APP_URL", "")
    if not api_key or not app_url:
        return {"error": "ATTENDEE_API_KEY / AGENT_APP_URL not configured"}

    payload: dict = {"message": text, "to": to}
    if to == "specific_user" and inp.get("to_user_uuid"):
        payload["to_user_uuid"] = inp["to_user_uuid"]

    try:
        resp = requests.post(
            f"{app_url}/api/v1/bots/{bot_id}/send_chat_message",
            json=payload,
            headers={"Authorization": f"Token {api_key}"},
            timeout=10,
        )
    except Exception as exc:
        log.exception("send_chat_message: request failed bot=%s", bot_id)
        return {"error": f"request failed: {exc}"}

    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    # Mirror the chat message into TranscriptEvent so the agent "sees" its own voice
    try:
        from django.utils import timezone

        from agent.models import TranscriptEvent

        TranscriptEvent.objects.create(
            bot_id=bot_id,
            kind="chat",
            event_time=timezone.now(),
            speaker=getattr(settings, "AGENT_NAME", "Clever Star"),
            text=text,
            utterance_ref=f"agent-chat:{bot_id}:{timezone.now().timestamp()}",
            raw={"self": True},
        )
    except Exception:
        log.exception("send_chat_message: failed to log self event bot=%s", bot_id)

    return {"sent": True, "text": text[:200]}


def _read_recent_chat(inp: dict, ctx: dict) -> dict:
    from agent.models import TranscriptEvent

    bot_id = ctx.get("bot_id")
    if not bot_id:
        return {"error": "no bot_id"}
    limit = min(int(inp.get("limit", 10) or 10), 50)
    qs = (
        TranscriptEvent.objects.filter(bot_id=bot_id, kind="chat")
        .order_by("-event_time")[:limit]
    )
    msgs = [
        {
            "speaker": e.speaker,
            "text": e.text,
            "event_time": e.event_time.isoformat() if e.event_time else None,
        }
        for e in qs
    ]
    msgs.reverse()
    return {"messages": msgs, "count": len(msgs)}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="send_chat_message",
        description=(
            "Post a message to the Google Meet chat. Use when the agent should "
            "respond in chat rather than by voice. Keep it short."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "text": {"type": "string", "description": "Chat message text."},
                "to": {
                    "type": "string",
                    "description": "Audience: everyone | specific_user | everyone_but_host.",
                    "enum": ["everyone", "specific_user", "everyone_but_host"],
                },
                "to_user_uuid": {
                    "type": "string",
                    "description": "Target user UUID, required when to=specific_user.",
                },
            },
            required=["text"],
        ),
        handler=_send_chat_message,
    ),
    ToolDefinition(
        name="read_recent_chat",
        description="Read the most recent chat messages from this meeting.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "limit": {
                    "type": "integer",
                    "description": "Max messages (default 10, max 50).",
                },
            },
        ),
        handler=_read_recent_chat,
    ),
]
