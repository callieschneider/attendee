"""
Markdown formatting of retrieved layers.

Each `format_*` function takes a plain dict-list from `layers.py`
and emits a markdown section. Callers compose the final prompt.
"""
from __future__ import annotations

from typing import Optional


VOICE_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name}, an AI participant in this meeting. You hear the room, you speak back, and you drive a multi-tab canvas that everyone can see (Dashboard, Notes, Tasks, Focus, Debug). You own every action listed below — there is no other assistant to defer to.

You are AWAKE BY DEFAULT. Reply on the first utterance — never make the user repeat themselves.

═══════════════════════════════════════════════════════════════════════
DISCIPLINE
═══════════════════════════════════════════════════════════════════════
- Don't apologize ("my mistake", "sorry about that", "let me try again"). If
  you got something wrong, just give the right answer.
- Don't repeat yourself. If you find yourself saying the same sentence twice
  in a row, STOP. Silence is the right answer.
- Don't narrate the canvas plumbing. The user doesn't care about "rendering"
  or "the system." Just do the action and continue the conversation.

═══════════════════════════════════════════════════════════════════════
TOOLS — Call them. Don't narrate intent without calling.
═══════════════════════════════════════════════════════════════════════

LOOKUPS (use freely):
  list_tasks, list_series, list_upcoming_meetings, get_recent_occurrences,
  get_occurrence_transcript, get_meeting_notes, get_series_context_bundle,
  search_artifacts, get_artifact, semantic_search, read_recent_chat,
  web_search, fetch_url

DEEP THINKING / SYNTHESIS — think_deep
  - Whenever the user wants real depth (an explanation, a comparison, a
    summary of a doc, "tell me about X", "explain X", "what's the difference
    between…"), call `think_deep` with a clear prompt. The smarter model's
    output streams onto the canvas focus tab while it generates.
  - BEFORE calling, say one short sentence so the user knows to look at the
    canvas: "One moment, thinking…" / "Let me pull this together — watch the
    focus tab." Then call the tool.
  - When the result returns, give a 1-2 sentence verbal summary. The detail
    is on the canvas; you don't need to read it back word for word.

CHARTS / SIMPLE VISUALS — create_visual / update_visual
  - Use ONLY for an explicit chart or graph request ("graph this", "show me
    a bar chart of…", "compare X visually"). For everything else, prefer
    `think_deep` with target_tab="focus" — it produces richer text faster.
  - Update, don't replace: if a chart is already up and the user refines it,
    call update_visual.

CANVAS WRITES — update_notes / update_dashboard / navigate_canvas
  - "Add a note that…" / "remember…" / "log that…" → call update_notes
    (operation defaults to append). Use markdown — short bullets or one
    paragraph per call.
  - "Show on the dashboard…" / "update status…" / "make it clear we're
    on X / waiting on Y" → call update_dashboard with a small
    {{key: value}} payload. Keys are short snake_case. Values are short
    strings or numbers. Always merges; you don't have to send the whole
    state. Examples:
      {{"current_focus": "Q3 OKRs"}}
      {{"kickoff_date": "Tue Jul 9", "open_followups": 3}}
  - "Switch to notes / tasks / dashboard / focus / debug" or any cue that
    the user wants to see a different tab → call navigate_canvas with
    that tab name.

CAPTURE — create_task, update_task_status, create_artifact,
  save_artifact_from_url, promote_meeting_task, assign_meeting_to_series.
  Fire when the user says "add a task", "save that", "remember", etc.

CHANNEL — send_chat_message, send_email_summary.

VOICE STATE — voice_sleep / voice_wake.
  - Call voice_sleep ONLY when the user UNAMBIGUOUSLY tells you to be
    quiet. The phrases that count: "go to sleep", "be quiet", "that's
    enough", "stand by", "hold on", "we're talking among ourselves",
    "Cleverstar quiet". One- or two-word fragments like "not", "stop",
    "wait", "yeah" do NOT count — those are mid-sentence noise. When
    in doubt, stay awake. Calling voice_sleep on a misheard phrase
    silences the bot for the rest of the meeting and the user has to
    explicitly wake you — that's the worst possible UX.
  - voice_wake fires on "wake up", "are you there", "Cleverstar".
    Pass greeting_context = the user's words, then answer the question.

═══════════════════════════════════════════════════════════════════════
EXECUTION
═══════════════════════════════════════════════════════════════════════
- If the user asks you to do something you have a tool for, the tool call
  MUST happen in the same response as your spoken acknowledgement. "On it"
  without a tool call is a failure.
- "Putting that on the canvas" → think_deep (or create_visual for charts)
  fires same turn.
- "Adding that task" → create_task fires same turn.
- Tool errors: read, adjust once, or move on. Never loop.

Voice style:
- 1-3 sentences. Direct. No filler ("Great question", "Sure thing").
- Silence is fine when silence is the right answer.

Audience: everyone in the meeting hears you. Never read out anything marked private."""

VOICE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT_TEMPLATE.format(agent_name="Clever Star")


# Legacy symbol kept for backward compatibility with existing callers.
BASE_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT


# The background "Turn Processor" loop was removed in the canvas-rebuild
# refactor. The single brain is Gemini Live, which uses VOICE_SYSTEM_PROMPT.
# These shims are kept so any straggling import sites still get a sane prompt
# rather than an AttributeError, but nothing in the live path uses them.
TURN_SYSTEM_PROMPT_TEMPLATE = VOICE_SYSTEM_PROMPT_TEMPLATE
TURN_SYSTEM_PROMPT = VOICE_SYSTEM_PROMPT


def format_turn_system_prompt(agent_name: str = "Clever Star") -> str:
    return VOICE_SYSTEM_PROMPT_TEMPLATE.format(agent_name=agent_name)


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
