"""
Voice tools.

  - `speak_via_voice`  — synthesize text via Gemini Live (rare; planner-side)
  - `voice_sleep`      — stop talking; agent goes silent until woken
  - `voice_wake`       — resume talking; agent re-engages

Both `voice_sleep` and `voice_wake` are intentionally available to the live
voice model AND the planner brain. The live voice calls them when it hears
the user mid-conversation; the planner calls them based on webhook
transcripts (which still arrive while voice is suspended). The LLMs decide
based on intent — there is no fixed phrase list.
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


def _kick_canvas(bot_id: str) -> None:
    try:
        from agent.canvas.pump import push_canvas_images_for_bot

        push_canvas_images_for_bot(bot_id)
    except Exception:
        log.exception("voice tool: canvas kick failed bot=%s", bot_id)


def _voice_sleep(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id")
    if not bot_id:
        return {"error": "no bot_id in context (not in a live meeting)"}
    reason = (inp.get("reason") or "user_request")[:120]
    try:
        from agent.live_session.signals import publish_gate_close, set_voice_suspended

        set_voice_suspended(bot_id, True)
        publish_gate_close(bot_id, reason=reason)
    except Exception as exc:
        log.exception("voice_sleep: signal failed bot=%s", bot_id)
        return {"error": f"{type(exc).__name__}: {exc}"}

    _kick_canvas(bot_id)
    log.info("voice_sleep: bot=%s reason=%s", bot_id, reason)
    return {"ok": True, "state": "asleep", "reason": reason}


def _voice_wake(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id")
    if not bot_id:
        return {"error": "no bot_id in context (not in a live meeting)"}
    reason = (inp.get("reason") or "user_request")[:120]
    greeting_context = (inp.get("greeting_context") or "").strip()
    ttl = 1800
    try:
        from agent.live_session.signals import (
            publish_gate_open,
            publish_voice_context,
            set_voice_suspended,
        )

        set_voice_suspended(bot_id, False)
        publish_gate_open(bot_id, reason=reason, ttl_seconds=ttl)
        if greeting_context:
            publish_voice_context(
                bot_id,
                f"User just woke you up: {greeting_context}",
            )
    except Exception as exc:
        log.exception("voice_wake: signal failed bot=%s", bot_id)
        return {"error": f"{type(exc).__name__}: {exc}"}

    _kick_canvas(bot_id)
    log.info(
        "voice_wake: bot=%s reason=%s greeting=%r",
        bot_id, reason, greeting_context[:80],
    )
    return {"ok": True, "state": "listening", "reason": reason, "ttl_seconds": ttl}


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
    ToolDefinition(
        name="voice_sleep",
        description=(
            "Put the live voice on hold. Stop talking immediately. Use this "
            "whenever the user expresses ANY intent for you to be quiet — "
            "the wording does not matter. Examples (non-exhaustive): "
            "'go to sleep', 'be quiet', 'that's enough', 'hold on', 'pause', "
            "'shut up', 'I'll come back to you', 'we're talking among "
            "ourselves', 'stand by'. Decide by intent, not keywords. The "
            "background brain (Turn Processor) keeps listening to the "
            "meeting and can wake you back up. Do NOT speak before calling "
            "this — just go silent."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "reason": {
                    "type": "string",
                    "description": (
                        "Short label for why you're going to sleep "
                        "(e.g. 'user_request', 'side_conversation')."
                    ),
                },
            },
            required=[],
        ),
        handler=_voice_sleep,
    ),
    ToolDefinition(
        name="voice_wake",
        description=(
            "Resume the live voice. Reopens the audio path so the user can "
            "hear you again. Use this whenever the user expresses ANY intent "
            "for you to start talking again — the wording does not matter. "
            "Examples (non-exhaustive): 'wake up', 'are you there', 'come "
            "back', 'okay you can talk', 'let's go again', 'jump back in'. "
            "Decide by intent, not keywords. While asleep the live voice "
            "model may not hear the user — the Turn Processor invokes this "
            "tool on its behalf based on webhook transcripts. After waking, "
            "respond to whatever the user actually asked (use "
            "`greeting_context` to pass the wake utterance text)."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "reason": {
                    "type": "string",
                    "description": "Short label for why you're waking (e.g. 'user_request').",
                },
                "greeting_context": {
                    "type": "string",
                    "description": (
                        "Optional: the user's wake utterance text, so the "
                        "live voice can address what was actually asked "
                        "instead of just saying hello."
                    ),
                },
            },
            required=[],
        ),
        handler=_voice_wake,
    ),
]
