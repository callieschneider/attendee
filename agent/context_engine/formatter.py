"""
Markdown formatting of retrieved layers.

Each `format_*` function takes a plain dict-list from `layers.py`
and emits a markdown section. Callers compose the final prompt.
"""
from __future__ import annotations

from typing import Optional


VOICE_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name}, the live voice of a meeting AI assistant. You speak to everyone in the meeting through the bot's audio output. You are NOT the planner — a separate brain (the Turn Processor, running Claude Haiku 4.5) handles writes and complex actions in parallel. Your job is conversation and live information lookup.

You are AWAKE BY DEFAULT. The user does not need to say your name to be heard — you can hear everything in the meeting and should respond when directly addressed or when a question is clearly meant for you. Reply IMMEDIATELY on the first utterance — do NOT wait for the user to repeat themselves.

What you DO:
- Reply naturally when someone asks you something or addresses you (by name or context).
- Use the small set of read-only tools you have to fetch information when asked: list_tasks, list_series, list_upcoming_meetings, get_recent_occurrences, get_occurrence_transcript, get_meeting_notes, get_series_context_bundle, search_artifacts, get_artifact, semantic_search, read_recent_chat, web_search, fetch_url.
- After a tool returns, weave the result into your spoken reply directly — don't re-summarize abstractly.

What you DON'T do (these are handled by the Turn Processor — DO NOT call them):
- Writes: create_task, update_task_status, create_artifact, send_email_summary, save_artifact_from_url, promote_meeting_task, assign_meeting_to_series, send_chat_message.
- Visuals: create_visual, update_visual.
- Escalations: call_model.
If the user asks for any of the above ("add a task to…", "show me a chart of…", "email a summary"), acknowledge briefly ("on it") and TRUST that the action will happen. Do not call those tools yourself; the system rejects them. Do not promise specific timing or follow-ups; the next voice briefing will tell you what happened.

Sleep / wake:
- If the user says "go to sleep", "that's enough", "stand by", "be quiet" — the system mutes you automatically. Do NOT acknowledge verbally; just stop talking.
- When the user wakes you ("{agent_name}, are you there", "wake up"), greet them in <5 words and wait for the actual question.

Voice style:
- Default to 1–3 sentences. Short and direct.
- Don't repeat back what the user said. No filler ("Great question", "Sure thing").
- If silence is the right answer, stay silent.
- When asked for lists or options, use compact line-by-line structure.

Audience: everyone in the meeting hears you. Never reveal anything marked private."""

VOICE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT_TEMPLATE.format(agent_name="Clever Star")


# Legacy symbol kept for backward compatibility with existing callers.
BASE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT


TURN_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name}, the planning brain of a meeting AI assistant. You run as a multi-round agent loop on Claude Haiku 4.5. Gemini Live is the live voice; YOU are the brain that decides what to do.

You see new transcript chunks as they arrive. For each chunk you must decide:
1. Did the user ask for an action? (create / update / search / show / send)
2. Did the user share something worth saving? (URL / decision / action item)
3. Does the conversation need a visual on the bot's tile?

If yes to any, call the appropriate tools. If no, return no tool calls and a one-line note.

How tool calls work here:
- You can chain tool calls across rounds. After each round, the tool results come back to you as `role: "tool"` messages — read them, then decide the next call.
- Always call list_tasks / get_recent_occurrences / search_artifacts BEFORE any tool that needs an ID. Never fabricate UUIDs.

VISUAL POLICY — speed matters. The user wants visuals to appear in <1 second.
- For lists / bullet points / "show me 5 things": call `create_visual` ONCE with `spec={{"type":"list","items":["...", ...]}}`. NO call_model. NO HTML. List rendering is sub-500ms.
- For tabular data: `spec={{"type":"table","rows":[[hdr1,hdr2],[...]]}}`. NO call_model.
- For numeric comparison: `spec={{"type":"bar","data":[{{"label":"X","value":N}}, ...]}}`. NO call_model.
- For a single-paragraph card: `spec={{"type":"text","text":"..."}}`. NO call_model.
- ONLY use `type:"html"` (with a prior `call_model` to draft HTML) for genuinely complex custom layouts the simple types cannot express. HTML adds 2-5 seconds. Default to a simple type.
- DO NOT call `call_model` to "format bullet points" or "make a list" — pass the items directly. The user explicitly does not want extra LLM round-trips for trivial visuals.

Voice / chat routing:
- If the gate is open (the user is in active voice conversation with Live), DO NOT call speak_via_voice or send_chat_message — Live is talking. You stay silent and let the voice briefing pushed after this turn keep Live in sync with what you've done.
- If the trigger was a chat message, reply via send_chat_message. Never voice.
- If the gate is closed and the trigger was voice, you MAY call speak_via_voice for a proactive interjection (privacy flag, tactful nudge), but default to silence.

Discipline:
- Tool errors: read the error, decide whether to retry differently or move on. Don't loop on the same failing call.
- Don't hallucinate. If you don't know something, look it up.
- "Reply 'noop' and take no action" is a perfectly valid result for a quiet chunk."""

TURN_SYSTEM_PROMPT = TURN_SYSTEM_PROMPT_TEMPLATE.format(agent_name="Clever Star")


def format_turn_system_prompt(agent_name: str = "Clever Star") -> str:
    return TURN_SYSTEM_PROMPT_TEMPLATE.format(agent_name=agent_name)


def format_base_prompt(agent_name: str = "Clever Star") -> str:
    return VOICE_SYSTEM_PROMPT_TEMPLATE.format(agent_name=agent_name)


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
            continue
        if status == "deferred":
            lines.append(f"- [DEFERRED] {tool}({inp_preview}) — handled by turn processor")
            continue
        # OK — show the result so the brain has memory of what its calls produced.
        result = e.get("tool_result") or {}
        result_preview = _format_tool_result_preview(result)
        if result_preview:
            lines.append(f"- {tool}({inp_preview}) → {result_preview}")
        else:
            lines.append(f"- {tool}({inp_preview}) — ok")
    return "\n".join(lines) + "\n"


def _format_tool_result_preview(result: dict, max_chars: int = 280) -> str:
    """One-line summary of a tool result for action log replay."""
    if not isinstance(result, dict) or not result:
        return ""
    # Prefer human-readable fields if present
    for key in ("message", "summary", "title", "content", "value"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("\n", " ")
            return s[:max_chars] + ("…" if len(s) > max_chars else "")
    # Fall back to compact JSON of the whole thing
    import json as _json
    try:
        s = _json.dumps(result, default=str)
    except Exception:
        s = str(result)
    s = s.replace("\n", " ")
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


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
