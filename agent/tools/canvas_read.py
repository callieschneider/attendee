"""
Canvas-content awareness for the agent.

Lets Gemini Live see what's actually on every canvas tab right now —
not just what flowed past in the transcript. Two tools:

- `get_canvas_content`: full text-state of every tab. Use whenever the
  user asks "what's on the canvas", "what did you put on the dashboard",
  "show me the notes", or whenever you want to verify your prior write
  actually landed.

- `get_browser_screenshot`: base64 PNG of the latest screencast frame
  from the agent's headless Chrome. Gemini Live accepts inline images,
  so the agent can SEE the page when textual page_get_text isn't
  enough (image-heavy sites, layout questions, "what does the button
  look like").
"""
from __future__ import annotations

import json
import logging

from agent.canvas_v2 import state as canvas_state

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.canvas_read")


_TEXT_BUDGET = 6000


def _get_canvas_content(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    snap = canvas_state.snapshot(bot_id)

    # Compact projection — we don't want to flood Gemini's context with
    # the full transcript; that's already in the conversation. Focus on
    # what's persistent and visible on the canvas.
    notes = (snap.get("notes_md") or "")[-_TEXT_BUDGET:]
    focus = snap.get("focus") or {}
    dashboard = snap.get("dashboard") or {}
    browser = snap.get("browser") or {}
    tasks = snap.get("tasks") or []
    meeting_tasks = snap.get("meeting_tasks") or []
    voice = snap.get("voice") or {}

    return {
        "ok": True,
        "active_tab": snap.get("active_tab"),
        "user_driving": snap.get("user_driving"),
        "voice_state": voice,
        "tabs": {
            "notes": {
                "length": len(snap.get("notes_md") or ""),
                "tail": notes,
            },
            "dashboard": dashboard,
            "focus": {
                "session_id": focus.get("session_id"),
                "done": focus.get("done"),
                "text": (focus.get("text") or "")[-_TEXT_BUDGET:],
                "length": len(focus.get("text") or ""),
            },
            "browser": {
                "url": browser.get("url") or "",
                "title": browser.get("title") or "",
                "screencast_active": bool(_browser_session_active(bot_id)),
            },
            "tasks": [
                {
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "priority": t.get("priority"),
                    "owner": t.get("owner"),
                }
                for t in tasks[:25]
            ],
            "meeting_tasks": [
                {
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "assignee": t.get("assignee"),
                }
                for t in meeting_tasks[:20]
            ],
        },
    }


def _browser_session_active(bot_id: str) -> bool:
    try:
        from agent import browser_session as _bs
        bs = _bs.get(bot_id)
        return bool(bs and not bs._closed and bs._driver is not None)
    except Exception:
        return False


def _get_browser_screenshot(inp: dict, ctx: dict) -> dict:
    """Return the latest screencast frame as base64 PNG."""
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required"}
    try:
        from agent import browser_session as _bs
    except Exception:
        return {"error": "browser_session module unavailable"}
    bs = _bs.get(bot_id)
    if bs is None or bs._closed:
        return {"error": "no active browser session — call page_navigate first"}
    if _bs._BRIDGE_LOOP is None:
        return {"error": "page tools only available in the bridge process"}
    try:
        png = _bs.run_coro_sync(bs._capture_png(), timeout=10.0)
    except _bs.BrowserSessionError as exc:
        return {"error": str(exc)}
    if not png:
        return {"error": "screenshot returned empty"}
    import base64
    return {
        "ok": True,
        "mime": "image/png",
        "data_b64": base64.b64encode(png).decode(),
        "viewport": [_bs.VIEWPORT_W, _bs.VIEWPORT_H],
        "url_at_capture": (bs._driver.current_url if bs._driver else ""),
    }


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_canvas_content",
        description=(
            "Read the current text-state of every canvas tab — what the "
            "user is actually seeing right now. Returns notes (markdown "
            "tail), dashboard payload, focus tab text, browser URL/title, "
            "open tasks, and voice state. Call when the user asks 'what's "
            "on the canvas?', 'what did you put on the dashboard?', 'show "
            "me the notes', or whenever you want to verify a prior write "
            "actually persisted. Cheap, text-only — call freely."
        ),
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_get_canvas_content,
    ),
    ToolDefinition(
        name="get_browser_screenshot",
        description=(
            "Capture the current visible viewport of the agent's "
            "headless Chrome (the page driven by page_navigate / "
            "page_click / etc.). Returns a base64 PNG. Use when "
            "page_get_text isn't enough — image-heavy pages, layout "
            "questions, 'what does the page look like'. Requires a "
            "browser session (call page_navigate first)."
        ),
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_get_browser_screenshot,
    ),
]
