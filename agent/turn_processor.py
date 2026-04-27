"""
Turn Processor — background Celery task that is THE listener for the meeting.

For each invocation:
  1. Load the MeetingCursor with select_for_update (single-flight per bot).
  2. Pull the new TranscriptEvents since the cursor.
  3. Build full context via context_engine.builder (task="live_turn").
  4. Run a multi-round agent loop with the configured turn model:
        round 1: model -> assistant message + optional tool_calls
        round 1.5: execute tool_calls, push tool results as `role: "tool"` messages
        round 2: model sees tool results, decides next action
        ... up to AGENT_MAX_ROUNDS.
  5. Persist ActionLogEntry per executed call (with tool_result), advance cursor.
  6. Push a voice-context briefing to Gemini Live.

This is the single place the "should I act?" decision gets made — Gemini Live
is a pure voice renderer + read-only fast-path tool surface.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Optional

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

log = logging.getLogger("agent.turn_processor")

# Cap how many events we feed into a single turn (protects against burst storms)
MAX_CHUNK_SIZE = 80

# Tools that the live voice agent (Gemini Live) is responsible for while
# the audio channel is engaged. The Turn Processor blocks these to avoid
# duplicate / racing calls. Lookups stay open so the brain can investigate
# something silently if it wants — but it should rarely need to.
_VOICE_OWNED_TOOLS = frozenset({
    # Visuals
    "create_visual",
    "update_visual",
    # Capture
    "create_task",
    "update_task_status",
    "create_artifact",
    "save_artifact_from_url",
    "promote_meeting_task",
    "assign_meeting_to_series",
    # Channels
    "send_chat_message",
    "send_email_summary",
    "speak_via_voice",
    # Voice state
    "voice_sleep",
    "voice_wake",
    # Heavier reasoning is owned by the voice path too
    "call_model",
})

# Multi-round agent loop — how many model+tool roundtrips per turn.
# 5 is plenty for meeting context (most turns finish in 1-2 rounds; 5 gives
# headroom for chained ops like list_tasks -> update_task_status -> speak).
AGENT_MAX_ROUNDS = 5

# Per-tool-result truncation when we feed it back to the model.
# Keeps the loop's token cost bounded even when tools return large payloads.
TOOL_RESULT_MAX_CHARS = 2000

# Cost estimation lives in agent/llm_client.py now (OpenRouter-based).


# ── Public Celery entrypoint ──────────────────────────────────────────────────


@shared_task(
    name="agent.turn_processor.process_meeting_turn",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
    time_limit=90,
    soft_time_limit=80,
)
def process_meeting_turn(
    self,
    bot_id: str,
    cursor_event_time_iso: Optional[str] = None,
    priority: str = "normal",
) -> dict:
    """Run a single turn for the given bot. Returns a summary dict."""
    if not bot_id:
        return {"skipped": "no bot_id"}

    try:
        return _process_turn(bot_id, cursor_event_time_iso, priority)
    except Exception as exc:
        log.exception("process_meeting_turn: unexpected failure bot=%s", bot_id)
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        # Always release the scheduler's single-flight lock so the next
        # turn can enqueue as soon as we're done. Safe to call even if we
        # never acquired the lock (e.g. task retry paths).
        try:
            from agent.scheduler import release_inflight

            release_inflight(bot_id)
        except Exception:
            log.exception("process_meeting_turn: release_inflight failed bot=%s", bot_id)


def _process_turn(
    bot_id: str,
    cursor_event_time_iso: Optional[str],
    priority: str,
) -> dict:
    from agent.context_engine.builder import build_context
    from agent.context_engine.horizon import maybe_compress_action_log
    from agent.models import ActionLogEntry, MeetingCursor, TranscriptEvent

    turn_id = uuid.uuid4()

    # ── Acquire cursor ────────────────────────────────────────────────────────
    with transaction.atomic():
        try:
            cursor = MeetingCursor.objects.select_for_update().get(bot_id=bot_id)
        except MeetingCursor.DoesNotExist:
            log.info("_process_turn: no cursor for bot=%s", bot_id)
            return {"skipped": "no cursor"}

        if cursor.budget_exceeded:
            return {"skipped": "budget_exceeded"}

        # Staleness check — skip if another worker already advanced past us
        if (
            cursor_event_time_iso
            and cursor.cursor_event_time
            and cursor.cursor_event_time.isoformat() > cursor_event_time_iso
        ):
            return {"skipped": "cursor advanced"}

        # Pull the chunk (inclusive > cursor) — exclude:
        #   - self-utterances (bot's own TTS / Gemini outputTranscription)
        #   - in-flight gemini_live fragments (only finalized utterances feed
        #     the brain; partials are display-only on the canvas)
        from django.db.models import Q
        qs = (
            TranscriptEvent.objects.filter(bot_id=bot_id)
            .filter(Q(raw__self_utterance__isnull=True) | Q(raw__self_utterance=False))
            .filter(
                Q(raw__source__isnull=True)
                | ~Q(raw__source="gemini_live")
                | Q(raw__finished=True)
            )
        )
        if cursor.cursor_event_time:
            qs = qs.filter(event_time__gt=cursor.cursor_event_time)
        chunk = list(qs.order_by("event_time", "created_at")[:MAX_CHUNK_SIZE])

        if not chunk:
            return {"skipped": "no new events"}

    # Gate state — drives whether we route replies via voice (Live) vs chat,
    # and whether the Turn Processor should stay silent (Live is talking).
    gate_open = False
    try:
        from agent.live_session.signals import is_gate_open

        gate_open = is_gate_open(bot_id)
    except Exception:
        log.exception("turn: gate-state check failed bot=%s", bot_id)
    if not gate_open:
        gate_open = bool(cursor.audio_gate_open)
    voice_conversation_active = gate_open

    # ── Build context ────────────────────────────────────────────────────────
    ctx_result = build_context(bot_id=bot_id, task="live_turn")

    chunk_payload = [_event_to_dict(e) for e in chunk]
    trigger_kind = _dominant_trigger_kind(chunk, priority)
    exec_ctx = {
        "bot_id": bot_id,
        "turn_id": str(turn_id),
        "series_id": (ctx_result.get("series") or {}).get("id"),
        "occurrence_id": _occurrence_id_for_bot(bot_id),
    }

    # region agent log
    log.warning("DBG68285d C turn_running bot=%s priority=%s series_id=%s chunk=%d trigger=%s",
        bot_id, priority, exec_ctx.get("series_id"), len(chunk), trigger_kind)
    # endregion

    # ── Run the multi-round agent loop ───────────────────────────────────────
    loop_result = _run_agent_loop(
        bot_id=bot_id,
        turn_id=turn_id,
        system_prompt=ctx_result["prompt_markdown"],
        chunk_events=chunk_payload,
        priority=priority,
        voice_conversation_active=voice_conversation_active,
        trigger_kind=trigger_kind,
        chunk=chunk,
        exec_ctx=exec_ctx,
        series=ctx_result.get("series"),
    )

    if loop_result.get("error") and not loop_result.get("rounds"):
        # Hard failure on round 1 (model unavailable etc.) — DO NOT advance the
        # cursor; let the next turn retry the same chunk. Bump a per-cursor
        # failure counter and only skip the chunk after 3 consecutive failures.
        skip_chunk = _bump_failure_counter(bot_id) >= 3
        if skip_chunk:
            log.warning("turn: 3+ consecutive failures bot=%s — skipping chunk", bot_id)
            _advance_cursor(bot_id, chunk[-1], turn_id, cost_delta=Decimal("0"))
            _reset_failure_counter(bot_id)
        return {
            "turn_id": str(turn_id),
            "chunk_size": len(chunk),
            "actions": [],
            "rounds": 0,
            "error": loop_result["error"],
            "skipped_chunk": skip_chunk,
        }

    _reset_failure_counter(bot_id)

    # ── Advance cursor + update cost ──────────────────────────────────────────
    _advance_cursor(
        bot_id,
        chunk[-1],
        turn_id,
        cost_delta=loop_result.get("cost_usd", Decimal("0")),
    )

    # ── Fire the voice briefing (best-effort) ────────────────────────────────
    # Only if the gate is open (otherwise no one's listening).
    if voice_conversation_active:
        _push_voice_briefing_safely(bot_id, turn_id)

    # ── Horizon compression when log gets long ────────────────────────────────
    try:
        maybe_compress_action_log(bot_id)
    except Exception:
        log.exception("turn: horizon compression failed bot=%s", bot_id)

    return {
        "turn_id": str(turn_id),
        "chunk_size": len(chunk),
        "actions": loop_result.get("actions", []),
        "rounds": loop_result.get("rounds", 0),
        "cost_usd": str(loop_result.get("cost_usd", Decimal("0"))),
    }


# ── Multi-round agent loop ────────────────────────────────────────────────────


def _run_agent_loop(
    *,
    bot_id: str,
    turn_id: uuid.UUID,
    system_prompt: str,
    chunk_events: list[dict],
    priority: str,
    voice_conversation_active: bool,
    trigger_kind: str,
    chunk: list,
    exec_ctx: dict,
    series: Optional[dict],
) -> dict:
    """
    Port of abstraKt's `executeHeadlessAgent` loop, adapted for OpenRouter
    chat completions. Runs up to AGENT_MAX_ROUNDS rounds:
        model -> assistant{tool_calls} -> execute -> tool messages -> repeat
    Stops when the model emits no tool_calls (final reply).

    Returns: {
        "rounds": int,
        "actions": [{"tool": str, "status": str}],
        "cost_usd": Decimal,
        "final_text": str,
        "error": Optional[str],
    }
    """
    from agent.llm_client import chat_completion
    from agent.models import ActionLogEntry
    from agent.tools import TOOL_REGISTRY, execute_tool
    from agent.tools.adapters import to_openai_function

    tool_schemas = [to_openai_function(t) for t in TOOL_REGISTRY.values()]
    model_name = getattr(settings, "AGENT_TURN_MODEL", "anthropic/claude-haiku-4.5")
    user_text = _render_user_prompt(
        chunk_events,
        priority,
        voice_conversation_active=voice_conversation_active,
        trigger_kind=trigger_kind,
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    actions: list[dict] = []
    total_cost = Decimal("0")
    final_text = ""
    last_error: Optional[str] = None

    for round_idx in range(1, AGENT_MAX_ROUNDS + 1):
        resp = chat_completion(
            model=model_name,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=1024,
            timeout=45.0,
        )
        if resp.get("error"):
            log.warning(
                "turn: agent loop round %d failed bot=%s err=%s",
                round_idx, bot_id, resp["error"],
            )
            last_error = resp["error"]
            # If the very first round failed, propagate the error so caller
            # can decide whether to advance the cursor.
            if round_idx == 1:
                return {
                    "rounds": 0,
                    "actions": actions,
                    "cost_usd": total_cost,
                    "final_text": "",
                    "error": last_error,
                }
            # Mid-loop failure: stop, keep what we've done.
            break

        total_cost += resp.get("cost_usd", Decimal("0"))
        text_out = resp.get("text", "") or ""
        tool_calls = resp.get("tool_calls", []) or []

        # No tool calls — model is done. Capture final text and exit.
        if not tool_calls:
            final_text = text_out
            break

        # Append the assistant turn (with tool_calls) so the model has its
        # own history when we add tool result messages.
        assistant_msg: dict = {"role": "assistant", "content": text_out or ""}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{round_idx}_{i}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": json.dumps(tc.get("args", {}) or {}),
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
        messages.append(assistant_msg)

        # Execute each tool call, persist ActionLogEntry, append role:tool message.
        for i, call in enumerate(tool_calls):
            tool_name = call.get("name") or ""
            tool_input = call.get("args", {}) or {}
            call_id = call.get("id") or f"call_{round_idx}_{i}"

            if not tool_name:
                _push_tool_result(messages, call_id, {"error": "missing tool name"})
                continue

            if not _is_tool_allowed(tool_name, series):
                msg = f"Tool {tool_name} blocked by series policy"
                log.info("turn: %s bot=%s", msg, bot_id)
                _push_tool_result(messages, call_id, {"error": msg})
                continue

            # When the voice channel is engaged, the live voice agent owns
            # ALL user-facing tool calls. The Turn Processor's job there is
            # purely background capture — anything that races with the
            # voice agent gets blocked here. Silent observation is the goal.
            if voice_conversation_active and tool_name in _VOICE_OWNED_TOOLS:
                msg = (
                    f"blocked {tool_name}: voice agent owns user-facing tools "
                    f"while audio is engaged"
                )
                log.info("turn: %s bot=%s", msg, bot_id)
                _push_tool_result(messages, call_id, {"success": False, "skipped": msg})
                continue
            if trigger_kind == "chat" and tool_name == "speak_via_voice":
                tool_name = "send_chat_message"
                tool_input = {"text": tool_input.get("text", ""), "to": "everyone"}
                log.info("turn: rerouted speak_via_voice → send_chat_message bot=%s", bot_id)
            elif trigger_kind == "voice" and tool_name == "send_chat_message":
                msg = "suppressed send_chat_message: voice trigger"
                log.info("turn: %s bot=%s", msg, bot_id)
                _push_tool_result(messages, call_id, {"success": False, "skipped": msg})
                continue

            # Persist the call up-front
            entry = ActionLogEntry.objects.create(
                bot_id=bot_id,
                turn_id=turn_id,
                tool_name=tool_name,
                tool_input=tool_input,
                trigger_start_event=chunk[0],
                trigger_end_event=chunk[-1],
                status="pending",
            )

            started = time.time()
            # region agent log
            log.warning("DBG68285d C tool_executing bot=%s tool=%s input=%r", bot_id, tool_name, str(tool_input)[:200])
            # endregion
            try:
                result = execute_tool(tool_name, tool_input, exec_ctx)
                if isinstance(result, dict) and result.get("error"):
                    entry.status = "error"
                    entry.error_message = str(result.get("error"))[:2000]
                else:
                    entry.status = "ok"
                entry.tool_result = (
                    _safe_jsonable(result) if isinstance(result, dict) else {"value": str(result)}
                )
            except Exception as exc:
                entry.status = "error"
                entry.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                entry.tool_result = {"error": entry.error_message}
                result = {"error": entry.error_message}
                log.exception("turn: tool handler raised bot=%s tool=%s", bot_id, tool_name)

            entry.latency_ms = int((time.time() - started) * 1000)
            entry.save()

            actions.append({"tool": tool_name, "status": entry.status})
            _push_tool_result(messages, call_id, result)

        # Continue to next round so the model can react to the tool results.

    return {
        "rounds": round_idx if not last_error or round_idx > 1 else 0,
        "actions": actions,
        "cost_usd": total_cost,
        "final_text": final_text,
        "error": last_error,
    }


def _push_tool_result(messages: list[dict], call_id: str, result: Any) -> None:
    """Append an OpenAI-format tool result message, truncated for cost control."""
    try:
        as_str = json.dumps(result, default=str) if not isinstance(result, str) else result
    except Exception:
        as_str = str(result)
    if len(as_str) > TOOL_RESULT_MAX_CHARS:
        as_str = as_str[: TOOL_RESULT_MAX_CHARS - 30] + "…[truncated]"
    messages.append({"role": "tool", "tool_call_id": call_id, "content": as_str})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _event_to_dict(event) -> dict:
    return {
        "kind": event.kind,
        "event_time": event.event_time.isoformat() if event.event_time else None,
        "speaker": event.speaker,
        "text": event.text,
    }


def _dominant_trigger_kind(chunk: list, priority: str) -> str:
    """Whether THIS turn was fired by chat or voice. Routes responses."""
    if priority == "chat":
        return "chat"
    agent_name = (getattr(settings, "AGENT_NAME", "") or "").lower()
    for event in reversed(chunk):
        speaker_lower = (getattr(event, "speaker", "") or "").lower()
        if agent_name and speaker_lower == agent_name:
            continue
        if getattr(event, "kind", "") == "chat":
            return "chat"
        return "voice"
    return "voice"


def _safe_jsonable(obj) -> dict:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))


def _occurrence_id_for_bot(bot_id: str) -> Optional[str]:
    from agent.models import MeetingOccurrence

    try:
        occ = (
            MeetingOccurrence.objects.filter(bot__object_id=bot_id)
            .only("id")
            .order_by("-created_at")
            .first()
        )
        return str(occ.id) if occ else None
    except Exception:
        return None


def _is_tool_allowed(tool_name: str, series: Optional[dict]) -> bool:
    """Respect per-series `allowed_tool_categories` if set."""
    if not series:
        return True
    allowed = series.get("allowed_tool_categories") or []
    if not allowed:
        return True
    tool_category = _TOOL_CATEGORY.get(tool_name, "unknown")
    return tool_category in allowed


_TOOL_CATEGORY = {
    # meetings.py
    "get_recent_occurrences": "meetings",
    "get_occurrence_transcript": "meetings",
    "get_meeting_notes": "meetings",
    "list_upcoming_meetings": "meetings",
    # series.py
    "get_series_context_bundle": "series",
    "list_series": "series",
    "assign_meeting_to_series": "series",
    # tasks.py
    "list_tasks": "tasks",
    "create_task": "tasks",
    "update_task_status": "tasks",
    # artifacts.py
    "search_artifacts": "artifacts",
    "create_artifact": "artifacts",
    "get_artifact": "artifacts",
    # search.py
    "semantic_search": "search",
    # utility.py
    "web_search": "utility",
    "fetch_url": "utility",
    "save_artifact_from_url": "utility",
    "promote_meeting_task": "utility",
    "send_email_summary": "utility",
    # voice / chat / visual / models
    "speak_via_voice": "voice",
    "send_chat_message": "chat",
    "read_recent_chat": "chat",
    "create_visual": "visual",
    "update_visual": "visual",
    "call_model": "models",
}


def _advance_cursor(bot_id: str, last_event, turn_id: uuid.UUID, cost_delta: Decimal) -> None:
    from agent.models import MeetingCursor

    with transaction.atomic():
        try:
            cursor = MeetingCursor.objects.select_for_update().get(bot_id=bot_id)
        except MeetingCursor.DoesNotExist:
            return
        cursor.cursor_event_time = last_event.event_time
        cursor.cursor_event_created_at = last_event.created_at
        cursor.last_turn_id = turn_id
        cursor.last_turn_at = timezone.now()
        if cost_delta and cost_delta > 0:
            cursor.total_cost_usd = (cursor.total_cost_usd or Decimal("0")) + cost_delta
            if cursor.total_cost_usd >= cursor.budget_cap_usd:
                cursor.budget_exceeded = True
                log.warning(
                    "turn: budget exceeded bot=%s cost=%s cap=%s",
                    bot_id,
                    cursor.total_cost_usd,
                    cursor.budget_cap_usd,
                )
        cursor.save()


# Per-bot consecutive failure counter (kept in Redis if available, else in-memory).
_FAIL_COUNTER: dict[str, int] = {}


def _bump_failure_counter(bot_id: str) -> int:
    try:
        from agent.live_session.signals import _get_redis  # type: ignore

        r = _get_redis()
        if r is not None:
            key = f"agent:turn_fail:{bot_id}"
            count = int(r.incr(key))
            r.expire(key, 600)
            return count
    except Exception:
        pass
    _FAIL_COUNTER[bot_id] = _FAIL_COUNTER.get(bot_id, 0) + 1
    return _FAIL_COUNTER[bot_id]


def _reset_failure_counter(bot_id: str) -> None:
    try:
        from agent.live_session.signals import _get_redis  # type: ignore

        r = _get_redis()
        if r is not None:
            r.delete(f"agent:turn_fail:{bot_id}")
    except Exception:
        pass
    _FAIL_COUNTER.pop(bot_id, None)


def _push_voice_briefing_safely(bot_id: str, turn_id: uuid.UUID) -> None:
    try:
        from agent.context_engine.builder import build_context
        from agent.live_session.voice_pump import enqueue_voice_briefing

        briefing = build_context(bot_id=bot_id, task="voice_briefing")
        enqueue_voice_briefing(
            bot_id=bot_id,
            text=briefing["prompt_markdown"],
            turn_id=str(turn_id),
        )
    except ImportError:
        pass
    except Exception:
        log.exception("_push_voice_briefing_safely: failed bot=%s", bot_id)


# ── User-prompt rendering for the Turn Processor ──────────────────────────────


def _render_user_prompt(
    chunk_events: list[dict],
    priority: str,
    voice_conversation_active: bool = False,
    trigger_kind: str = "voice",
) -> str:
    """Direct user-role message: just the new transcript, plus a one-line
    instruction matching the trigger. The rules + identity live in the
    system prompt — DON'T re-declare them here."""
    lines = ["## New transcript events since last turn", ""]
    for ev in chunk_events:
        ts = (ev.get("event_time") or "")[:19]
        kind = ev.get("kind", "speech")
        prefix = f"[{kind}]" if kind != "speech" else ""
        lines.append(f"- {ts} {prefix} {ev.get('speaker', '?')}: {ev.get('text', '')}")
    lines.append("")

    if trigger_kind == "chat":
        lines.append(
            "Trigger: chat message. Reply via `send_chat_message` (1–2 "
            "sentences). Do NOT use `speak_via_voice`."
        )
    elif voice_conversation_active:
        lines.append(
            "Voice channel is ACTIVELY engaged. The voice agent is replying "
            "and calling its own tools (visuals, tasks, artifacts, chat, "
            "email, voice_sleep/wake). Default to NOOP. Do NOT call any "
            "user-facing tool here — duplicates will race the voice agent. "
            "Only act if the chunk reveals a clear action item the voice "
            "agent demonstrably missed (e.g., a URL shared as an aside that "
            "no one acknowledged). Otherwise reply with no tool calls and "
            "a one-line note."
        )
    else:
        lines.append(
            "Voice channel is QUIET. Capture clear missed items: explicit "
            "action items (`create_task`), shared URLs "
            "(`save_artifact_from_url`), decisions worth saving. If nothing "
            "stands out, reply with no tool calls and a short note."
        )
    return "\n".join(lines)
