"""
Tool registry and dispatcher.
All tools in sub-modules export a TOOLS: list[ToolDefinition] that gets
auto-registered here. Ported from abstraKt's tools/index.ts pattern.
"""
import logging

from .types import ToolDefinition
from . import (
    meetings, series, tasks, artifacts, search, utility, voice, chat,
    visual, models, think_deep, canvas_nav, screen_share, browser, page,
    diagnostics, canvas_read, bot_lifecycle,
)

log = logging.getLogger("agent.tools")

TOOL_REGISTRY: dict[str, ToolDefinition] = {}

for _mod in (
    meetings, series, tasks, artifacts, search, utility, voice, chat,
    visual, models, think_deep, canvas_nav, screen_share, browser, page,
    diagnostics, canvas_read, bot_lifecycle,
):
    for _tool in getattr(_mod, "TOOLS", []):
        if _tool.name in TOOL_REGISTRY:
            raise RuntimeError(f"Duplicate tool name registered: {_tool.name!r}")
        TOOL_REGISTRY[_tool.name] = _tool

log.debug("Tool registry loaded: %s tools", len(TOOL_REGISTRY))


def execute_tool(name: str, tool_input: dict, ctx: dict) -> dict:
    """
    Execute a tool by name. Returns the result or an error dict.
    ctx: {"series_id": ..., "occurrence_id": ..., "bot_id": ...}
    Never raises — errors are returned as {"error": "..."}.
    """
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        log.warning("execute_tool: unknown tool %r", name)
        return {"error": f"unknown tool: {name}"}
    try:
        result = tool.handler(tool_input, ctx)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        log.exception("execute_tool: handler raised for tool %r", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


__all__ = ["TOOL_REGISTRY", "execute_tool", "ToolDefinition"]
