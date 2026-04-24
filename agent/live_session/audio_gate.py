"""
Audio gate — controls whether Attendee audio is forwarded into Gemini Live.

Default: closed. Opens on explicit signal (direct-address detected, speak_via_voice
tool call, or @agent chat mention). Self-closes after a TTL, and is also closed
explicitly when Gemini Live emits turnComplete (the voice is done responding).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("agent.live_session.audio_gate")


class AudioGate:
    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self.is_open = False
        self._auto_close_task: Optional[asyncio.Task] = None
        self._reason: str = ""

    @property
    def reason(self) -> str:
        return self._reason

    async def open(self, reason: str, ttl_seconds: int = 15) -> None:
        was_open = self.is_open
        self.is_open = True
        self._reason = reason
        await self._persist_state(open_state=True, reason=reason)
        if self._auto_close_task and not self._auto_close_task.done():
            self._auto_close_task.cancel()
        self._auto_close_task = asyncio.create_task(self._auto_close(ttl_seconds))
        if not was_open:
            log.info("audio_gate: OPEN bot=%s reason=%s ttl=%ds", self.bot_id, reason, ttl_seconds)
        else:
            log.info("audio_gate: EXTEND bot=%s reason=%s ttl=%ds", self.bot_id, reason, ttl_seconds)

    async def close(self, reason: str = "explicit") -> None:
        if self._auto_close_task and not self._auto_close_task.done():
            self._auto_close_task.cancel()
        if not self.is_open:
            return
        self.is_open = False
        self._reason = ""
        await self._persist_state(open_state=False, reason=reason)
        log.info("audio_gate: CLOSE bot=%s reason=%s", self.bot_id, reason)

    async def _auto_close(self, ttl_seconds: int) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
        except asyncio.CancelledError:
            return
        await self.close(reason="auto_ttl")

    async def _persist_state(self, open_state: bool, reason: str) -> None:
        """Reflect gate state into MeetingCursor for observability."""
        from asgiref.sync import sync_to_async

        from django.utils import timezone

        @sync_to_async
        def _save():
            from agent.models import MeetingCursor

            try:
                cursor, _ = MeetingCursor.objects.get_or_create(bot_id=self.bot_id)
                cursor.audio_gate_open = open_state
                cursor.audio_gate_opened_at = timezone.now() if open_state else None
                cursor.audio_gate_reason = reason[:64]
                cursor.save(update_fields=["audio_gate_open", "audio_gate_opened_at", "audio_gate_reason", "updated_at"])
            except Exception:
                log.exception("audio_gate: state persist failed bot=%s", self.bot_id)

        try:
            await _save()
        except Exception:
            log.exception("audio_gate: _persist_state wrapper failed")
