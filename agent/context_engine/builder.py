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
        return int(getattr(settings, "AGENT_VOICE_SETUP_TOKEN_BUDGET", 4000))
    return int(getattr(settings, "AGENT_SEMANTIC_TOKEN_BUDGET", 10500))


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
    transcript = layers.get_recent_transcript(bot_id, last_n_events=60) if bot_id else []

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
        sections.append(formatter.format_transcript(transcript, max_events=40))
        sections.append(formatter.format_action_log(action_log))
    else:  # initial_voice_setup
        sections.append(formatter.format_current_occurrence(current_occ))
    sections.append(formatter.format_recent_meetings(recent_meetings))
    sections.append(formatter.format_open_tasks(open_tasks))
    sections.append(formatter.format_relevant_artifacts(artifacts))

    prompt = "\n".join(s for s in sections if s).strip()

    # Budget enforcement
    budget = _budget_for_task(task)
    prompt = truncate_text_to_budget(prompt, budget)

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

    parts.append("Stay silent unless directly addressed.")
    return " ".join(parts)


def _describe_action(entry: dict) -> str:
    tool = entry.get("tool_name", "")
    inp = entry.get("tool_input") or {}
    if tool == "create_task":
        return f"captured task '{inp.get('title', '?')}'"
    if tool == "create_artifact":
        return f"saved artifact '{inp.get('title', '?')}'"
    if tool == "promote_meeting_task":
        return "promoted an action item"
    if tool == "send_email_summary":
        return "sent the email summary"
    if tool == "send_chat_message":
        return f"posted in chat: \"{(inp.get('text') or '')[:60]}\""
    if tool == "speak_via_voice":
        return f"said \"{(inp.get('text') or '')[:60]}\""
    return f"ran {tool}"
