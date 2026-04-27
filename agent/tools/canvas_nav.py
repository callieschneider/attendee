"""
Canvas navigation tools (Phase 2 of the canvas-rebuild plan).

These let Gemini Live drive the multi-tab canvas web app:
  - `navigate_canvas`  — switch which tab is shown
  - `update_notes`     — append or replace the agent's running notes
  - `update_dashboard` — populate cards on the dashboard tab

All three publish a Redis pubsub event so connected canvas clients see
the change live, and persist into `agent.models.CanvasState` so a
late-joining client can backfill from `state.json`.
"""
from __future__ import annotations

import logging

from agent.canvas_v2 import state as canvas_state

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.canvas_nav")


def _navigate_canvas(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    tab = (inp.get("tab") or "").strip()
    return canvas_state.navigate(bot_id, tab, source="agent")


def _update_notes(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    text = inp.get("text") or ""
    operation = (inp.get("operation") or "append").strip().lower()
    return canvas_state.update_notes(bot_id, text=text, operation=operation)


def _update_dashboard(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    payload = inp.get("payload") or {}
    if not isinstance(payload, dict):
        return {"error": "payload must be an object"}
    return canvas_state.update_dashboard(bot_id, payload)


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="navigate_canvas",
        description=(
            "Switch which tab the canvas web app is showing (everyone in the "
            "meeting sees this — your video tile renders this same page). Use "
            "when the user says things like 'go to tasks', 'show me notes', "
            "'back to dashboard', or whenever shifting focus would help. Tabs: "
            "dashboard, notes, tasks, focus, debug. If the user is currently "
            "driving the canvas in their own browser, your navigation is "
            "ignored — that's fine, they're in control."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "tab": {
                    "type": "string",
                    "description": "Which tab to switch to.",
                    "enum": list(canvas_state.VALID_TABS),
                },
            },
            required=["tab"],
        ),
        handler=_navigate_canvas,
    ),
    ToolDefinition(
        name="update_notes",
        description=(
            "Append or replace the running meeting-notes markdown that's "
            "shown on the Notes tab. Use this whenever a decision is made, a "
            "noteworthy fact comes up, or you summarize a sub-discussion. "
            "Default operation is 'append' (adds a new paragraph at the "
            "bottom). Use markdown — headings, bullets, bold all render."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "text": {
                    "type": "string",
                    "description": "Markdown content to add to the notes.",
                },
                "operation": {
                    "type": "string",
                    "description": (
                        "'append' (default) adds to the existing notes. "
                        "'replace' overwrites all of them — use rarely."
                    ),
                    "enum": ["append", "replace"],
                },
            },
            required=["text"],
        ),
        handler=_update_notes,
    ),
    ToolDefinition(
        name="update_dashboard",
        description=(
            "Patch the dashboard payload — small key/value chunks the "
            "Dashboard tab renders as cards (e.g. {'current_focus': 'Q3 "
            "OKRs', 'attendees': 5}). Always merge-patches, never replaces "
            "the whole object. Use sparingly; the dashboard is mostly "
            "auto-populated."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "payload": {
                    "type": "object",
                    "description": "Object of small key/value pairs to merge into the dashboard.",
                },
            },
            required=["payload"],
        ),
        handler=_update_dashboard,
    ),
]
