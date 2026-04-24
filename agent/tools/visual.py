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

    series_id = ctx.get("series_id")
    if not series_id:
        return {"error": "series_id required (pass via context or create_artifact-equivalent)"}

    spec = inp.get("spec")
    if not isinstance(spec, dict):
        return {"error": "spec must be an object (dict)"}
    title = (inp.get("title") or "Visual").strip()

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

    return {"visual_id": str(artifact.id), "url": "", "status": "pending_render"}


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
    return {"updated": True, "visual_id": visual_id}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="create_visual",
        description=(
            "Create a visual (chart, slide, diagram) by spec. Renders in Phase 6; "
            "for now this stores the spec so it can be referenced later."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "title": {"type": "string", "description": "Short title for the visual."},
                "spec": {
                    "type": "object",
                    "description": "JSON spec (e.g. Vega-Lite, or a simple chart config).",
                },
            },
            required=["spec"],
        ),
        handler=_create_visual,
    ),
    ToolDefinition(
        name="update_visual",
        description="Update the JSON spec of an existing visual by ID.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "visual_id": {"type": "string", "description": "Visual artifact UUID."},
                "spec": {"type": "object", "description": "New spec JSON."},
            },
            required=["visual_id", "spec"],
        ),
        handler=_update_visual,
    ),
]
