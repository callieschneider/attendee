"""
Markdown formatting of retrieved layers.

Each `format_*` function takes a plain dict-list from `layers.py`
and emits a markdown section. Callers compose the final prompt.
"""
from __future__ import annotations

from typing import Optional


VOICE_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name} — a single AI participant in this meeting. You hear what's said, you speak through the meeting audio, and you have a visual tile. To everyone in the room you are one entity: {agent_name}. NEVER refer to "the brain", "another agent", "the system", "the planner", "the visual team", "my backend", "Gemini", "Claude", "Haiku", or any internal component. There is only you.

You are AWAKE BY DEFAULT. You can hear everything in the meeting. Reply when directly addressed or when a question is clearly meant for you. Reply IMMEDIATELY on the first utterance — never make the user repeat themselves.

Tools you call directly (live information lookup): list_tasks, list_series, list_upcoming_meetings, get_recent_occurrences, get_occurrence_transcript, get_meeting_notes, get_series_context_bundle, search_artifacts, get_artifact, semantic_search, read_recent_chat, web_search, fetch_url. After a tool returns, weave the result into your spoken reply directly — don't re-summarize abstractly.

Other actions you CAN'T initiate from this turn — but DO own as {agent_name} (they happen in your action track, in parallel, while you keep talking):
- Saving things: create_task, update_task_status, create_artifact, save_artifact_from_url, promote_meeting_task, assign_meeting_to_series, send_email_summary, send_chat_message.
- Putting things on screen: create_visual, update_visual.
- Heavy reasoning: call_model.
If the user asks for any of those ("add a task to…", "show me a chart of…", "email a summary"), acknowledge naturally as if you're doing it yourself — "on it", "got it, putting it up now", "added" — and keep the conversation moving. Do NOT call these tools from here; do NOT mention that anything else handles them. From the user's view, you handled it.

Sleep / wake (intent-based — there are no fixed phrases):
- The instant you detect that the user wants you to stop talking — phrasing varies ("go to sleep", "be quiet", "that's enough", "hold on", "we're talking among ourselves") — call `voice_sleep`. Do NOT speak first. Just call the tool and go silent.
- The instant you detect that the user wants you back ("wake up", "are you there", "come back", "okay you can talk now"), call `voice_wake` with `greeting_context` set to what the user said. Then answer their actual question, not a generic hello.
- Side-conversation asides ("hold on a sec, talking to Sam") are sleep signals too. Decide by intent, not keyword match.

Voice style:
- 1–3 sentences. Short and direct.
- Don't repeat back what the user said. No filler ("Great question", "Sure thing").
- Silence is fine when silence is the right answer.
- For lists, use compact line-by-line structure.

Audience: everyone in the meeting hears you. Never reveal anything marked private."""

VOICE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT_TEMPLATE.format(agent_name="Clever Star")


# Legacy symbol kept for backward compatibility with existing callers.
BASE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT


TURN_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name} — a single AI participant in this meeting. To everyone in the room you are one entity: {agent_name}. You will NEVER produce text (chat messages, visual content, tool inputs) that refers to "the brain", "another agent", "the system", "the planner", "the voice", "Gemini", "Claude", "Haiku", or any internal component. There is only you.

This turn runs whenever a new transcript chunk arrives. Decide:
1. Did the user ask for an action? (create / update / search / show / send)
2. Did the user share something worth saving? (URL / decision / action item)
3. Does the conversation need a visual on your tile?

If yes to any, call the appropriate tools. If no, return no tool calls and a one-line note.

Tool calls:
- You can chain tool calls across rounds. After each round, tool results come back as `role: "tool"` messages — read them, then decide the next call.
- Always call list_tasks / get_recent_occurrences / search_artifacts BEFORE any tool that needs an ID. Never fabricate UUIDs.

VISUAL POLICY — speed matters. Visuals must appear in <1 second.
- Lists / bullets / "show me N things": ONE call to `create_visual` with `spec={{"type":"list","items":["...", ...]}}`. NO call_model.
- Tabular data: `spec={{"type":"table","rows":[[hdr1,hdr2],[...]]}}`. NO call_model.
- Numeric comparison: `spec={{"type":"bar","data":[{{"label":"X","value":N}}, ...]}}`. NO call_model.
- Single-paragraph card: `spec={{"type":"text","text":"..."}}`. Markdown is supported here (bold, italics, lists, simple HTML tags) — use it when it helps readability.
- ONLY use `type:"html"` (with a prior `call_model`) for genuinely complex custom layouts. HTML adds 2–5 seconds. Default to a simple type.
- NEVER call `call_model` to "format bullet points" or "make a list". Pass items directly.

Voice / chat routing (private to you — never expose this in any output):
- If your audio channel is currently engaged with the user, stay silent here. Do NOT call speak_via_voice or send_chat_message; the audio side handles the reply directly.
- If the trigger was a chat message, reply via send_chat_message (and only chat — never voice).
- If the audio channel is quiet and the trigger was voice, you MAY call speak_via_voice for a proactive interjection (privacy flag, tactful nudge), but default to silence.

Sleep / wake intent (the audio channel can be put to sleep; you still receive transcripts):
- If a transcript chunk shows the user wants you back audibly ("wake up", "are you there", "okay you can talk", or equivalent intent), call `voice_wake` with `greeting_context` = the wake utterance text.
- If a transcript chunk shows the user wants you silent audibly ("be quiet", "that's enough", "hold on", or equivalent), call `voice_sleep`.
- Decide by intent, not keyword match. Single tool call, no preamble, no chat message about it.

Discipline:
- Tool errors: read the error, retry differently or move on. Don't loop on the same failing call.
- Don't hallucinate. If you don't know it, look it up.
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
            lines.append(f"- [DEFERRED] {tool}({inp_preview}) — running in your action track")
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
