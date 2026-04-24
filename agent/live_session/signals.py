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
SPEAK_CHANNEL = "agent:live:speak"
VOICE_CONTEXT_CHANNEL = "agent:live:voice_ctx"


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


def publish_gate_open(bot_id: str, reason: str, ttl_seconds: int = 30) -> bool:
    return _publish(GATE_CHANNEL, {"bot_id": bot_id, "reason": reason, "ttl_seconds": ttl_seconds})


def publish_gate_extend(bot_id: str, ttl_seconds: int = 30) -> bool:
    """Extend the gate TTL iff already open. No-op if gate is closed."""
    return _publish(GATE_EXTEND_CHANNEL, {"bot_id": bot_id, "ttl_seconds": ttl_seconds})


def publish_speak(bot_id: str, text: str) -> bool:
    return _publish(SPEAK_CHANNEL, {"bot_id": bot_id, "text": text})


def publish_voice_context(bot_id: str, text: str, turn_id: Optional[str] = None) -> bool:
    return _publish(
        VOICE_CONTEXT_CHANNEL,
        {"bot_id": bot_id, "text": text, "turn_id": turn_id},
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
