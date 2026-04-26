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


# Tools Gemini Live is allowed to execute DIRECTLY from its toolCall stream.
# Live IS the user-facing agent (Clever Star) so it owns every user-facing
# tool: lookups, visuals, task/artifact writes, chat/email, voice state,
# heavier reasoning via call_model. The Turn Processor (Haiku 4.5) runs
# only in the background, capturing items Live didn't already act on.
#
# Why this set is safe (as opposed to the earlier read-only-only design):
#   - Each Live toolCall logs an ActionLogEntry up front, so the Turn
#     Processor sees the action in its prompt and won't redo it.
#   - The Turn Processor's prompt explicitly forbids re-firing user-
#     requested tools while voice is active.
#   - Every tool here is short-lived enough (<3s) to be fine inside the
#     Live toolCall round-trip.
_LIVE_ALLOWED_TOOLS = frozenset({
    # Read / lookup
    "list_tasks",
    "list_series",
    "list_upcoming_meetings",
    "get_recent_occurrences",
    "get_occurrence_transcript",
    "get_meeting_notes",
    "get_series_context_bundle",
    "search_artifacts",
    "get_artifact",
    "semantic_search",
    "read_recent_chat",
    "web_search",
    "fetch_url",
    # Visuals
    "create_visual",
    "update_visual",
    # Tasks & artifacts
    "create_task",
    "update_task_status",
    "create_artifact",
    "save_artifact_from_url",
    "promote_meeting_task",
    # Chat / email
    "send_chat_message",
    "send_email_summary",
    # Heavier reasoning (still keep latency by using only when needed)
    "call_model",
    # Voice state flips
    "voice_sleep",
    "voice_wake",
})

# Backwards-compat alias: a few callers (and tests) still reference the
# old name. Pointing it at the same set keeps them working.
_LIVE_READ_ONLY_TOOLS = _LIVE_ALLOWED_TOOLS

# Friendly error returned to Live if it ever tries to call a tool that
# isn't in the allowed set (shouldn't happen with the new prompt, but
# the guardrail stays).
_LIVE_WRITE_REJECTION_TEMPLATE = (
    "Tool '{name}' isn't available right now. Try a different approach or "
    "tell the user what you can do instead."
)


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
        # Setup audio buffer: while Gemini Live is still negotiating
        # `setupComplete`, attendee mic audio arrives but we have no
        # WebSocket to forward it on. Without buffering, the user's
        # opening utterance is silently dropped — that's the
        # "took 2-3 questions to get a response" UX bug.
        # We hold up to ~2 seconds of audio (capped to avoid OOM during
        # long setup hangs) and flush it the moment setup completes.
        self._setup_complete: bool = False
        self._setup_audio_buffer: list[dict] = []
        self._SETUP_BUFFER_MAX_FRAMES = 100  # ~2s at 50 frames/s

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

        # Reset setup state for reconnects so the audio buffer kicks back in
        # while the new session negotiates `setupComplete`.
        self._setup_complete = False
        self._setup_audio_buffer = []

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
                self._setup_complete = True
                # Flush any audio that arrived while setup was negotiating
                # — keeps the user's opening utterance from being lost.
                await self._flush_setup_audio_buffer()
                await self._persist_session_opened()
                # AWAKE BY DEFAULT: open the audio gate immediately so the
                # very first user utterance reaches Gemini Live. Without
                # this the gate stays closed until a finalized webhook
                # transcript triggers `is_addressed`, by which point the
                # opening utterance's audio has already been dropped —
                # producing the "have to say the name twice" UX bug.
                # The gate stays open until a sleep phrase, an explicit
                # close, or the long TTL elapses (the TTL is refreshed on
                # every speech event by `publish_gate_extend`).
                if not await self._is_voice_suspended():
                    await self.gate.open(
                        reason="session_default",
                        ttl_seconds=int(getattr(settings, "AGENT_GATE_DEFAULT_TTL_SECONDS", 1800)),
                    )
                    try:
                        from . import signals as _sig
                        _sig._set_gate_state(
                            self.bot_id, "session_default",
                            int(getattr(settings, "AGENT_GATE_DEFAULT_TTL_SECONDS", 1800)),
                        )
                    except Exception:
                        log.exception("live_session: redis gate state set failed bot=%s", self.bot_id)
                return
        raise RuntimeError("Gemini WS closed before setupComplete")

    async def _is_voice_suspended(self) -> bool:
        """User explicitly said sleep/quiet — keep gate closed until they wake it."""
        try:
            from . import signals as _sig
            return _sig.is_voice_suspended(self.bot_id)
        except Exception:
            return False

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

        # Gemini Live inline transcripts — instant, used for canvas display
        in_tr = server_content.get("inputTranscription") or {}
        if in_tr.get("text"):
            await self._log_transcript(
                kind="speech",
                speaker="User",
                text=in_tr["text"],
                finished=bool(in_tr.get("finished")),
                buf_key="_in_tr_buf",
            )
        out_tr = server_content.get("outputTranscription") or {}
        if out_tr.get("text"):
            await self._log_transcript(
                kind="speech",
                speaker="Clever Star",
                text=out_tr["text"],
                finished=bool(out_tr.get("finished")),
                buf_key="_out_tr_buf",
            )
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
                # Mark "bot is currently speaking" — used by the audio pump
                # to drop incoming mic audio for the duration of bot speech
                # + a short echo-tail window. Each audio frame extends
                # this. Set to 0.30s — long enough to swallow the
                # Attendee→Meet→back-to-Attendee echo loop (~150-250ms
                # round trip) without making real interrupts feel laggy.
                # When Gemini detects a real interrupt it resets this to
                # 0 immediately (see server_content.interrupted handler).
                self._bot_speaking_until = time.monotonic() + 0.30
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
            if name not in _LIVE_ALLOWED_TOOLS:
                # Should not happen — Gemini Live is only shown tools from
                # the allowed set. Guardrail in case the model hallucinates
                # a tool name. We don't defer to the Turn Processor here;
                # that path was the source of the "On it" / nothing-happens
                # bug.
                rejection = _LIVE_WRITE_REJECTION_TEMPLATE.format(name=name)
                responses.append(
                    {
                        "id": call_id,
                        "name": name,
                        "response": {"error": rejection},
                    }
                )
                await self._log_action_create(
                    name, args,
                    status="error",
                    error_msg=f"unknown tool: {name}",
                )
                continue

            # Log "pending" IMMEDIATELY so the canvas shows the in-flight action.
            entry_id = await self._log_action_create(name, args, status="pending")

            @sync_to_async
            def _exec(n=name, a=args):
                return execute_tool(n, a, {"bot_id": self.bot_id})

            import time as _time
            t0 = _time.time()
            try:
                result = await _exec()
                latency_ms = int((_time.time() - t0) * 1000)
                if isinstance(result, dict):
                    response_obj = result
                else:
                    response_obj = {"output": result}
                is_error = isinstance(result, dict) and result.get("error")
                await self._log_action_finish(
                    entry_id,
                    result=response_obj,
                    status="error" if is_error else "ok",
                    error_msg=(str(result.get("error"))[:300] if is_error else ""),
                    latency_ms=latency_ms,
                )
                responses.append({"id": call_id, "name": name, "response": response_obj})
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                latency_ms = int((_time.time() - t0) * 1000)
                log.exception("live_session: tool %s raised bot=%s", name, self.bot_id)
                await self._log_action_finish(
                    entry_id,
                    result={"error": err},
                    status="error",
                    error_msg=err[:300],
                    latency_ms=latency_ms,
                )
                responses.append({"id": call_id, "name": name, "response": {"error": err}})

        try:
            async with self._send_lock:
                await self._gemini_ws.send(
                    json.dumps({"toolResponse": {"functionResponses": responses}})
                )
        except Exception:
            log.exception("live_session: tool response send failed bot=%s", self.bot_id)
        # After tool calls finish, push a fresh canvas immediately so the
        # action log + any new visual show up without waiting for next tick.
        self._push_canvas_now()

    def _push_canvas_now(self) -> None:
        """Best-effort fire-and-forget canvas refresh after a state change."""
        try:
            from agent.canvas.pump import push_canvas_images_for_bot

            asyncio.create_task(self._push_canvas_async(push_canvas_images_for_bot))
        except Exception:
            pass

    def _kick_turn_processor(self) -> None:
        """Nudge the Turn Processor (Celery) to run a turn for this bot
        immediately, instead of waiting for the next scheduler tick.
        Used when Gemini Live attempts a write tool that we deferred."""
        try:
            from agent.turn_processor import process_meeting_turn

            asyncio.create_task(self._kick_turn_async(process_meeting_turn))
        except Exception:
            log.exception("_kick_turn_processor: failed bot=%s", self.bot_id)

    async def _kick_turn_async(self, fn) -> None:
        try:
            await sync_to_async(lambda: fn.delay(self.bot_id, None, "voice"))()
        except Exception:
            log.exception("_kick_turn_async: enqueue failed bot=%s", self.bot_id)

    async def _push_canvas_async(self, fn) -> None:
        try:
            await sync_to_async(fn)(self.bot_id)
        except Exception:
            log.exception("_push_canvas_async: failed bot=%s", self.bot_id)

    async def _log_transcript(
        self,
        kind: str,
        speaker: str,
        text: str,
        finished: bool,
        buf_key: str,
    ) -> None:
        """
        Gemini Live emits transcript fragments via inputTranscription /
        outputTranscription. Persist them as TranscriptEvent rows tagged
        `raw.source = "gemini_live"` so the canvas can show the live
        in-flight utterance (Attendee webhook transcripts arrive only on
        utterance-finalize, which is too slow for "user is speaking now"
        feedback).

        IMPORTANT: the scheduler and turn_processor filter these rows OUT
        (.exclude(raw__source="gemini_live")) so the agent loop never sees
        them — Attendee's webhook events are the canonical source for the
        brain. This is what prevents the "every utterance gets seen twice"
        bug while keeping live canvas feedback.
        """
        from datetime import datetime as _dt, timezone as _tz

        buf = getattr(self, buf_key, None)
        if buf is None:
            buf = {"text": "", "started_at": None}
            setattr(self, buf_key, buf)

        if not buf["started_at"]:
            buf["started_at"] = _dt.now(_tz.utc)
        buf["text"] += text

        @sync_to_async
        def _persist():
            try:
                from agent.models import TranscriptEvent

                ref = f"live:{kind}:{speaker}:{buf['started_at'].timestamp()}"
                TranscriptEvent.objects.update_or_create(
                    bot_id=self.bot_id,
                    utterance_ref=ref,
                    defaults={
                        "kind": kind,
                        "event_time": buf["started_at"],
                        "speaker": speaker,
                        "text": buf["text"],
                        "raw": {"finished": finished, "source": "gemini_live"},
                    },
                )
            except Exception:
                log.exception("_log_transcript: persist failed bot=%s", self.bot_id)

        try:
            await _persist()
        except Exception:
            log.exception("_log_transcript: wrapper failed")

        if finished:
            setattr(self, buf_key, {"text": "", "started_at": None})
            self._push_canvas_now()

    async def _log_action_create(
        self,
        tool_name: str,
        tool_input: dict,
        status: str = "pending",
        error_msg: str = "",
    ) -> str | None:
        """Create an ActionLogEntry up-front so the canvas shows the in-flight tool call."""
        import uuid as _uuid

        @sync_to_async
        def _save():
            try:
                from agent.models import ActionLogEntry

                e = ActionLogEntry.objects.create(
                    bot_id=self.bot_id,
                    turn_id=_uuid.uuid4(),
                    tool_name=tool_name,
                    tool_input=tool_input or {},
                    tool_result={},
                    status=status,
                    error_message=error_msg or "",
                )
                return str(e.id)
            except Exception:
                log.exception("_log_action_create: failed bot=%s tool=%s", self.bot_id, tool_name)
                return None

        try:
            return await _save()
        except Exception:
            log.exception("_log_action_create: wrapper failed")
            return None

    async def _log_action_finish(
        self,
        entry_id: str | None,
        result: dict,
        status: str,
        error_msg: str = "",
        latency_ms: int | None = None,
    ) -> None:
        """Update an existing ActionLogEntry to its final status."""
        if not entry_id:
            return

        @sync_to_async
        def _save():
            try:
                from agent.models import ActionLogEntry

                ActionLogEntry.objects.filter(id=entry_id).update(
                    tool_result=result or {},
                    status=status,
                    error_message=error_msg or "",
                    latency_ms=latency_ms,
                )
            except Exception:
                log.exception("_log_action_finish: failed entry=%s", entry_id)

        try:
            await _save()
        except Exception:
            log.exception("_log_action_finish: wrapper failed")

    async def _flush_setup_audio_buffer(self) -> None:
        """Send any audio captured during setup once Gemini is ready."""
        if not self._setup_audio_buffer:
            return
        buf = self._setup_audio_buffer
        self._setup_audio_buffer = []
        log.info(
            "live_session: flushing %d buffered audio frames bot=%s",
            len(buf), self.bot_id,
        )
        for frame in buf:
            try:
                async with self._send_lock:
                    await self._gemini_ws.send(
                        json.dumps(
                            {
                                "realtimeInput": {
                                    "audio": {
                                        "data": frame["data"],
                                        "mimeType": f"audio/pcm;rate={ATTENDEE_SAMPLE_RATE}",
                                    }
                                }
                            }
                        )
                    )
            except Exception:
                log.exception("live_session: setup-buffer flush failed bot=%s", self.bot_id)
                return

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
                pcm_b64 = pcm16_to_b64(pcm)
                # Buffer audio if Gemini Live isn't ready yet — keeps the
                # user's opening utterance from being silently dropped
                # during setup negotiation.
                if not self._setup_complete:
                    if len(self._setup_audio_buffer) < self._SETUP_BUFFER_MAX_FRAMES:
                        self._setup_audio_buffer.append({"data": pcm_b64})
                    continue
                try:
                    async with self._send_lock:
                        await self._gemini_ws.send(
                            json.dumps(
                                {
                                    "realtimeInput": {
                                        "audio": {
                                            "data": pcm_b64,
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
            signals.GATE_CLOSE_CHANNEL,
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
                    self._push_canvas_now()
                elif channel == signals.GATE_EXTEND_CHANNEL:
                    await self.gate.extend_if_open(
                        ttl_seconds=int(payload.get("ttl_seconds", 30)),
                    )
                elif channel == signals.GATE_CLOSE_CHANNEL:
                    await self.gate.close(reason=payload.get("reason", "sleep"))
                    self._push_canvas_now()
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
