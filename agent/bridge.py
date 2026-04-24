"""
Meeting Agent realtime audio bridge.

Standalone asyncio WebSocket server.

Post Phase 5: this file is a thin shim. All the Gemini Live logic lives
in `agent.live_session.manager.LiveSessionManager`. This process just:
  1. Accepts Attendee WS connections at /audio/<bot_id>.
  2. Resolves the session_id path segment back to a bot_id.
  3. Hands the WS off to a LiveSessionManager for that bot.

Run: python -m agent.bridge
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

# Bootstrap Django before any agent imports
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings.production_with_agent")

import django
django.setup()

import websockets
import websockets.server
from asgiref.sync import sync_to_async

log = logging.getLogger("agent.bridge")

BRIDGE_PORT = int(os.getenv("PORT", os.getenv("BRIDGE_PORT", "8765")))


async def _resolve_bot_id(session_id_or_bot_id: str) -> str:
    """
    The WS path carries either a `session_id` (legacy, from create_meeting_bot)
    or a real `bot_id` (preferred). Try bot_id directly first; if that doesn't
    match, fall through to session_id (letting the caller still have something).
    """

    @sync_to_async
    def _lookup():
        try:
            from bots.models import Bot

            bot = Bot.objects.filter(object_id=session_id_or_bot_id).first()
            if bot:
                return bot.object_id
        except Exception:
            log.exception("_resolve_bot_id: DB lookup failed")
        return session_id_or_bot_id  # fallback

    return await _lookup()


async def bridge_session(attendee_ws, session_id: str) -> None:
    """Route one Attendee WS connection to a LiveSessionManager."""
    from agent.live_session.manager import LiveSessionManager

    bot_id = await _resolve_bot_id(session_id)
    log.info("bridge: session started session_id=%s bot_id=%s", session_id, bot_id)

    manager = LiveSessionManager(bot_id=bot_id)
    try:
        await manager.handle_attendee_connection(attendee_ws)
    except websockets.exceptions.ConnectionClosed as e:
        log.info("bridge: session closed bot=%s — %s", bot_id, e)
    except Exception:
        log.exception("bridge: unexpected error bot=%s", bot_id)
    finally:
        log.info("bridge: session ended bot=%s", bot_id)


async def handler(websocket) -> None:
    """Route incoming connections by path /audio/{bot_id_or_session_id}."""
    path = websocket.request.path
    parts = path.strip("/").split("/")

    if len(parts) >= 2 and parts[0] == "audio":
        session_id = parts[1]
        await bridge_session(websocket, session_id)
    else:
        log.warning("bridge: unknown path %s", path)
        await websocket.close(1008, "Unknown path — expected /audio/{bot_id}")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    log.info("bridge: starting on port %d", BRIDGE_PORT)

    stop = asyncio.get_event_loop().create_future()
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)
    loop.add_signal_handler(signal.SIGINT, stop.set_result, None)

    async with websockets.serve(handler, "0.0.0.0", BRIDGE_PORT):
        log.info("bridge: ready on 0.0.0.0:%d", BRIDGE_PORT)
        await stop
    log.info("bridge: stopped")


if __name__ == "__main__":
    asyncio.run(main())
