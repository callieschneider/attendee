"""
Top-level context builder — orchestrates retrieval layers, scoring,
MMR diversity, dedup, and token budgeting into a single prompt string.

Three `task` modes:
  - "live_turn"           : full detail for the Turn Processor (Gemini Flash with tools)
  - "voice_briefing"      : compact prose for mid-session updates to Gemini Live
  - "initial_voice_setup" : medium detail for the first systemInstruction of a Live session
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from django.conf import settings

from . import formatter, layers
from .budget import count_tokens, truncate_text_to_budget
from .dedup import deduplicate_items
from .mmr import mmr_rerank
from .scoring import blend_score, recency_score

log = logging.getLogger("agent.context_engine.builder")


def _agent_name(series: Optional[dict]) -> str:
    if series and series.get("agent_name_override"):
        return series["agent_name_override"]
    return getattr(settings, "AGENT_NAME", "Clever Star")


def _budget_for_task(task: str) -> int:
    if task == "voice_briefing":
        return int(getattr(settings, "AGENT_VOICE_BRIEFING_TOKEN_BUDGET", 350))
    if task == "initial_voice_setup":
        # Bumped from 4k → 24k so a session resume after Gemini Live's ~10-min
        # cap can carry the full in-progress meeting transcript. Gemini 2.5
        # Flash supports a 1M context — 24k is conservative and keeps cost
        # predictable while spanning a typical meeting.
        return int(getattr(settings, "AGENT_VOICE_SETUP_TOKEN_BUDGET", 24000))
    # live_turn — Haiku 4.5 has a 200k context window, but typical turns only
    # need recent conversation. Bumped from 10.5k → 24k so the agent can
    # actually answer "what did we discuss earlier?" mid-meeting.
    return int(getattr(settings, "AGENT_SEMANTIC_TOKEN_BUDGET", 24000))


def build_context(
    bot_id: Optional[str] = None,
    series_id: Optional[str] = None,
    occurrence_id: Optional[str] = None,
    query: Optional[str] = None,
    task: str = "live_turn",
    now=None,
) -> dict:
    """
    Assemble a context payload for the given task.

    Returns:
        {
            "prompt_markdown": str,
            "token_count": int,
            "layer_summary": dict,   # diagnostic counts by layer
            "agent_name": str,
            "series": dict | None,
        }
    """
    # Resolve IDs from bot_id if needed
    if bot_id and not series_id:
        series_id = layers.resolve_series_id_for_bot(bot_id)
    if bot_id and not occurrence_id:
        occurrence_id = layers.resolve_occurrence_id_for_bot(bot_id)

    series = layers.get_series_config(series_id)
    agent_name = _agent_name(series)

    # Retrieve layers
    pinned = layers.get_pinned_context_items(series_id)
    recent_meetings = layers.get_recent_occurrence_summaries(series_id)
    open_tasks = layers.get_open_tasks(series_id)
    artifacts = layers.get_relevant_artifacts(query, series_id, limit=5)
    current_occ = layers.get_current_occurrence(occurrence_id)
    action_log = layers.get_recent_action_log(bot_id, limit=20) if bot_id else []
    # Pull a wide transcript window so the agent has memory across the whole
    # meeting. format_transcript + truncate_text_to_budget will trim if the
    # token budget overflows. 500 events ≈ 60-90 min of typical conversation.
    transcript = layers.get_recent_transcript(bot_id, last_n_events=500) if bot_id else []

    # Dedup artifacts vs. pinned vs. recent meetings by content similarity
    artifacts = deduplicate_items(
        artifacts,
        get_text=lambda a: f"{a.get('title', '')} {a.get('content', '')}",
        threshold=0.9,
    )

    # Assemble — different tasks pick different sections + system prompts
    if task == "live_turn":
        system_prompt = formatter.format_turn_system_prompt(agent_name)
    else:
        system_prompt = formatter.format_base_prompt(agent_name)
    sections: list[str] = [system_prompt, ""]

    if task == "voice_briefing":
        # Compact prose — no sections, use a running paragraph
        sections.append(_voice_briefing(series, action_log, transcript))
        prompt = "\n".join(s for s in sections if s)
        prompt = truncate_text_to_budget(prompt, _budget_for_task(task))
        return {
            "prompt_markdown": prompt,
            "token_count": count_tokens(prompt),
            "layer_summary": {
                "actions": len(action_log),
                "transcript": len(transcript),
            },
            "agent_name": agent_name,
            "series": series,
        }

    # live_turn and initial_voice_setup share the same structure with different budgets
    sections.append(formatter.format_series(series))
    sections.append(formatter.format_pinned(pinned))
    if task == "live_turn":
        sections.append(formatter.format_current_occurrence(current_occ))
        # Show as much conversation history as we have — truncate_text_to_budget
        # will trim from the bottom if we overflow. Spanning the full meeting
        # is the goal; 300 events covers typical 1h calls.
        sections.append(formatter.format_transcript(transcript, max_events=300))
        sections.append(formatter.format_action_log(action_log))
    else:  # initial_voice_setup
        sections.append(formatter.format_current_occurrence(current_occ))
        # Re-injecting the full transcript on every Gemini Live (re)open is
        # what gives the agent memory across the ~10-min Live session cap.
        # Without this, every session resume forgets everything said earlier.
        sections.append(formatter.format_transcript(transcript, max_events=300))
        sections.append(formatter.format_action_log(action_log))
    sections.append(formatter.format_recent_meetings(recent_meetings))
    sections.append(formatter.format_open_tasks(open_tasks))
    sections.append(formatter.format_relevant_artifacts(artifacts))

    prompt = "\n".join(s for s in sections if s).strip()

    # Budget enforcement
    budget = _budget_for_task(task)
    pre_truncate_tokens = count_tokens(prompt)
    prompt = truncate_text_to_budget(prompt, budget)
    post_truncate_tokens = count_tokens(prompt)

    # region agent log
    try:
        import logging as _logging
        _log = _logging.getLogger("agent.context_engine")
        _log.warning(
            "DBG68285d F context bot=%s task=%s transcript_events=%d budget=%d pre_tokens=%d post_tokens=%d truncated=%s",
            bot_id, task, len(transcript), budget, pre_truncate_tokens, post_truncate_tokens,
            pre_truncate_tokens > post_truncate_tokens,
        )
    except Exception:
        pass
    # endregion

    return {
        "prompt_markdown": prompt,
        "token_count": count_tokens(prompt),
        "layer_summary": {
            "pinned": len(pinned),
            "recent_meetings": len(recent_meetings),
            "open_tasks": len(open_tasks),
            "artifacts": len(artifacts),
            "action_log": len(action_log),
            "transcript": len(transcript),
        },
        "agent_name": agent_name,
        "series": series,
    }


def _voice_briefing(
    series: Optional[dict],
    action_log: list[dict],
    transcript: list[dict],
) -> str:
    """Build a compact prose briefing for Gemini Live."""
    parts: list[str] = []
    if series:
        parts.append(f"Current meeting series: {series['title']}.")
    # Latest actions (oldest → newest, last 5)
    if action_log:
        recent_actions = action_log[-5:]
        summaries = []
        for e in recent_actions:
            if e.get("is_archived") and e.get("tool_name") == "_horizon_summary":
                summaries.append(
                    (e.get("tool_result") or {}).get("summary", "")
                )
                continue
            if e.get("status") == "ok":
                summaries.append(_describe_action(e))
        if summaries:
            parts.append("Recently I: " + "; ".join(s for s in summaries if s) + ".")
    # Latest 2 utterances
    if transcript:
        tail = transcript[-2:]
        tail_text = " / ".join(f"{ev.get('speaker', '?')}: {(ev.get('text') or '')[:120]}" for ev in tail)
        parts.append(f"Last heard — {tail_text}.")

    return " ".join(parts)


def _describe_action(entry: dict) -> str:
    tool = entry.get("tool_name", "")
    inp = entry.get("tool_input") or {}
    res = entry.get("tool_result") or {}
    if tool == "create_task":
        return f"captured task '{inp.get('title', '?')}'"
    if tool == "create_artifact":
        return f"saved artifact '{inp.get('title', '?')}'"
    if tool == "create_visual":
        title = inp.get("title") or (res.get("title") if isinstance(res, dict) else "")
        return f"put a visual on the bot tile: '{title or 'untitled'}'"
    if tool == "update_visual":
        return "updated the visual on the bot tile"
    if tool == "update_task_status":
        return f"set task {inp.get('task_id', '?')[:8]} to {inp.get('status', '?')}"
    if tool == "promote_meeting_task":
        return "promoted an action item"
    if tool == "send_email_summary":
        return "sent the email summary"
    if tool == "save_artifact_from_url":
        return f"saved a link: {inp.get('url', '')[:80]}"
    if tool == "send_chat_message":
        return f"posted in chat: \"{(inp.get('text') or '')[:60]}\""
    if tool == "speak_via_voice":
        return f"said \"{(inp.get('text') or '')[:60]}\""
    if tool == "call_model":
        return f"asked {inp.get('model', '?')} for help"
    return f"ran {tool}"
