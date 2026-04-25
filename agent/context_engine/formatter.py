"""
Markdown formatting of retrieved layers.

Each `format_*` function takes a plain dict-list from `layers.py`
and emits a markdown section. Callers compose the final prompt.
"""
from __future__ import annotations

from typing import Optional


VOICE_SYSTEM_PROMPT = """You are Clever Star, a meeting AI assistant in a Gemini Live realtime voice session. You have a set of tools available — USE THEM to look up real data. Never make up facts, IDs, or tool results.

Voice and transcript rules:
- Default to concise spoken answers. 1–3 sentences unless the user asks for detail.
- Don't repeat back what the user said.
- Don't add filler like "That's a great question" or "Sure thing."
- If the answer is short, keep it short. Silence is fine.
- The transcript also shows on the bot's video tile in the meeting. When the user asks for lists, options, comparisons, or plans, use compact line-by-line structure (one item per line, numbers or bullets).
- If tool results contain concrete items, quote or enumerate them directly instead of vaguely summarizing.

Tool use:
- When the user asks for information, CALL the relevant tool. Do not pretend you called it.
- If a tool returns `success: true` or a `message` saying it's done, IT WORKED — confirm briefly.
- If you're stuck or need heavy reasoning, call `call_model` with model "anthropic/claude-haiku-4.5" or "anthropic/claude-sonnet-4.5" and a clear task prompt. Use the result as your answer.
- For visualizations on the bot's video tile, call `create_visual` with a spec. For complex layouts, first call `call_model` to generate rich HTML (theme: dark bg #0a0b0f, light text #e5e7eb, accent #a5b4fc, inline CSS+SVG only) then pass the HTML via {"type":"html","html":"...","title":"..."}.

Audience: everyone in the meeting hears you. Only share what's appropriate for all attendees. Never reveal private notes or anything marked private."""


# Legacy symbol kept for backward compatibility with existing callers.
BASE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT


TURN_SYSTEM_PROMPT = """You are the silent-action half of a meeting assistant. Gemini Live is handling spoken replies; your job is different.

**What you do:**
- Watch the transcript for decisions, action items, shared URLs, and facts worth saving.
- Use tools to capture them: create_task, create_artifact, save_artifact_from_url, send_email_summary, create_visual.
- When chat-mentioned, reply via send_chat_message (NEVER voice — chat is chat).
- If nothing worth capturing, reply 'noop' and take no action.

**When NOT to speak via voice:**
- If audio_gate_open is true, Gemini Live is already talking to the user. You MUST NOT call speak_via_voice.
- Default behavior is silence. Only call speak_via_voice for proactive interjections the voice agent cannot provide (e.g., flagging a privacy concern).

**Tool discipline:**
- list_tasks or get_recent_occurrences BEFORE calling update_task_status or anything with IDs.
- Never fabricate UUIDs. All IDs must come from tool results.
- Tool errors: acknowledge in one sentence, move on."""


def format_turn_system_prompt(agent_name: str = "Clever Star") -> str:
    return TURN_SYSTEM_PROMPT.replace(
        "silent-action half of a meeting assistant",
        f"silent-action half of {agent_name}",
        1,
    )


def format_base_prompt(agent_name: str = "Clever Star") -> str:
    # Voice persona — keep it natural and conversational.
    return VOICE_SYSTEM_PROMPT.replace(
        "You are a live voice AI assistant",
        f"You are {agent_name}, a live voice AI assistant",
        1,
    )


def format_series(series: Optional[dict]) -> str:
    if not series:
        return ""
    lines = [f"## Meeting Series: {series['title']}"]
    if series.get("description"):
        lines.append(series["description"])
    tags = series.get("tags") or []
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    verbosity = series.get("agent_verbosity") or "normal"
    proactivity = series.get("agent_proactivity") or "reactive"
    lines.append(f"Behavior: verbosity={verbosity}, proactivity={proactivity}.")
    return "\n".join(lines) + "\n"


def format_pinned(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["## Pinned Context", ""]
    for p in items:
        label = f"**{p['label']}**: " if p.get("label") else ""
        lines.append(f"- {label}{p.get('content', '')}")
    return "\n".join(lines) + "\n"


def format_recent_meetings(occurrences: list[dict]) -> str:
    if not occurrences:
        return ""
    lines = ["## Recent Meetings", ""]
    for occ in occurrences:
        date_str = (
            occ["started_at"].strftime("%Y-%m-%d") if occ.get("started_at") else "unknown"
        )
        title = occ.get("title") or f"Meeting {date_str}"
        lines.append(f"### {title} ({date_str})")
        lines.append(occ.get("summary", "")[:400])
        lines.append("")
    return "\n".join(lines)


def format_open_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return ""
    lines = ["## Open Tasks", ""]
    for t in tasks:
        due = f" (due {t['due_date']})" if t.get("due_date") else ""
        owner = f" — {t['owner']}" if t.get("owner") else ""
        lines.append(f"- [{t.get('priority', 'medium')}] **{t['title']}**{due}{owner}")
    return "\n".join(lines) + "\n"


def format_relevant_artifacts(artifacts: list[dict]) -> str:
    if not artifacts:
        return ""
    lines = ["## Relevant Artifacts", ""]
    for a in artifacts:
        sim = (
            f" (similarity {a['similarity']:.2f})"
            if a.get("similarity") is not None
            else ""
        )
        header = f"- **{a['title']}** [{a.get('type', 'note')}]{sim}"
        lines.append(header)
        content = (a.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"  > {content[:240]}")
        if a.get("url"):
            lines.append(f"  {a['url']}")
    return "\n".join(lines) + "\n"


def format_current_occurrence(occ: Optional[dict]) -> str:
    if not occ:
        return ""
    lines = ["## This Meeting"]
    if occ.get("title"):
        lines.append(f"**{occ['title']}**")
    if occ.get("summary"):
        lines.append(occ["summary"])
    atts = occ.get("attendees") or []
    if atts:
        lines.append(f"Attendees: {', '.join(atts)}")
    return "\n".join(lines) + "\n"


def format_action_log(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = ["## Recent Actions Taken", ""]
    for e in entries:
        if e.get("is_archived") and e.get("tool_name") == "_horizon_summary":
            lines.append(f"- (summary) {e.get('tool_result', {}).get('summary', '')}")
            continue
        status = e.get("status", "ok")
        tool = e.get("tool_name", "?")
        inp = e.get("tool_input") or {}
        inp_preview = ", ".join(f"{k}={str(v)[:30]}" for k, v in list(inp.items())[:3])
        if status == "error":
            msg = (e.get("error_message") or "").replace("\n", " ")[:80]
            lines.append(f"- [FAILED] {tool}({inp_preview}) — {msg}")
        else:
            lines.append(f"- {tool}({inp_preview}) — ok")
    return "\n".join(lines) + "\n"


def format_transcript(events: list[dict], max_events: int = 30) -> str:
    if not events:
        return ""
    # Limit to the last N events to keep token usage reasonable
    events = events[-max_events:]
    lines = ["## Recent Conversation", ""]
    for ev in events:
        ts = ev["event_time"].strftime("%H:%M:%S") if ev.get("event_time") else "??"
        speaker = ev.get("speaker") or "?"
        kind = ev.get("kind", "speech")
        prefix = f"[{kind}]" if kind != "speech" else ""
        lines.append(f"- {ts} {prefix} **{speaker}**: {ev.get('text', '')}")
    return "\n".join(lines) + "\n"


def format_chunk_events(events: list[dict]) -> str:
    """
    Format brand-new TranscriptEvents (since last turn) — used as the
    direct user message to the Turn Processor.
    """
    if not events:
        return "(no new events)"
    lines = []
    for ev in events:
        ts = ev["event_time"].strftime("%H:%M:%S") if ev.get("event_time") else "??"
        speaker = ev.get("speaker") or "?"
        kind = ev.get("kind", "speech")
        prefix = f"[{kind}]" if kind != "speech" else ""
        lines.append(f"- {ts} {prefix} **{speaker}**: {ev.get('text', '')}")
    return "\n".join(lines)
