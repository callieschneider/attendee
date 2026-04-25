"""
LiveSessionManager — owns the persistent Gemini Live WebSocket for a single bot.

Runs inside the bridge process as an asyncio task. Three concurrent sub-tasks:

  1. `gemini_reader`         — reads from Gemini Live; emits audio (when gate open)
                               and dispatches toolCalls for read-only fast-path tools.
  2. `attendee_audio_pump`   — forwards Attendee PCM16 into Gemini Live iff gate open.
  3. `signal_listener`       — subscribes to Redis signals (gate-open / speak / voice_ctx)
                               and drives the audio gate + realtimeInput.text pushes.

The session is resilient to Gemini Live's ~10-min cap: we persist the
`sessionResumption.newHandle` on every update and reopen transparently
on close/`goAway`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import websockets
from asgiref.sync import sync_to_async
from django.conf import settings

from agent.audio_utils import b64_to_pcm16, pcm16_resample, pcm16_to_b64

from . import signals
from .audio_gate import AudioGate

log = logging.getLogger("agent.live_session.manager")

GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com"
    "/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
)
ATTENDEE_SAMPLE_RATE = 16000
GEMINI_OUTPUT_RATE = 24000


# Tools considered safe to execute DIRECTLY from Gemini Live's toolCall stream
# (no race conditions with the Turn Processor). All other tools must go through
# the Turn Processor where they're tracked in ActionLogEntry.
_LIVE_READ_ONLY_TOOLS = {
    "get_recent_occurrences",
    "get_occurrence_transcript",
    "get_meeting_notes",
    "list_upcoming_meetings",
    "get_series_context_bundle",
    "list_series",
    "list_tasks",
    "search_artifacts",
    "get_artifact",
    "semantic_search",
    "web_search",
    "fetch_url",
    "read_recent_chat",
    # Visual tools — write-mutating but idempotent, user-facing, and the
    # canvas pump picks up changes on its next tick. Letting Gemini Live
    # call these directly avoids the user hearing "I can't do that" when
    # they ask for a chart on the bot's video.
    "create_visual",
    "update_visual",
}


class LiveSessionManager:
    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self.gate = AudioGate(bot_id)
        self._gemini_ws = None
        self._attendee_ws = None
        self._resumption_handle: Optional[str] = None
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        # Echo suppression: while the bot is actively emitting TTS, drop
        # incoming meeting audio. Without this, the bot hears its own voice,
        # Gemini's VAD trips, the response restarts mid-sentence, and the
        # transcript shows "twice in the same turn" duplication.
        self._bot_speaking_until: float = 0.0
        self._interrupted_until: float = 0.0

    # ── Public entrypoint ────────────────────────────────────────────────────

    async def handle_attendee_connection(self, attendee_ws) -> None:
        """Run one live session for this bot end-to-end."""
        self._attendee_ws = attendee_ws
        log.info("live_session: starting for bot=%s", self.bot_id)

        await self._load_resumption_handle()

        try:
            await self._open_gemini_session()
        except Exception:
            log.exception("live_session: failed to open Gemini for bot=%s", self.bot_id)
            await attendee_ws.close(1011, "Failed to connect to Gemini Live")
            return

        try:
            await asyncio.gather(
                self._gemini_reader(),
                self._attendee_audio_pump(),
                self._signal_listener(),
            )
        except websockets.exceptions.ConnectionClosed as e:
            log.info("live_session: conn closed bot=%s — %s", self.bot_id, e)
        except Exception:
            log.exception("live_session: unexpected failure bot=%s", self.bot_id)
        finally:
            await self._shutdown()

    # ── Session lifecycle ────────────────────────────────────────────────────

    async def _open_gemini_session(self) -> None:
        """(Re)open the Gemini Live WS with current context + resumption handle."""
        from agent.context_engine.builder import build_context
        from agent.gemini_live import build_live_setup

        @sync_to_async
        def _build():
            result = build_context(bot_id=self.bot_id, task="initial_voice_setup")
            return result["prompt_markdown"]

        system_prompt = await _build()
        voice = getattr(settings, "AGENT_DEFAULT_VOICE", "Zephyr")
        setup_msg = build_live_setup(system_prompt, voice=voice)

        # Inject sessionResumption into the setup payload (v1alpha field)
        if self._resumption_handle:
            setup_msg["setup"]["sessionResumption"] = {"handle": self._resumption_handle}
            log.info("live_session: resuming with handle=%s…", self._resumption_handle[:12])
        else:
            setup_msg["setup"]["sessionResumption"] = {}

        api_key = settings.GOOGLE_API_KEY
        url = f"{GEMINI_WS_URL}?key={api_key}"
        self._gemini_ws = await websockets.connect(url, max_size=20 * 1024 * 1024)
        await self._gemini_ws.send(json.dumps({"setup": setup_msg["setup"]}))

        # Wait for setupComplete
        async for raw in self._gemini_ws:
            msg = json.loads(raw)
            if "setupComplete" in msg:
                log.info("live_session: setupComplete bot=%s", self.bot_id)
                await self._persist_session_opened()
                return
        raise RuntimeError("Gemini WS closed before setupComplete")

    async def _reopen_gemini_session(self, reason: str) -> None:
        log.info("live_session: reopening Gemini bot=%s reason=%s", self.bot_id, reason)
        try:
            if self._gemini_ws:
                await self._gemini_ws.close()
        except Exception:
            pass
        await self._open_gemini_session()

    async def _shutdown(self) -> None:
        try:
            await self.gate.close("session_end")
        except Exception:
            pass
        try:
            if self._gemini_ws:
                await self._gemini_ws.close()
        except Exception:
            pass
        self._stop_event.set()
        log.info("live_session: shut down bot=%s", self.bot_id)

    # ── Concurrent sub-tasks ─────────────────────────────────────────────────

    async def _gemini_reader(self) -> None:
        """Read Gemini Live frames: audio, toolCalls, goAway, sessionResumption."""
        while not self._stop_event.is_set():
            try:
                async for raw in self._gemini_ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_gemini_message(msg)
                # WS closed cleanly — reopen via resumption handle
                if not self._stop_event.is_set():
                    await self._reopen_gemini_session("ws_closed")
            except websockets.exceptions.ConnectionClosed as e:
                log.info("live_session: Gemini WS closed bot=%s — %s", self.bot_id, e)
                if self._stop_event.is_set():
                    return
                await self._reopen_gemini_session("conn_closed")
            except Exception:
                log.exception("live_session: gemini_reader error bot=%s", self.bot_id)
                await asyncio.sleep(1)
                if not self._stop_event.is_set():
                    try:
                        await self._reopen_gemini_session("error")
                    except Exception:
                        log.exception("live_session: reopen failed; giving up bot=%s", self.bot_id)
                        return

    async def _handle_gemini_message(self, msg: dict) -> None:
        # Session resumption update — persist the newHandle
        sr = msg.get("sessionResumptionUpdate")
        if sr and sr.get("newHandle"):
            self._resumption_handle = sr["newHandle"]
            await self._persist_resumption_handle(self._resumption_handle)

        # Server tells us to reopen soon
        if "goAway" in msg:
            log.info("live_session: goAway bot=%s — scheduling reopen", self.bot_id)
            asyncio.create_task(self._reopen_gemini_session("go_away"))
            return

        # Audio out (only flush to Attendee when gate open)
        server_content = msg.get("serverContent", {}) or {}
        if server_content.get("interrupted"):
            log.info("live_session: Gemini interrupted bot=%s — dropping buffered audio", self.bot_id)
            # Mark interrupted so we skip any modelTurn audio in THIS frame
            # and the next ~500ms window (Gemini Live continues to send
            # modelTurn frames briefly after emitting 'interrupted' while
            # its TTS stream drains).
            self._interrupted_until = time.monotonic() + 0.5
            # Also reset the bot-speaking flag so user can speak immediately.
            self._bot_speaking_until = 0.0
        if self._interrupted_until > time.monotonic():
            # Skip any audio output during the drain window
            server_content.pop("modelTurn", None)
        model_turn = server_content.get("modelTurn", {}) or {}
        for part in model_turn.get("parts", []) or []:
            inline = part.get("inlineData", {}) or {}
            mime = inline.get("mimeType", "")
            data = inline.get("data", "")
            if mime.startswith("audio/pcm") and data:
                if not self.gate.is_open:
                    # Gemini should not be emitting audio when gate closed;
                    # log once at INFO and drop the chunk.
                    log.debug("live_session: audio while gate closed bot=%s (dropped)", self.bot_id)
                    continue
                # Mark "bot is currently speaking" — used by the audio pump to
                # drop incoming mic audio for the duration of bot speech +
                # a small echo-tail window. Each audio frame extends this.
                self._bot_speaking_until = time.monotonic() + 0.6
                pcm_24k = b64_to_pcm16(data)
                pcm_16k = pcm16_resample(pcm_24k, GEMINI_OUTPUT_RATE, ATTENDEE_SAMPLE_RATE)
                try:
                    await self._attendee_ws.send(
                        json.dumps(
                            {
                                "trigger": "realtime_audio.bot_output",
                                "data": {
                                    "chunk": pcm16_to_b64(pcm_16k),
                                    "sample_rate": ATTENDEE_SAMPLE_RATE,
                                },
                            }
                        )
                    )
                except Exception:
                    log.exception("live_session: attendee send failed bot=%s", self.bot_id)
                    return

        if server_content.get("turnComplete"):
            # Don't slam the gate shut on turnComplete — the user may be
            # mid-conversation. Let the AudioGate's TTL auto-close handle
            # the "no activity for N seconds" case instead.
            log.debug(
                "live_session: turnComplete bot=%s — leaving gate open for reply",
                self.bot_id,
            )
            # Bot is done speaking; user can speak immediately now.
            self._bot_speaking_until = 0.0

        # Tool calls — only read-only fast-path; writes go through Turn Processor
        tc = msg.get("toolCall")
        if tc and tc.get("functionCalls"):
            await self._handle_tool_calls(tc["functionCalls"])

    async def _handle_tool_calls(self, calls: list[dict]) -> None:
        from agent.tools import execute_tool

        responses = []
        for c in calls:
            name = c.get("name", "")
            args = c.get("args", {}) or {}
            call_id = c.get("id", "")
            if name not in _LIVE_READ_ONLY_TOOLS:
                # Not safe to run inline — surface a stub so Gemini doesn't hang.
                responses.append(
                    {
                        "id": call_id,
                        "name": name,
                        "response": {
                            "error": (
                                f"Tool '{name}' is write-mutating. The background Turn "
                                "Processor handles those; continue listening."
                            )
                        },
                    }
                )
                # Log the rejection so it shows in the canvas debug UI
                await self._log_action(
                    name, args,
                    result={},
                    status="error",
                    error_msg=f"rejected: {name} is write-mutating",
                )
                continue

            @sync_to_async
            def _exec(n=name, a=args):
                return execute_tool(n, a, {"bot_id": self.bot_id})

            import time as _time
            t0 = _time.time()
            try:
                result = await _exec()
                latency_ms = int((_time.time() - t0) * 1000)
                if isinstance(result, dict) and result.get("error"):
                    await self._log_action(
                        name, args,
                        result=result,
                        status="error",
                        error_msg=str(result.get("error"))[:300],
                        latency_ms=latency_ms,
                    )
                else:
                    await self._log_action(
                        name, args,
                        result=result if isinstance(result, dict) else {"value": str(result)},
                        status="ok",
                        latency_ms=latency_ms,
                    )
                responses.append({"id": call_id, "name": name, "response": result})
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                latency_ms = int((_time.time() - t0) * 1000)
                log.exception("live_session: tool %s raised bot=%s", name, self.bot_id)
                await self._log_action(
                    name, args, result={}, status="error",
                    error_msg=err[:300], latency_ms=latency_ms,
                )
                responses.append({"id": call_id, "name": name, "response": {"error": err}})

        try:
            async with self._send_lock:
                await self._gemini_ws.send(
                    json.dumps({"toolResponse": {"functionResponses": responses}})
                )
        except Exception:
            log.exception("live_session: tool response send failed bot=%s", self.bot_id)

    async def _log_action(
        self,
        tool_name: str,
        tool_input: dict,
        result: dict,
        status: str,
        error_msg: str = "",
        latency_ms: int | None = None,
    ) -> None:
        """Persist an ActionLogEntry for a Gemini Live tool call so it shows in
        the canvas debug UI alongside Turn Processor actions."""
        import uuid as _uuid

        @sync_to_async
        def _save():
            try:
                from agent.models import ActionLogEntry

                ActionLogEntry.objects.create(
                    bot_id=self.bot_id,
                    turn_id=_uuid.uuid4(),
                    tool_name=tool_name,
                    tool_input=tool_input or {},
                    tool_result=result or {},
                    status=status,
                    error_message=error_msg or "",
                    latency_ms=latency_ms,
                )
            except Exception:
                log.exception("_log_action: persist failed bot=%s tool=%s", self.bot_id, tool_name)

        try:
            await _save()
        except Exception:
            log.exception("_log_action: wrapper failed")

    async def _attendee_audio_pump(self) -> None:
        """Forward Attendee audio into Gemini Live iff gate is open."""
        try:
            async for raw in self._attendee_ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("trigger") != "realtime_audio.mixed":
                    continue

                if not self.gate.is_open:
                    # Drop silently — Gemini gets no audio unless gate is open
                    continue
                # Echo suppression: the bot's own TTS comes back through the
                # meeting's mixed audio. Don't forward it back to Gemini Live
                # while it's speaking + a short tail (otherwise Gemini's VAD
                # trips on the echo and restarts the response mid-sentence).
                if time.monotonic() < self._bot_speaking_until:
                    continue

                data = msg.get("data", {})
                chunk_b64 = data.get("chunk", "")
                if not chunk_b64:
                    continue
                sample_rate = int(data.get("sample_rate", ATTENDEE_SAMPLE_RATE))
                pcm = b64_to_pcm16(chunk_b64)
                if len(pcm) < 64:
                    continue
                if sample_rate != ATTENDEE_SAMPLE_RATE:
                    pcm = pcm16_resample(pcm, sample_rate, ATTENDEE_SAMPLE_RATE)
                try:
                    async with self._send_lock:
                        await self._gemini_ws.send(
                            json.dumps(
                                {
                                    "realtimeInput": {
                                        "audio": {
                                            "data": pcm16_to_b64(pcm),
                                            "mimeType": f"audio/pcm;rate={ATTENDEE_SAMPLE_RATE}",
                                        }
                                    }
                                }
                            )
                        )
                except Exception:
                    log.exception("live_session: gemini audio send failed bot=%s", self.bot_id)
                    return
        except websockets.exceptions.ConnectionClosed:
            raise
        finally:
            self._stop_event.set()

    async def _signal_listener(self) -> None:
        """Subscribe to Redis signals and dispatch into the live session."""
        channels = [
            signals.GATE_CHANNEL,
            signals.GATE_EXTEND_CHANNEL,
            signals.SPEAK_CHANNEL,
            signals.VOICE_CONTEXT_CHANNEL,
        ]
        try:
            async for channel, payload in signals.subscribe(channels):
                if self._stop_event.is_set():
                    return
                if payload.get("bot_id") != self.bot_id:
                    continue
                if channel == signals.GATE_CHANNEL:
                    await self.gate.open(
                        reason=payload.get("reason", "signal"),
                        ttl_seconds=int(payload.get("ttl_seconds", 30)),
                    )
                elif channel == signals.GATE_EXTEND_CHANNEL:
                    await self.gate.extend_if_open(
                        ttl_seconds=int(payload.get("ttl_seconds", 30)),
                    )
                elif channel == signals.SPEAK_CHANNEL:
                    text = payload.get("text", "")
                    if text:
                        await self._speak_directive(text)
                elif channel == signals.VOICE_CONTEXT_CHANNEL:
                    text = payload.get("text", "")
                    if text:
                        await self._push_realtime_text(text)
        except Exception:
            log.exception("live_session: signal_listener failed bot=%s", self.bot_id)

    # ── Helpers: send to Gemini ──────────────────────────────────────────────

    async def _push_realtime_text(self, text: str) -> None:
        """Push text via realtimeInput.text (NEVER clientContent.turns)."""
        if not self._gemini_ws:
            return
        try:
            async with self._send_lock:
                await self._gemini_ws.send(
                    json.dumps({"realtimeInput": {"text": text}})
                )
        except Exception:
            log.exception("live_session: realtimeInput.text send failed bot=%s", self.bot_id)

    async def _speak_directive(self, text: str) -> None:
        """
        Proactive speech — push the text into Gemini Live as if it were
        the agent's own thought-in-progress, so it flows out naturally.
        Open the audio gate first so the TTS is actually emitted to the meeting.
        """
        await self.gate.open(reason="speak_via_voice", ttl_seconds=20)
        # Phrase it as something Gemini Live would naturally produce next.
        # Using plain text here avoids Gemini treating it as a command/instruction.
        await self._push_realtime_text(text)

    # ── Persistence (MeetingCursor) ──────────────────────────────────────────

    async def _load_resumption_handle(self) -> None:
        from asgiref.sync import sync_to_async

        @sync_to_async
        def _load():
            try:
                from agent.models import MeetingCursor

                cursor = MeetingCursor.objects.filter(bot_id=self.bot_id).first()
                return cursor.voice_session_handle if cursor else ""
            except Exception:
                log.exception("_load_resumption_handle: bot=%s", self.bot_id)
                return ""

        handle = await _load()
        if handle:
            self._resumption_handle = handle

    async def _persist_resumption_handle(self, handle: str) -> None:
        from asgiref.sync import sync_to_async

        @sync_to_async
        def _save():
            try:
                from agent.models import MeetingCursor

                cursor, _ = MeetingCursor.objects.get_or_create(bot_id=self.bot_id)
                cursor.voice_session_handle = handle[:512]
                cursor.save(update_fields=["voice_session_handle", "updated_at"])
            except Exception:
                log.exception("_persist_resumption_handle: bot=%s", self.bot_id)

        await _save()

    async def _persist_session_opened(self) -> None:
        from asgiref.sync import sync_to_async

        from django.utils import timezone

        @sync_to_async
        def _save():
            try:
                from agent.models import MeetingCursor

                cursor, _ = MeetingCursor.objects.get_or_create(bot_id=self.bot_id)
                cursor.voice_session_opened_at = timezone.now()
                cursor.save(update_fields=["voice_session_opened_at", "updated_at"])
            except Exception:
                log.exception("_persist_session_opened: bot=%s", self.bot_id)

        await _save()
