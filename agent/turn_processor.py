"""
Turn Processor — background Celery task that is THE listener for the meeting.

For each invocation:
  1. Load the MeetingCursor with select_for_update (single-flight per bot).
  2. Pull the new TranscriptEvents since the cursor.
  3. Build full context via context_engine.builder (task="live_turn").
  4. Call Gemini 2.5 Flash with the 24-tool schema and let it make tool calls.
  5. Dispatch tool calls via execute_tool; persist ActionLogEntry per call.
  6. Advance cursor, push a voice-context briefing to Gemini Live, compress log.

This code is the single place the "should I act?" decision gets made —
Gemini Live becomes a pure voice renderer once this is online.
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

# Cost estimation lives in agent/llm_client.py now (OpenRouter-based).


# ── Public Celery entrypoint ──────────────────────────────────────────────────


@shared_task(
    name="agent.turn_processor.process_meeting_turn",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
    time_limit=45,
    soft_time_limit=40,
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

        # Pull the chunk (inclusive > cursor) — exclude self-utterances so we
        # don't feed the bot's own voice back into the LLM as user input.
        qs = TranscriptEvent.objects.filter(bot_id=bot_id).exclude(raw__self_utterance=True)
        if cursor.cursor_event_time:
            qs = qs.filter(event_time__gt=cursor.cursor_event_time)
        chunk = list(qs.order_by("event_time", "created_at")[:MAX_CHUNK_SIZE])

        if not chunk:
            return {"skipped": "no new events"}

        # Snapshot budget cap
        budget_cap = cursor.budget_cap_usd
        current_cost = cursor.total_cost_usd

    # Haiku is the brain for ALL tool calls and decisions.
    # "voice_conversation_active" just tells it whether the gate is open
    # (i.e., user is in active conversation) vs passive monitoring.
    # Either way, Haiku can speak — it's no longer suppressed.
    # The gate just tells us whether to prioritize a spoken reply.
    gate_open = False
    try:
        from agent.live_session.signals import is_gate_open

        gate_open = is_gate_open(bot_id)
    except Exception:
        log.exception("turn: gate-state check failed bot=%s", bot_id)
    if not gate_open:
        gate_open = bool(cursor.audio_gate_open)
    voice_conversation_active = gate_open

    # ── Build context + call Flash ────────────────────────────────────────────
    ctx_result = build_context(bot_id=bot_id, task="live_turn")

    chunk_payload = [_event_to_dict(e) for e in chunk]
    gemini_response = _call_gemini_with_tools(
        system_prompt=ctx_result["prompt_markdown"],
        chunk_events=chunk_payload,
        turn_id=str(turn_id),
        agent_name=ctx_result.get("agent_name", "Clever Star"),
        series=ctx_result.get("series"),
        priority=priority,
        voice_conversation_active=voice_conversation_active,
    )

    if gemini_response.get("error"):
        log.warning("turn: Gemini call failed bot=%s err=%s", bot_id, gemini_response["error"])
        # Still advance the cursor so we don't retry this exact chunk forever
        _advance_cursor(bot_id, chunk[-1], turn_id, cost_delta=Decimal("0"))
        return {
            "turn_id": str(turn_id),
            "chunk_size": len(chunk),
            "actions": [],
            "error": gemini_response["error"],
        }

    tool_calls = gemini_response.get("tool_calls", [])
    cost_delta = gemini_response.get("cost_usd", Decimal("0"))

    # ── Dispatch tool calls ───────────────────────────────────────────────────
    exec_ctx = {
        "bot_id": bot_id,
        "turn_id": str(turn_id),
        "series_id": (ctx_result.get("series") or {}).get("id"),
        "occurrence_id": _occurrence_id_for_bot(bot_id),
    }

    results = []
    from agent.tools import execute_tool

    # Derive the dominant trigger kind for this chunk.
    # If the chunk was triggered by a chat message, responses to the user
    # MUST go via send_chat_message (not voice), and vice versa.
    trigger_kind = _dominant_trigger_kind(chunk, priority)

    for call in tool_calls:
        tool_name = call.get("name", "")
        tool_input = call.get("args", {}) or {}
        if not tool_name:
            continue
        if not _is_tool_allowed(tool_name, ctx_result.get("series")):
            log.info("turn: tool %s blocked by series policy bot=%s", tool_name, bot_id)
            continue
        # Haiku is the brain — it always handles tool calls.
        # Only routing rule: chat trigger → chat reply, voice trigger → voice reply.
        if trigger_kind == "chat" and tool_name == "speak_via_voice":
            tool_name = "send_chat_message"
            tool_input = {"text": tool_input.get("text", ""), "to": "everyone"}
            log.info("turn: rerouted speak_via_voice → send_chat_message (chat trigger) bot=%s", bot_id)
        elif trigger_kind == "voice" and tool_name == "send_chat_message":
            tool_name = "speak_via_voice"
            tool_input = {"text": tool_input.get("text", "")}
            log.info("turn: rerouted send_chat_message → speak_via_voice (voice trigger) bot=%s", bot_id)

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
        try:
            result = execute_tool(tool_name, tool_input, exec_ctx)
            if isinstance(result, dict) and result.get("error"):
                entry.status = "error"
                entry.error_message = str(result.get("error"))[:2000]
            else:
                entry.status = "ok"
            entry.tool_result = _safe_jsonable(result) if isinstance(result, dict) else {"value": str(result)}
        except Exception as exc:
            entry.status = "error"
            entry.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            entry.tool_result = {}
            log.exception("turn: tool handler raised bot=%s tool=%s", bot_id, tool_name)

        entry.latency_ms = int((time.time() - started) * 1000)
        entry.save()
        results.append({"tool": tool_name, "status": entry.status})

    # ── Advance cursor + update cost ──────────────────────────────────────────
    _advance_cursor(bot_id, chunk[-1], turn_id, cost_delta=cost_delta)

    # ── Fire the voice briefing (best-effort) ────────────────────────────────
    _push_voice_briefing_safely(bot_id, turn_id)

    # ── Horizon compression when log gets long ────────────────────────────────
    try:
        maybe_compress_action_log(bot_id)
    except Exception:
        log.exception("turn: horizon compression failed bot=%s", bot_id)

    return {
        "turn_id": str(turn_id),
        "chunk_size": len(chunk),
        "actions": results,
        "cost_usd": str(cost_delta),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _event_to_dict(event) -> dict:
    return {
        "kind": event.kind,
        "event_time": event.event_time.isoformat() if event.event_time else None,
        "speaker": event.speaker,
        "text": event.text,
    }


def _dominant_trigger_kind(chunk: list, priority: str) -> str:
    """
    Decide whether THIS turn was fired by a chat message or a speech
    utterance, so the Turn Processor can route responses accordingly.

    Rule: if priority=="chat" OR the most recent event in the chunk is a
    chat message authored by someone other than the agent, the trigger is
    chat. Otherwise voice.
    """
    if priority == "chat":
        return "chat"
    agent_name = (getattr(settings, "AGENT_NAME", "") or "").lower()
    # Walk latest → oldest; first non-agent event determines the kind.
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
        # Fall back to stringifying non-serializable values
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
        return True  # empty list = all allowed

    # Map tool → category (mirrors agent/tools/ submodules)
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
    # voice / chat / visual (Phase 5f)
    "speak_via_voice": "voice",
    "send_chat_message": "chat",
    "read_recent_chat": "chat",
    "create_visual": "visual",
    "update_visual": "visual",
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
        # Live session module not yet loaded in this phase — safe to skip.
        pass
    except Exception:
        log.exception("_push_voice_briefing_safely: failed bot=%s", bot_id)


# ── Gemini Flash with tools (via OpenRouter) ──────────────────────────────────


def _call_gemini_with_tools(
    system_prompt: str,
    chunk_events: list[dict],
    turn_id: str,
    agent_name: str,
    series: Optional[dict],
    priority: str,
    voice_conversation_active: bool = False,
) -> dict:
    """
    Call the configured turn model with the full tool registry via OpenRouter.
    Returns the normalized dict shape produced by `agent.llm_client.chat_completion`.
    """
    from agent.llm_client import chat_completion
    from agent.tools import TOOL_REGISTRY
    from agent.tools.adapters import to_openai_function

    tool_schemas = [to_openai_function(t) for t in TOOL_REGISTRY.values()]

    model_name = getattr(settings, "AGENT_TURN_MODEL", "google/gemini-2.5-flash")
    user_text = _render_user_prompt(
        chunk_events, priority, voice_conversation_active=voice_conversation_active
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    return chat_completion(
        model=model_name,
        messages=messages,
        tools=tool_schemas,
        tool_choice="auto",
        temperature=0.3,
        max_tokens=1024,
    )


def _render_user_prompt(
    chunk_events: list[dict],
    priority: str,
    voice_conversation_active: bool = False,
) -> str:
    """Render the user-role message passed alongside the systemInstruction."""
    lines = ["## New transcript events since last turn", ""]
    for ev in chunk_events:
        ts = ev.get("event_time", "")[:19]
        kind = ev.get("kind", "speech")
        prefix = f"[{kind}]" if kind != "speech" else ""
        lines.append(f"- {ts} {prefix} {ev.get('speaker', '?')}: {ev.get('text', '')}")
    lines.append("")

    is_chat = priority == "chat" or any(
        e.get("kind") == "chat" for e in chunk_events[-3:]
    )

    if is_chat:
        lines.append(
            "## Chat reply\n"
            "The user addressed you in chat. Use `send_chat_message`. Keep it 1–2 sentences."
        )
    elif voice_conversation_active:
        lines.append(
            "## YOU ARE THE BRAIN — the user is talking to you\n"
            "Gemini Live is a pure voice renderer with NO reasoning, NO tools. "
            "You (Haiku) are 100% responsible for deciding what to say and do.\n\n"
            "**Your job this turn:**\n"
            "1. Read the latest user utterance carefully.\n"
            "2. Use whatever tools you need to fulfill the request.\n"
            "3. Call `speak_via_voice` with a SHORT spoken reply (1-2 sentences max).\n"
            "4. If the user asked for a visual: call `create_visual` WITH a full rich "
            "   HTML spec (see below), THEN call `speak_via_voice` confirming it's up.\n\n"
            "**Visualizations — generate RICH HTML:**\n"
            "- Use spec type 'html' with a complete self-contained HTML page.\n"
            "- Include inline CSS, real data, beautiful layout. Dark background (#0a0b0f) "
            "  with light text (#e5e7eb). Accent color: #a5b4fc.\n"
            "- Charts: use inline SVG bars/lines. Tables: styled. Lists: clean bullets.\n"
            "- NO external resources — everything inline.\n"
            "- Example: {\"type\": \"html\", \"html\": \"<!DOCTYPE html><html>...\", \"title\": \"...\"}\n\n"
            "**Decision rules:**\n"
            "- If the user asks to 'show', 'display', 'chart', 'visualize' anything → "
            "  immediately build the best HTML you can and call create_visual.\n"
            "- If the user asks a question → answer it with speak_via_voice.\n"
            "- If there's a clear action item → create_task, then confirm via voice.\n"
            "- Stay decisive: when given freedom ('you decide'), pick the best option and do it.\n"
            "- NEVER say you can't do something unless the tool returned an actual error."
        )
    else:
        lines.append(
            "## Background scan\n"
            "The user is not actively addressing you. Quietly capture any clear "
            "action items (create_task), shared URLs (save_artifact_from_url), or "
            "decisions worth saving. If nothing stands out, reply 'noop'.\n"
            "Do NOT call speak_via_voice unless the user clearly addressed you."
        )

    lines.append("")
    lines.append(
        "## Rules"
        "\n- Never fabricate UUIDs; use tool results."
        "\n- Prefer silence. 'noop' is always a valid response."
    )
    return "\n".join(lines)
