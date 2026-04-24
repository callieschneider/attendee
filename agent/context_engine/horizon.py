"""
Horizon summarization — compress old ActionLogEntry history into a
single synthetic summary entry when the log grows too long.

Gemini Flash-backed summarization; safe to skip if the call fails
(the uncompressed log is still usable).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from django.conf import settings

log = logging.getLogger("agent.context_engine.horizon")


ARCHIVE_THRESHOLD = 40
ARCHIVE_KEEP_RECENT = 15
ARCHIVE_BATCH = ARCHIVE_THRESHOLD - ARCHIVE_KEEP_RECENT  # 25


def maybe_compress_action_log(bot_id: str) -> Optional[str]:
    """
    If the action log for a bot exceeds ARCHIVE_THRESHOLD, summarize the
    oldest ARCHIVE_BATCH non-archived entries into a synthetic
    `_horizon_summary` entry and mark the originals archived.

    Returns the turn_id UUID string of the synthetic entry if it ran;
    None otherwise (including on failure).
    """
    from agent.models import ActionLogEntry

    qs = ActionLogEntry.objects.filter(bot_id=bot_id, is_archived=False).order_by("created_at")
    total = qs.count()
    if total <= ARCHIVE_THRESHOLD:
        return None

    to_archive = list(qs[:ARCHIVE_BATCH])
    if not to_archive:
        return None

    summary_text = _summarize_entries(to_archive)
    if not summary_text:
        log.info("maybe_compress_action_log: summarization returned empty, keeping raw log")
        return None

    turn_id = uuid.uuid4()
    # Archive originals and write synthetic summary in one pass
    ActionLogEntry.objects.filter(id__in=[e.id for e in to_archive]).update(is_archived=True)
    synthetic = ActionLogEntry.objects.create(
        bot_id=bot_id,
        turn_id=turn_id,
        tool_name="_horizon_summary",
        tool_input={"archived_count": len(to_archive)},
        tool_result={"summary": summary_text},
        status="ok",
    )
    log.info(
        "horizon: archived %d entries into synthetic summary %s bot=%s",
        len(to_archive),
        synthetic.id,
        bot_id,
    )
    return str(turn_id)


def _summarize_entries(entries: list) -> str:
    """
    Call Gemini Flash to produce a 2-4 sentence summary of the action log
    entries. Returns empty string on any failure.
    """
    if not entries:
        return ""

    lines = []
    for e in entries:
        inp = e.tool_input or {}
        inp_str = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:3])
        status = "ok" if e.status == "ok" else e.status
        lines.append(f"- {e.tool_name}({inp_str}) — {status}")
    log_text = "\n".join(lines)

    prompt = (
        "Summarize the following agent actions from earlier in a meeting. "
        "Produce 2-4 sentences capturing what was accomplished (tasks created, "
        "artifacts saved, decisions recorded, emails sent). Be specific but brief. "
        "Omit individual tool names; focus on outcomes.\n\n"
        f"Actions:\n{log_text}"
    )

    try:
        import google.generativeai as genai

        api_key = getattr(settings, "GOOGLE_API_KEY", "")
        if not api_key:
            log.warning("horizon: GOOGLE_API_KEY not configured")
            return ""
        genai.configure(api_key=api_key)
        model_name = getattr(settings, "AGENT_SUMMARIZER_MODEL", "gemini-2.5-flash")
        model = genai.GenerativeModel(model_name)
        resp = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": 256, "temperature": 0.2},
        )
        return (resp.text or "").strip()
    except Exception:
        log.exception("horizon: summarization call failed")
        return ""
