"""
Voice tool — `speak_via_voice` opens the audio gate and instructs Gemini Live
to speak the given text. Emits a Redis signal so the bridge process
actually drives the WebSocket.
"""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.voice")


def _speak_via_voice(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id")
    text = (inp.get("text") or "").strip()
    if not bot_id:
        return {"error": "no bot_id in context (not in a live meeting)"}
    if not text:
        return {"error": "text required"}

    try:
        from agent.live_session.signals import publish_speak
    except Exception:
        log.exception("speak_via_voice: live_session unavailable")
        return {"error": "live_session not available"}

    ok = publish_speak(bot_id, text)
    return {"spoken": bool(ok), "text": text[:200]}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="speak_via_voice",
        description=(
            "Speak the given text out loud via Gemini Live in the meeting. "
            "Use sparingly — only when addressed, or for proactive interjections "
            "that are clearly useful."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "text": {
                    "type": "string",
                    "description": "Short conversational text to speak (under 50 words).",
                },
            },
            required=["text"],
        ),
        handler=_speak_via_voice,
    ),
]
