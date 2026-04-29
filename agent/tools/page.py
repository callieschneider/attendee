"""
Page tools — Phase 2 of the browser-automation plan.

Lets Gemini Live drive a per-bot headless Chrome owned by the bridge's
LiveSessionManager (via agent.browser_session). Each handler is sync
(the tool dispatcher runs them on a thread pool) and dispatches the
async BrowserSession methods back onto the bridge's asyncio loop via
`run_coro_sync`.

These tools can ONLY work inside the bridge process — the web server
doesn't have the bridge loop registered. If a page_* tool is somehow
dispatched from a non-bridge context (eg. the worker), it returns an
error rather than blowing up.
"""
from __future__ import annotations

import logging

from agent import browser_session as _bs
from agent.canvas_v2 import state as canvas_state

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.page")


def _bot_id(inp: dict, ctx: dict) -> str | None:
    return ctx.get("bot_id") or inp.get("bot_id") or None


def _wrap(coro_factory, bot_id: str | None) -> dict:
    """Run a coroutine on the bridge loop and return its dict result."""
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}
    if _bs._BRIDGE_LOOP is None:
        return {"error": "page tools only available in the bridge process"}
    try:
        return _bs.run_coro_sync(coro_factory())
    except _bs.BrowserSessionError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.exception("page tool: unexpected failure bot=%s", bot_id)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _page_navigate(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    url = (inp.get("url") or "").strip()
    if not url:
        return {"error": "url required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    bs = _bs.get_or_create(bot_id) if bot_id else None
    if bs is None:
        return {"error": "bot_id required"}
    out = _wrap(lambda: bs.navigate(url), bot_id)
    # Mirror onto CanvasState so the snapshot shows current URL/title even
    # for late-joining canvas clients before the next screencast frame.
    if out.get("ok"):
        try:
            canvas_state.open_url(bot_id, out.get("url") or url, title=out.get("title") or "")
        except Exception:
            log.exception("page_navigate: canvas_state mirror failed bot=%s", bot_id)
        # Log the visit so it shows up in browser_history / history tab.
        # Series-scoped — same URL across multiple meetings recorded once
        # per visit, ordered by timestamp.
        try:
            from agent.tools.browser import _log_browser_visit
            _log_browser_visit(bot_id, out.get("url") or url, out.get("title") or "", source="page_navigate")
        except Exception:
            log.exception("page_navigate: history logging failed bot=%s", bot_id)
    return out


def _page_click(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    text = (inp.get("text") or "").strip()
    selector = (inp.get("selector") or "").strip()
    if not text and not selector:
        return {"error": "either 'text' or 'selector' required"}
    case_sensitive = bool(inp.get("case_sensitive"))
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session — call page_navigate first"}
    if selector:
        return _wrap(lambda: bs.click_selector(selector), bot_id)
    return _wrap(lambda: bs.click_text(text, case_sensitive=case_sensitive), bot_id)


def _page_type(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    selector = (inp.get("selector") or "").strip()
    text = inp.get("text") or ""
    if not selector:
        return {"error": "selector required"}
    submit = bool(inp.get("submit"))
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session — call page_navigate first"}
    return _wrap(lambda: bs.type_in(selector, text, submit=submit), bot_id)


def _page_scroll(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    direction = (inp.get("direction") or "down").strip().lower()
    pixels = int(inp.get("pixels") or 800)
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session — call page_navigate first"}
    return _wrap(lambda: bs.scroll(direction, pixels=pixels), bot_id)


def _page_press(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    key = (inp.get("key") or "").strip()
    if not key:
        return {"error": "key required"}
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session — call page_navigate first"}
    return _wrap(lambda: bs.press(key), bot_id)


def _page_get_text(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    selector = (inp.get("selector") or "").strip() or None
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session — call page_navigate first"}
    return _wrap(lambda: bs.get_text(selector), bot_id)


def _page_back(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session"}
    return _wrap(lambda: bs.back(), bot_id)


def _page_reload(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"error": "no active browser session"}
    return _wrap(lambda: bs.reload(), bot_id)


def _page_status(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    bs = _bs.get(bot_id) if bot_id else None
    if bs is None:
        return {"ok": True, "active": False}
    out = _wrap(lambda: bs.status(), bot_id)
    out["active"] = True
    return out


def _page_close(inp: dict, ctx: dict) -> dict:
    bot_id = _bot_id(inp, ctx)
    if not bot_id:
        return {"error": "bot_id required"}
    if _bs._BRIDGE_LOOP is None:
        return {"error": "page tools only available in the bridge process"}
    try:
        _bs.run_coro_sync(_bs.close_for_bot(bot_id))
    except _bs.BrowserSessionError as exc:
        return {"error": str(exc)}
    # Also clear the canvas browser_url so the iframe path resets.
    try:
        canvas_state.close_url(bot_id)
    except Exception:
        log.exception("page_close: canvas_state.close_url failed bot=%s", bot_id)
    return {"ok": True}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="page_navigate",
        description=(
            "Drive the agent's browser to a URL. Spawns a headless Chrome "
            "the first time you call it. The page is rendered live onto "
            "the canvas Browser tab as a screencast — everyone sees what "
            "you see. Use whenever the user wants you to actually visit, "
            "search, fill out, or interact with a webpage. For pure "
            "display ('show this article on screen so we can read it') "
            "without any interaction, use open_url instead — it's faster "
            "for the user's own browser."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "url": {"type": "string", "description": "Absolute URL (https:// added if missing)"},
            },
            required=["url"],
        ),
        handler=_page_navigate,
    ),
    ToolDefinition(
        name="page_click",
        description=(
            "Click an element on the current page. Prefer matching by "
            "visible text ('Submit', 'Sign in') — pass the text in the "
            "`text` field. For ambiguous cases pass an explicit CSS "
            "selector via `selector`. Text matching is case-insensitive "
            "by default; pass case_sensitive=true to make it strict."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "text": {"type": "string", "description": "Visible text on the element to click"},
                "selector": {"type": "string", "description": "Explicit CSS selector (overrides text)"},
                "case_sensitive": {"type": "boolean", "description": "Default false"},
            },
            required=[],
        ),
        handler=_page_click,
    ),
    ToolDefinition(
        name="page_type",
        description=(
            "Type into an input/textarea on the current page. Pass a CSS "
            "selector for the field (e.g. 'input[name=q]', "
            "'textarea[aria-label=\"Search\"]'). Optionally set "
            "submit=true to press Enter after typing — handy for search "
            "boxes."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "selector": {"type": "string", "description": "CSS selector for the input"},
                "text": {"type": "string", "description": "Value to type"},
                "submit": {"type": "boolean", "description": "Press Enter after typing"},
            },
            required=["selector", "text"],
        ),
        handler=_page_type,
    ),
    ToolDefinition(
        name="page_scroll",
        description=(
            "Scroll the page. Direction is one of 'down', 'up', 'top', "
            "'bottom'. For 'up'/'down' the optional `pixels` controls how "
            "far (default 800)."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "direction": {
                    "type": "string",
                    "enum": ["down", "up", "top", "bottom"],
                },
                "pixels": {"type": "integer", "description": "Default 800"},
            },
            required=["direction"],
        ),
        handler=_page_scroll,
    ),
    ToolDefinition(
        name="page_press",
        description=(
            "Send a keyboard press to the focused element. Supports "
            "Enter, Tab, Escape, Space, Backspace, ArrowUp, ArrowDown, "
            "ArrowLeft, ArrowRight."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "key": {
                    "type": "string",
                    "enum": ["Enter", "Tab", "Escape", "Space", "Backspace",
                             "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"],
                },
            },
            required=["key"],
        ),
        handler=_page_press,
    ),
    ToolDefinition(
        name="page_get_text",
        description=(
            "Extract text from the current page. With no args, returns "
            "the whole-document innerText (truncated to 8KB). Pass "
            "`selector` to scope it to a specific element."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "selector": {"type": "string", "description": "Optional CSS selector"},
            },
            required=[],
        ),
        handler=_page_get_text,
    ),
    ToolDefinition(
        name="page_back",
        description="Navigate one step back in the browser history.",
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_page_back,
    ),
    ToolDefinition(
        name="page_reload",
        description="Reload the current page.",
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_page_reload,
    ),
    ToolDefinition(
        name="page_status",
        description=(
            "Report whether a browser session is open and, if so, its "
            "current URL and title. Useful before deciding whether to "
            "page_navigate or page_back."
        ),
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_page_status,
    ),
    ToolDefinition(
        name="page_close",
        description=(
            "Close the agent's browser session. The screencast disappears "
            "from the canvas Browser tab. Use when the user says 'we're "
            "done with the browser' or you want to free resources."
        ),
        input_schema=ToolSchema(type="object", properties={}, required=[]),
        handler=_page_close,
    ),
]
