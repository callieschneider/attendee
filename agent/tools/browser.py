"""
Canvas browser tools — let Gemini Live put a URL on the canvas.

The canvas web app renders the URL in an iframe inside a "Browser" tab.
The bot's video tile (canvas screenshot) and any active screenshare
both pick it up automatically.

Phase 1 (this file): display only. The agent can change the URL but
can't click/scroll/type inside the page. Sites that send
X-Frame-Options: DENY won't load — the canvas shows a "this site
can't be embedded" fallback with an open-in-new-tab link.

Phase 2 (TBD): full automation via a headless Chrome controlled by
the bridge service. That requires a per-bot browser process and a
screencast pipe; surfaced separately.
"""
from __future__ import annotations

import logging

from agent.canvas_v2 import state as canvas_state

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.browser")


def _open_url(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    url = (inp.get("url") or "").strip()
    if not url:
        return {"error": "url required"}
    title = (inp.get("title") or "").strip()
    return canvas_state.open_url(bot_id, url, title=title)


def _close_url(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    return canvas_state.close_url(bot_id)


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="open_url",
        description=(
            "Open a webpage on the canvas Browser tab. The page loads in an "
            "iframe so everyone in the meeting sees it via your video tile "
            "(and via screen-share if active). Use when the user says 'pull "
            "up X', 'show me the docs for X', 'open this article', etc. "
            "Many high-security sites (Google login pages, banking, etc.) "
            "block iframe embedding — the canvas will show a 'can't embed' "
            "fallback with an open-in-new-tab link in that case. For full "
            "interactive automation (clicking, scrolling, typing), tell the "
            "user that's not yet supported and offer to summarize the page "
            "via think_deep instead."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "url": {
                    "type": "string",
                    "description": "Absolute URL to load (https:// is added if missing).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional short label shown above the iframe.",
                },
            },
            required=["url"],
        ),
        handler=_open_url,
    ),
    ToolDefinition(
        name="close_url",
        description=(
            "Clear the canvas Browser tab. Use when the user says 'close "
            "that', 'we're done with the page', or you want to switch the "
            "browser tab back to its empty state."
        ),
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_close_url,
    ),
]
