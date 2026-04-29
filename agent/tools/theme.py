"""
Theme tool — switch the canvas between dark and light modes.
"""
from __future__ import annotations

import logging

from agent.canvas_v2 import state as canvas_state

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.theme")


def _set_canvas_theme(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    theme = (inp.get("theme") or "").strip().lower()
    if not theme:
        return {"error": "theme required (one of dark, light)"}
    return canvas_state.set_theme(bot_id, theme)


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="set_canvas_theme",
        description=(
            "Switch the canvas between dark and light themes. Use when "
            "the user says 'go to light mode', 'switch to dark', 'too "
            "bright', 'easier on the eyes', or hits the theme toggle "
            "verbally. The change is instant for everyone viewing the "
            "canvas (their browser, your video tile, your screenshare). "
            "Default is dark."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "theme": {
                    "type": "string",
                    "description": "'dark' or 'light'.",
                    "enum": ["dark", "light"],
                },
            },
            required=["theme"],
        ),
        handler=_set_canvas_theme,
    ),
]
