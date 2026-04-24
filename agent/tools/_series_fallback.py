"""
Series fallback helper — resolves a series_id for a tool call, falling back
to a singleton "Inbox" series when the bot isn't tied to a calendar event.

Used by write-mutating tools (create_artifact, create_visual, create_task, …)
so the LLM doesn't have to make a series decision just to save something.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("agent.tools._series_fallback")

INBOX_SERIES_TITLE = "Inbox"


def ensure_series_id(inp: dict, ctx: dict) -> str:
    """
    Resolve a series_id for write tools.

    Lookup order:
      1. explicit `series_id` in the tool input
      2. `series_id` in the execution context (set by the Turn Processor
         when the bot is linked to a MeetingSeries via metadata / occurrence)
      3. the singleton "Inbox" MeetingSeries (get-or-create)
    """
    from agent.models import MeetingSeries

    series_id = inp.get("series_id") or ctx.get("series_id")
    if series_id:
        # Validate it exists to surface a clear error vs a Django FK explosion
        if MeetingSeries.objects.filter(id=series_id).exists():
            return str(series_id)
        log.warning("ensure_series_id: series_id %s not found; falling back to Inbox", series_id)

    inbox, created = MeetingSeries.objects.get_or_create(
        title=INBOX_SERIES_TITLE,
        defaults={
            "description": (
                "Catch-all series for artifacts/tasks created during ad-hoc "
                "meetings that weren't linked to a specific calendar event."
            ),
            "is_active": True,
        },
    )
    if created:
        log.info("ensure_series_id: created Inbox series %s", inbox.id)
    return str(inbox.id)
