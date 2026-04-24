"""
Meeting Agent realtime audio bridge.

Standalone asyncio WebSocket server.

Protocol:
  Attendee → Bridge: JSON {"trigger":"realtime_audio.mixed","bot_id":"...","data":{"chunk":"<b64>","sample_rate":16000,...}}
  Bridge → Attendee: JSON {"trigger":"realtime_audio.bot_output","data":{"chunk":"<b64>","sample_rate":16000}}

  Bridge → Gemini Live: JSON {"realtimeInput":{"audio":{"data":"<b64>","mimeType":"audio/pcm;rate=16000"}}}
  Gemini Live → Bridge: JSON {"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":"audio/pcm;rate=24000","data":"<b64>"}}]}}}
                          or: {"toolCall":{"functionCalls":[{"id":"...","name":"...","args":{}}]}}

Run: python -m agent.bridge
"""
import asyncio
import json
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
from django.conf import settings

from agent.audio_utils import b64_to_pcm16, pcm16_to_b64, pcm16_resample
from agent.context_builder import build_context
from agent.gemini_live import build_live_setup
from agent.tools import execute_tool

log = logging.getLogger("agent.bridge")

BRIDGE_PORT = int(os.getenv("PORT", os.getenv("BRIDGE_PORT", "8765")))

# Gemini Live WebSocket URL
# BidiGenerateContent (not Constrained) accepts API key directly.
# BidiGenerateContentConstrained requires an ephemeral token — that's the browser-side path.
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com"
    "/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
)

ATTENDEE_SAMPLE_RATE = 16000   # Hz — Attendee sends and expects 16kHz
GEMINI_OUTPUT_RATE = 24000     # Hz — Gemini Live outputs at 24kHz


async def build_gemini_context(bot_id: str) -> dict:
    """Load series context for this bot. Runs ORM queries in a thread."""

    @sync_to_async
    def _load():
        series_id = None
        try:
            from agent.models import MeetingOccurrence
            occ = MeetingOccurrence.objects.filter(
                bot__object_id=bot_id
            ).order_by("-created_at").first()
            if occ:
                series_id = str(occ.series_id)
        except Exception:
            log.exception("build_gemini_context: failed to load series for bot %s", bot_id)
        return build_context(series_id=series_id)

    return await _load()


async def open_gemini_ws(bot_id: str):
    """Open Gemini Live WebSocket and send setup frame. Returns the WS."""
    system_prompt = await build_gemini_context(bot_id)
    voice = getattr(settings, "AGENT_DEFAULT_VOICE", "Zephyr")
    setup_msg = build_live_setup(system_prompt, voice=voice)

    api_key = settings.GOOGLE_API_KEY
    url = f"{GEMINI_WS_URL}?key={api_key}"

    log.info("bridge: connecting to Gemini Live for bot %s", bot_id)
    gemini_ws = await websockets.connect(url, max_size=20 * 1024 * 1024)

    # Send setup as the very first frame
    await gemini_ws.send(json.dumps({"setup": setup_msg["setup"]}))

    # Wait for setupComplete
    async for raw in gemini_ws:
        msg = json.loads(raw)
        if "setupComplete" in msg:
            log.info("bridge: Gemini Live setup complete for bot %s", bot_id)
            return gemini_ws
        # Ignore other early messages
        log.debug("bridge: pre-setup message: %s", str(msg)[:100])

    raise RuntimeError("Gemini Live WS closed before setupComplete")


async def forward_attendee_to_gemini(attendee_ws, gemini_ws, bot_id: str):
    """
    Receive JSON messages from Attendee, extract PCM16 audio,
    forward to Gemini Live as realtimeInput audio frames.
    """
    async for raw in attendee_ws:
        try:
            if isinstance(raw, bytes):
                # Shouldn't happen but handle gracefully
                log.debug("bridge: got raw bytes from Attendee (unexpected)")
                continue

            msg = json.loads(raw)
            trigger = msg.get("trigger", "")

            if trigger == "realtime_audio.mixed":
                data = msg.get("data", {})
                chunk_b64 = data.get("chunk", "")
                sample_rate = int(data.get("sample_rate", ATTENDEE_SAMPLE_RATE))

                if not chunk_b64:
                    continue

                pcm = b64_to_pcm16(chunk_b64)

                # Skip near-silence / empty frames
                if len(pcm) < 64:
                    continue

                # Resample to Gemini's expected input rate if needed
                if sample_rate != ATTENDEE_SAMPLE_RATE:
                    pcm = pcm16_resample(pcm, sample_rate, ATTENDEE_SAMPLE_RATE)

                await gemini_ws.send(json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "data": pcm16_to_b64(pcm),
                            "mimeType": f"audio/pcm;rate={ATTENDEE_SAMPLE_RATE}",
                        }
                    }
                }))
            # Ignore all other trigger types silently

        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception:
            log.exception("bridge: error forwarding Attendee audio for bot %s", bot_id)


async def forward_gemini_to_attendee(gemini_ws, attendee_ws, bot_id: str):
    """
    Receive messages from Gemini Live:
    - Audio chunks → resample 24kHz→16kHz → send to Attendee as bot_output
    - Tool calls → execute → send tool responses back to Gemini
    """
    ctx = {"bot_id": bot_id}

    # Load series_id for tool context
    try:
        from agent.models import MeetingOccurrence

        @sync_to_async
        def _get_occ():
            return MeetingOccurrence.objects.filter(
                bot__object_id=bot_id
            ).order_by("-created_at").first()

        occ = await _get_occ()
        if occ:
            ctx["series_id"] = str(occ.series_id)
            ctx["occurrence_id"] = str(occ.id)
    except Exception:
        pass

    async for raw in gemini_ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # ── Audio output from Gemini ──────────────────────────────────────
        server_content = msg.get("serverContent", {})

        # Handle interrupt: Gemini stopped itself because user spoke
        if server_content.get("interrupted"):
            log.info("bridge: Gemini interrupted (user spoke) for bot %s", bot_id)
            continue

        model_turn = server_content.get("modelTurn", {})
        audio_chunks_sent = 0
        for part in model_turn.get("parts", []):
            inline = part.get("inlineData", {})
            mime = inline.get("mimeType", "")
            if mime.startswith("audio/pcm") and inline.get("data"):
                # Gemini outputs at 24kHz; Attendee wants 16kHz
                pcm_24k = b64_to_pcm16(inline["data"])
                pcm_16k = pcm16_resample(pcm_24k, GEMINI_OUTPUT_RATE, ATTENDEE_SAMPLE_RATE)
                # Send to Attendee as bot_output
                await attendee_ws.send(json.dumps({
                    "trigger": "realtime_audio.bot_output",
                    "data": {
                        "chunk": pcm16_to_b64(pcm_16k),
                        "sample_rate": ATTENDEE_SAMPLE_RATE,
                    },
                }))
                audio_chunks_sent += 1

        if audio_chunks_sent:
            log.info("bridge: sent %d audio chunks to Attendee for bot %s", audio_chunks_sent, bot_id)

        if server_content.get("turnComplete"):
            log.info("bridge: Gemini turn complete for bot %s", bot_id)

        # ── Tool calls from Gemini ────────────────────────────────────────
        tool_call = msg.get("toolCall", {})
        function_calls = tool_call.get("functionCalls", [])
        if function_calls:
            responses = []
            for fc in function_calls:
                name = fc.get("name", "")
                args = fc.get("args", {})
                call_id = fc.get("id", "")
                log.info("bridge: tool call %s(%s) for bot %s", name, list(args.keys()), bot_id)

                @sync_to_async
                def _exec(n=name, a=args, c=ctx):
                    return execute_tool(n, a, c)

                result = await _exec()
                responses.append({
                    "id": call_id,
                    "name": name,
                    "response": result,
                })

            await gemini_ws.send(json.dumps({
                "toolResponse": {"functionResponses": responses}
            }))


async def bridge_session(attendee_ws, bot_id: str):
    """Handle one Attendee ↔ Gemini Live bridged session for a single bot."""
    log.info("bridge: session started for bot %s", bot_id)
    try:
        gemini_ws = await open_gemini_ws(bot_id)
    except Exception:
        log.exception("bridge: failed to open Gemini Live for bot %s", bot_id)
        await attendee_ws.close(1011, "Failed to connect to Gemini Live")
        return

    try:
        await asyncio.gather(
            forward_attendee_to_gemini(attendee_ws, gemini_ws, bot_id),
            forward_gemini_to_attendee(gemini_ws, attendee_ws, bot_id),
        )
    except websockets.exceptions.ConnectionClosed as e:
        log.info("bridge: session closed for bot %s — %s", bot_id, e)
    except Exception:
        log.exception("bridge: unexpected error for bot %s", bot_id)
    finally:
        try:
            await gemini_ws.close()
        except Exception:
            pass
        log.info("bridge: session ended for bot %s", bot_id)


async def handler(websocket):
    """Route incoming connections by path /audio/{bot_id}."""
    path = websocket.request.path
    parts = path.strip("/").split("/")

    if len(parts) >= 2 and parts[0] == "audio":
        bot_id = parts[1]
        await bridge_session(websocket, bot_id)
    else:
        log.warning("bridge: unknown path %s", path)
        await websocket.close(1008, "Unknown path — expected /audio/{bot_id}")


async def main():
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
