"""
Voice Context Pump — pushes short text briefings into the live Gemini Live
session via `realtimeInput.text` (NEVER `clientContent.turns`, which would
terminate the session per the abstraKt landmine).

Callable from the web/worker process (publishes to Redis), consumed by the
bridge process which owns the WS connection.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from asgiref.sync import sync_to_async

from .signals import publish_voice_context

log = logging.getLogger("agent.live_session.voice_pump")


def enqueue_voice_briefing(bot_id: str, text: str, turn_id: Optional[str] = None) -> bool:
    """
    Publish a briefing to the live session. Called from the worker process
    (Turn Processor). Writes a VoiceContextPush row for auditability.
    Returns True if published; False on failure.
    """
    if not bot_id or not text:
        return False

    # Audit row
    try:
        from agent.models import VoiceContextPush

        VoiceContextPush.objects.create(
            bot_id=bot_id,
            text=text,
            triggered_by_turn_id=turn_id,
        )
    except Exception:
        log.exception("enqueue_voice_briefing: audit write failed bot=%s", bot_id)

    return publish_voice_context(bot_id, text, turn_id)


async def push_voice_context(live_ws, text: str) -> bool:
    """
    Send a text briefing frame to an open Gemini Live WebSocket.
    MUST use realtimeInput.text (clientContent.turns terminates the session).
    """
    if not live_ws or not text:
        return False
    try:
        await live_ws.send(json.dumps({"realtimeInput": {"text": text}}))
        return True
    except Exception:
        log.exception("push_voice_context: WS send failed")
        return False
