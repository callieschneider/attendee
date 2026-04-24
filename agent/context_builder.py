"""
Context builder — assembles the system prompt injected into every Gemini Live session.
4-layer retrieval (simplified from abstraKt's 16-layer context-builder.ts):
  1. Pinned ContextItems for the series
  2. Recent MeetingOccurrence summaries (last 5)
  3. Open Tasks (up to 10)
  4. Current occurrence transcript (if in-meeting)
"""
import logging
from typing import Optional

log = logging.getLogger("agent.context_builder")

BASE_SYSTEM_PROMPT = """You are the Meeting Agent — an AI assistant that sits in on meetings and helps participants by:
- Answering questions using project context and past meeting history
- Noting action items and decisions
- Providing relevant information on demand
- Staying concise and conversational during live meetings

You only speak when directly addressed. Keep responses brief during meetings. Use tools to look up specific information rather than guessing."""


def build_context(
    series_id: Optional[str] = None,
    occurrence_id: Optional[str] = None,
    extra_context: str = "",
) -> str:
    """
    Build a system prompt with series-scoped context.
    Returns a markdown string ready for injection as Gemini Live systemInstruction.
    """
    from .models import MeetingSeries, MeetingOccurrence, Task, ContextItem

    lines = [BASE_SYSTEM_PROMPT, ""]

    if series_id:
        try:
            series = MeetingSeries.objects.get(id=series_id)
            lines.append(f"## Meeting Series: {series.title}")
            if series.description:
                lines.append(series.description)
            lines.append("")
        except MeetingSeries.DoesNotExist:
            log.warning("build_context: series_id %s not found", series_id)

        # Layer 1: Pinned context items
        pins = ContextItem.objects.filter(series_id=series_id, is_pinned=True).order_by("order")[:10]
        if pins.exists():
            lines.append("## Pinned Context\n")
            for p in pins:
                label = f"**{p.label}**: " if p.label else ""
                lines.append(f"- {label}{p.content}")
            lines.append("")

        # Layer 2: Recent meeting summaries
        recent = (
            MeetingOccurrence.objects.filter(series_id=series_id)
            .exclude(summary="")
            .order_by("-started_at")[:5]
        )
        if recent.exists():
            lines.append("## Recent Meetings\n")
            for occ in recent:
                date_str = occ.started_at.strftime("%Y-%m-%d") if occ.started_at else "unknown date"
                title = occ.title or f"Meeting {date_str}"
                lines.append(f"### {title} ({date_str})\n{occ.summary[:400]}\n")

        # Layer 3: Open tasks
        open_tasks = (
            Task.objects.filter(series_id=series_id)
            .exclude(status__in=["done", "cancelled"])
            .order_by("priority", "due_date")[:10]
        )
        if open_tasks.exists():
            lines.append("## Open Tasks\n")
            for t in open_tasks:
                due = f" (due {t.due_date})" if t.due_date else ""
                owner = f" — {t.owner}" if t.owner else ""
                lines.append(f"- [{t.priority}] **{t.title}**{due}{owner}")
            lines.append("")

    # Layer 4: Current meeting occurrence
    if occurrence_id:
        try:
            occ = MeetingOccurrence.objects.get(id=occurrence_id)
            if occ.summary:
                lines.append(f"## This Meeting\n{occ.summary}\n")
        except MeetingOccurrence.DoesNotExist:
            pass

    if extra_context:
        lines.append(f"\n## Additional Context\n{extra_context}\n")

    return "\n".join(lines).strip()
