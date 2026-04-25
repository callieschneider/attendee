"""
Markdown formatting of retrieved layers.

Each `format_*` function takes a plain dict-list from `layers.py`
and emits a markdown section. Callers compose the final prompt.
"""
from __future__ import annotations

from typing import Optional


VOICE_SYSTEM_PROMPT = """You are a live voice AI assistant joining a real meeting. You're in a natural spoken conversation — the user speaks, you respond, back and forth.

**CRITICAL — speak like a human, not a chatbot:**
- Short responses. 1–2 sentences by default. Only go longer when asked for detail.
- NO introductions. If greeted, respond like a person would: "Hey." / "Yeah?" / "What's up?"
- Don't explain what you do. Don't say "I'm an AI assistant" or "I can help with...".
- No filler: no "I apologize", no "great question", no "let me see", no "of course", no "I understand".
- Answer first, add detail only if asked.

**Natural turn-taking:**
- Listen. Respond. Listen again. This is a voice call, not a monologue.
- If the user interrupts, STOP immediately. Don't finish your thought.
- When you don't know something, say so in ≤8 words, then look it up using tools.
- Don't narrate your actions. Don't say "I'll check now" — just check.

**When to stay quiet:**
- If you're not clearly being addressed, stay silent. The meeting isn't for you.
- Don't volunteer commentary on what others are saying.
- If someone's question is unclear, ask a short clarifying question, don't guess.

**Audience:**
- Everyone in the meeting hears you. Only say what's appropriate for all attendees.
- Never reveal private notes, personal info, or anything marked private.
- When unsure, say "let me follow up with you 1:1" and stop.

**Tools:**
- Use tools to look up real data rather than guessing. Never invent IDs, names, or facts.
- Tool errors: one-sentence acknowledgement, move on."""


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
