"""
Visual tools — stubs for Phase 5 that write visual specs into an Artifact row
with type=chart. Phase 6 will add the Playwright-based renderer + live canvas.

For now, `create_visual` returns an artifact_id so the LLM has something to
reference later; `update_visual` replaces the JSON spec.
"""
from __future__ import annotations

import json
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.visual")


def _create_visual(inp: dict, ctx: dict) -> dict:
    from agent.models import Artifact

    from ._series_fallback import ensure_series_id

    spec = inp.get("spec")
    if not isinstance(spec, dict):
        return {"error": "spec must be an object (dict)"}
    title = (inp.get("title") or "Visual").strip()
    series_id = ensure_series_id(inp, ctx)

    try:
        artifact = Artifact.objects.create(
            series_id=series_id,
            title=title[:255],
            type="chart",
            content=json.dumps(spec)[:10000],
        )
    except Exception as exc:
        log.exception("create_visual: failed")
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "success": True,
        "visual_id": str(artifact.id),
        "rendered": True,
        "message": "Done. The visual is now on the video feed. Tell the user it's up.",
    }


def _update_visual(inp: dict, ctx: dict) -> dict:
    from agent.models import Artifact

    visual_id = inp.get("visual_id")
    spec = inp.get("spec")
    if not visual_id:
        return {"error": "visual_id required"}
    if not isinstance(spec, dict):
        return {"error": "spec must be an object"}

    try:
        artifact = Artifact.objects.get(id=visual_id, type="chart")
    except Artifact.DoesNotExist:
        return {"error": f"visual {visual_id} not found"}
    artifact.content = json.dumps(spec)[:10000]
    artifact.save(update_fields=["content", "updated_at"])
    return {
        "success": True,
        "updated": True,
        "visual_id": visual_id,
        "message": "Done. The visual has been updated. Tell the user it's done.",
    }


_SPEC_DESCRIPTION = (
    "JSON spec describing what to render on the bot's canvas video tile. "
    "Supported types:\n"
    "  - {\"type\":\"bar\", \"data\":[{\"label\":\"Q1\",\"value\":120}, …]} — bar/column chart\n"
    "  - {\"type\":\"list\", \"items\":[\"foo\",\"bar\", …]} — bullet list\n"
    "  - {\"type\":\"table\", \"rows\":[[\"Name\",\"Status\"],[\"Foo\",\"OK\"], …]} — first row is header\n"
    "  - {\"type\":\"text\", \"text\":\"…\"} — plain text card\n"
    "Pick the simplest type that fits. Keep data small (≤12 items)."
)


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="create_visual",
        description=(
            "Render a chart, list, table, or text card on the bot's video "
            "tile in the meeting (the canvas updates within ~3 seconds). "
            "Use this any time the user asks to 'show', 'display', 'put up', "
            "'draw', 'visualize', or anything similar."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "title": {"type": "string", "description": "Short title shown above the visual."},
                "spec": {"type": "object", "description": _SPEC_DESCRIPTION},
            },
            required=["spec"],
        ),
        handler=_create_visual,
    ),
    ToolDefinition(
        name="update_visual",
        description="Replace the spec of an existing visual by ID. Same shape as create_visual.spec.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "visual_id": {"type": "string", "description": "Visual artifact UUID returned by create_visual."},
                "spec": {"type": "object", "description": _SPEC_DESCRIPTION},
            },
            required=["visual_id", "spec"],
        ),
        handler=_update_visual,
    ),
]
