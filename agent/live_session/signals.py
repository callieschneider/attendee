"""
Redis pub/sub channel helpers for cross-process coordination between the
web/worker processes (where Turn Processor and webhook handlers run) and
the bridge process (where the Gemini Live WebSocket lives).

Channels (one global channel; messages carry `bot_id`):
  - agent:live:gate        → "open gate for bot X reason Y"
  - agent:live:speak       → "have bot X speak text T"
  - agent:live:voice_ctx   → "push text briefing B to bot X"

We use a single channel per concept rather than one-per-bot so the
bridge can run a single subscribe() connection regardless of active bot count.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("agent.live_session.signals")

GATE_CHANNEL = "agent:live:gate"
GATE_EXTEND_CHANNEL = "agent:live:gate_extend"
GATE_CLOSE_CHANNEL = "agent:live:gate_close"
SPEAK_CHANNEL = "agent:live:speak"
VOICE_CONTEXT_CHANNEL = "agent:live:voice_ctx"
# Test-harness channel: simulate a fully-transcribed user utterance.
# Bridge forwards as clientContent so Gemini treats it as a complete
# user turn and responds. Used by the overnight test harness.
INJECT_UTTERANCE_CHANNEL = "agent:live:inject_utterance"

# Redis keys tracking voice state for cross-process coordination.
GATE_STATE_KEY_FMT = "agent:gate:{bot_id}"
# Sticky "user said sleep / quiet" flag. Persists across gate auto-closes.
# Cleared explicitly when the user says a wake phrase, addresses the agent
# by name, or when the meeting ends.
VOICE_SUSPENDED_KEY_FMT = "agent:voice_suspended:{bot_id}"


_REDIS_CLIENT = None


def _get_redis():
    """Lazy-init a redis client using REDIS_URL / CELERY_BROKER_URL."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        import redis
    except Exception:
        log.exception("redis package unavailable")
        return None

    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    try:
        _REDIS_CLIENT = redis.from_url(url, decode_responses=True)
        return _REDIS_CLIENT
    except Exception:
        log.exception("signals: redis.from_url failed (url=%s)", url[:30])
        return None


def _publish(channel: str, payload: dict) -> bool:
    client = _get_redis()
    if not client:
        return False
    try:
        client.publish(channel, json.dumps(payload))
        return True
    except Exception:
        log.exception("signals: publish failed channel=%s", channel)
        return False


def _set_gate_state(bot_id: str, reason: str, ttl_seconds: int) -> None:
    """
    Synchronously mark the gate open in Redis so the Turn Processor can
    see it immediately (no DB roundtrip). TTL slightly longer than the
    audio gate's auto-close so it doesn't expire first.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        r.set(
            GATE_STATE_KEY_FMT.format(bot_id=bot_id),
            reason or "1",
            ex=max(ttl_seconds + 5, 10),
        )
    except Exception:
        log.exception("_set_gate_state: failed bot=%s", bot_id)


def _extend_gate_state(bot_id: str, ttl_seconds: int) -> bool:
    """Extend Redis gate key iff already set. Returns True if extended."""
    r = _get_redis()
    if r is None:
        return False
    try:
        key = GATE_STATE_KEY_FMT.format(bot_id=bot_id)
        existing = r.get(key)
        if existing is None:
            return False
        r.set(key, existing, ex=max(ttl_seconds + 5, 10))
        return True
    except Exception:
        log.exception("_extend_gate_state: failed bot=%s", bot_id)
        return False


def clear_gate_state(bot_id: str) -> None:
    """Clear the Redis gate key. Called by the bridge on explicit close."""
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(GATE_STATE_KEY_FMT.format(bot_id=bot_id))
    except Exception:
        log.exception("clear_gate_state: failed bot=%s", bot_id)


def is_gate_open(bot_id: str) -> bool:
    """Fast gate-state check used by the Turn Processor."""
    r = _get_redis()
    if r is None:
        return False
    try:
        return bool(r.get(GATE_STATE_KEY_FMT.format(bot_id=bot_id)))
    except Exception:
        log.exception("is_gate_open: failed bot=%s", bot_id)
        return False


def publish_gate_open(bot_id: str, reason: str, ttl_seconds: int = 30) -> bool:
    _set_gate_state(bot_id, reason, ttl_seconds)
    return _publish(
        GATE_CHANNEL,
        {"bot_id": bot_id, "reason": reason, "ttl_seconds": ttl_seconds},
    )


def publish_gate_close(bot_id: str, reason: str = "explicit") -> bool:
    """
    Tell the bridge to close the audio gate immediately. Used by sleep
    phrases ("go to sleep", "that's enough", "stand by", etc.).
    """
    clear_gate_state(bot_id)
    return _publish(
        GATE_CLOSE_CHANNEL,
        {"bot_id": bot_id, "reason": reason},
    )


def set_voice_suspended(bot_id: str, suspended: bool) -> None:
    """Sticky flag that survives gate auto-close. Sleep phrase sets True."""
    r = _get_redis()
    if r is None:
        return
    try:
        key = VOICE_SUSPENDED_KEY_FMT.format(bot_id=bot_id)
        if suspended:
            r.set(key, "1", ex=86400)  # 24h cap; cleared on wake
        else:
            r.delete(key)
    except Exception:
        log.exception("set_voice_suspended: failed bot=%s", bot_id)


def is_voice_suspended(bot_id: str) -> bool:
    r = _get_redis()
    if r is None:
        return False
    try:
        return bool(r.get(VOICE_SUSPENDED_KEY_FMT.format(bot_id=bot_id)))
    except Exception:
        return False


def publish_gate_extend(bot_id: str, ttl_seconds: int = 30) -> bool:
    """Extend the gate TTL iff already open. No-op if gate is closed."""
    if not _extend_gate_state(bot_id, ttl_seconds):
        return False
    return _publish(
        GATE_EXTEND_CHANNEL,
        {"bot_id": bot_id, "ttl_seconds": ttl_seconds},
    )


def publish_speak(bot_id: str, text: str) -> bool:
    return _publish(SPEAK_CHANNEL, {"bot_id": bot_id, "text": text})


def publish_voice_context(bot_id: str, text: str, turn_id: Optional[str] = None) -> bool:
    return _publish(
        VOICE_CONTEXT_CHANNEL,
        {"bot_id": bot_id, "text": text, "turn_id": turn_id},
    )


def publish_inject_utterance(bot_id: str, text: str, speaker: str = "Tester") -> bool:
    """
    Push a synthetic user utterance into the live session as if it were
    transcribed by Gemini. The bridge handler forwards as clientContent
    with turnComplete:true so Gemini treats it as a finished user turn
    and emits a response. Test-harness only.
    """
    return _publish(
        INJECT_UTTERANCE_CHANNEL,
        {"bot_id": bot_id, "text": text, "speaker": speaker},
    )


async def subscribe(channels: list[str]):
    """
    Async generator that yields `(channel, payload_dict)` for each pubsub
    message received. Caller is responsible for running inside an asyncio loop.
    Intended for use by the bridge process.
    """
    import asyncio

    try:
        import redis.asyncio as aioredis
    except Exception:
        log.exception("redis.asyncio unavailable; voice signals disabled")
        return

    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    client = aioredis.from_url(url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(*channels)
    log.info("signals: subscribed channels=%s", channels)

    try:
        async for msg in pubsub.listen():
            if msg is None or msg.get("type") != "message":
                continue
            channel = msg.get("channel", "")
            raw = msg.get("data", "")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                payload = {"raw": raw}
            yield channel, payload
    finally:
        try:
            await pubsub.unsubscribe(*channels)
            await pubsub.close()
        except Exception:
            pass
