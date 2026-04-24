"""
Live voice session package.

- manager: owns the persistent Gemini Live WebSocket for a single bot
- audio_gate: closed-by-default gate controlling when Attendee audio is forwarded
- voice_pump: text-briefing dispatcher via Redis pub/sub and realtimeInput.text
- signals: Redis channel name helpers + publish/subscribe primitives
"""
from .signals import (
    GATE_CHANNEL,
    SPEAK_CHANNEL,
    VOICE_CONTEXT_CHANNEL,
    publish_gate_open,
    publish_speak,
    publish_voice_context,
    subscribe,
)
from .voice_pump import enqueue_voice_briefing

__all__ = [
    "GATE_CHANNEL",
    "SPEAK_CHANNEL",
    "VOICE_CONTEXT_CHANNEL",
    "publish_gate_open",
    "publish_speak",
    "publish_voice_context",
    "subscribe",
    "enqueue_voice_briefing",
]
