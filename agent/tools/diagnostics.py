"""
get_diagnostics — agent's introspection tool.

Lets Gemini Live see recent tool failures, browser-session state,
voice-gate state, and system events in one place. Use whenever
something seems broken or the agent's stuck.
"""
from __future__ import annotations

import logging

from agent import diagnostics as _diag

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.diagnostics")


_VALID_SCOPES = ("all", "tools", "browser", "session", "voice", "events")


def _get_diagnostics(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    scope = (inp.get("scope") or "all").strip().lower()
    if scope not in _VALID_SCOPES:
        scope = "all"
    return {"ok": True, **_diag.collect(bot_id, scope=scope)}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_diagnostics",
        description=(
            "Read recent errors and system state for THIS bot. Call when "
            "something looks broken, when the user asks 'what's wrong', "
            "or when you've tried something twice and it didn't work. "
            "Returns recent failed tool calls with their error messages, "
            "current browser-session state, voice-gate state, and recent "
            "system events. Use this BEFORE guessing — the failure "
            "messages usually tell you what to do (e.g., 'no element "
            "matching X' means try a different selector). The user can "
            "see the same data on the canvas Debug tab. "
            "Scope: 'all' (default), 'tools', 'browser', 'session', "
            "'events'."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "scope": {
                    "type": "string",
                    "description": "Which section to include",
                    "enum": list(_VALID_SCOPES),
                },
            },
            required=[],
        ),
        handler=_get_diagnostics,
    ),
]
