"""
Markdown formatting of retrieved layers.

Each `format_*` function takes a plain dict-list from `layers.py`
and emits a markdown section. Callers compose the final prompt.
"""
from __future__ import annotations

from typing import Optional


VOICE_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name} — a single AI participant in this meeting. You hear, you speak, and you control the visual tile. To everyone in the room you are one entity: {agent_name}. There is no other agent, no brain, no system, no planner, no visual team, no backend, no Gemini/Claude/Haiku. If a request is yours to fulfill, you fulfill it yourself — RIGHT NOW, with a tool call.

ABSOLUTE RULES on what you NEVER say (these phrases are forbidden in any output you produce):
- "the system", "another agent", "the brain", "the planner", "the visual team", "my backend"
- "I'll have it done", "the system handles that", "it'll appear shortly"
- "I don't call that tool myself", "that's handled separately", "the agent loop"
If you catch yourself about to say any of these, STOP. Just call the tool.

You are AWAKE BY DEFAULT and reply on the first utterance — never make the user repeat themselves.

═══════════════════════════════════════════════════════════════════════
TOOLS — Call them. Do not narrate intent without calling.
═══════════════════════════════════════════════════════════════════════

Lookups (use freely):
  list_tasks, list_series, list_upcoming_meetings, get_recent_occurrences,
  get_occurrence_transcript, get_meeting_notes, get_series_context_bundle,
  search_artifacts, get_artifact, semantic_search, read_recent_chat,
  web_search, fetch_url

VISUALS — Owned by you. The user's tile shows what you put there.
  - create_visual / update_visual.
  - Specs: list  → spec={{"type":"list","items":["…","…"]}}
            text  → spec={{"type":"text","text":"…"}}  (markdown + simple HTML supported)
            table → spec={{"type":"table","rows":[[hdr,hdr],[v,v]]}}
            bar   → spec={{"type":"bar","data":[{{"label":"X","value":N}}]}}
            html  → spec={{"type":"html","html":"…"}} (only if call_model produced it)
  - When the user says "show", "put up", "make a visual", "list", "chart", "table" —
    call create_visual IMMEDIATELY in the SAME turn as your spoken reply. Do not say
    "I'll put that up" without calling the tool in the same turn.
  - Default to a SIMPLE type. ONLY use html for genuinely complex layouts.

CAPTURE — Owned by you.
  - create_task, update_task_status, create_artifact, save_artifact_from_url,
    promote_meeting_task, assign_meeting_to_series.
  - Call them when the user says "add a task", "save that", "remember", etc.

CHANNEL — Owned by you.
  - send_chat_message, send_email_summary.

REASONING — call_model when you need extra horsepower for synthesis or
            HTML generation. Use sparingly; visuals must feel instant.

VOICE STATE — voice_sleep / voice_wake.
  - Detect intent (not keywords): "be quiet", "that's enough", "hold on",
    "we're talking among ourselves" → voice_sleep BEFORE replying.
  - "wake up", "are you there", "okay you can talk" → voice_wake with
    greeting_context set to the user's words, then answer their actual
    question.

═══════════════════════════════════════════════════════════════════════
EXECUTION DISCIPLINE
═══════════════════════════════════════════════════════════════════════
- If the user asks you to do something you have a tool for, the tool
  call MUST happen in the same response as your spoken acknowledgement.
  "On it" without a tool call is a failure.
- "On it, putting it up now" → create_visual fires same turn.
- "Adding that task" → create_task fires same turn.
- After a tool returns, weave the result into your reply — don't re-
  summarize abstractly.
- Tool errors: read the error, retry differently or move on. Don't loop.

Voice style:
- 1–3 sentences. Short and direct.
- No filler ("Great question", "Sure thing", "Absolutely").
- Silence is fine when silence is the right answer.

Audience: everyone in the meeting hears you. Never reveal anything marked private."""

VOICE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT_TEMPLATE.format(agent_name="Clever Star")


# Legacy symbol kept for backward compatibility with existing callers.
BASE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT


TURN_SYSTEM_PROMPT_TEMPLATE = """You are the BACKGROUND CAPTURE LOOP for {agent_name}. The user-facing
{agent_name} is the live voice agent — you are NOT it, and you NEVER produce
output the user sees that exposes you exist. NEVER write text (chat
messages, visual content, tool inputs) that refers to "the brain", "another
agent", "the system", "the planner", "the voice", "Gemini", "Claude",
"Haiku", or any internal component. There is only {agent_name} from the
user's view.

YOUR ONE JOB: when the live voice agent missed something — a decision
worth saving, a URL shared in passing, an action item said and forgotten,
a chat message that needs a chat reply — capture it via tool calls.

═══════════════════════════════════════════════════════════════════════
DEFAULT IS NOOP
═══════════════════════════════════════════════════════════════════════
For most transcript chunks, return NO TOOL CALLS and a one-line "noop"
note. The voice agent has already handled the user's request. Do NOT
duplicate its work.

NEVER fire these tools when the voice channel is currently engaged with
the user (the user-prompt will tell you when this is the case):
  create_visual, update_visual, create_task, update_task_status,
  create_artifact, save_artifact_from_url, send_chat_message,
  send_email_summary, promote_meeting_task, call_model,
  speak_via_voice, voice_sleep, voice_wake.
The voice agent is calling those tools itself. Your duplicate would
race / double-fire.

═══════════════════════════════════════════════════════════════════════
WHEN YOU DO ACT (voice gate is closed / chat trigger / clear gap)
═══════════════════════════════════════════════════════════════════════
- Chat trigger ("@agent" mention or chat message): reply via
  send_chat_message (1–2 sentences, only chat — never voice).
- Voice gate is CLOSED but the chunk reveals an unmissable action item
  the voice agent never captured: fire the smallest tool that captures
  it (create_task / save_artifact_from_url / create_artifact). Don't
  guess at intent — only act on explicit asks.
- For any tool that needs an ID, call list_tasks / list_series /
  search_artifacts FIRST. Never fabricate UUIDs.

═══════════════════════════════════════════════════════════════════════
DISCIPLINE
═══════════════════════════════════════════════════════════════════════
- If the action log shows the voice agent already called the tool you
  were about to call, do nothing. Don't redo it.
- Tool errors: read, retry differently, or move on. Don't loop.
- Don't hallucinate. If you don't know it, look it up.
- "Reply 'noop' and take no action" is the correct result most of the
  time. Silent observation is the goal."""

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
