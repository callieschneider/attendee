"""
Visual tools — write a visual spec into an Artifact row with type=chart.
The canvas pump (Celery beat task `agent.canvas.pump.push_canvas_images`,
running every ~1s) renders these to PNG and POSTs them to Attendee. We
also kick a synchronous one-shot pump on the worker so the new visual
appears within ~50ms instead of waiting for the next tick.
"""
from __future__ import annotations

import json
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.visual")


def _coerce_spec(spec) -> dict | None:
    """Accept spec as either a dict or a JSON-encoded string."""
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str) and spec.strip():
        try:
            parsed = json.loads(spec)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _create_visual(inp: dict, ctx: dict) -> dict:
    from agent.models import Artifact

    from ._series_fallback import ensure_series_id

    spec = _coerce_spec(inp.get("spec"))
    if spec is None:
        return {"error": "spec must be an object or a JSON string"}
    title = (inp.get("title") or "Visual").strip()
    series_id = ensure_series_id(inp, ctx)

    # region agent log
    log.warning("DBG68285d D create_visual series=%s ctx_series=%s bot=%s spec_type=%s",
        series_id, ctx.get("series_id"), ctx.get("bot_id"), spec.get("type") if isinstance(spec, dict) else "?")
    # endregion

    try:
        artifact = Artifact.objects.create(
            series_id=series_id,
            title=title[:255],
            type="chart",
            content=json.dumps(spec)[:50000],  # generous limit for HTML
        )
    except Exception as exc:
        log.exception("create_visual: failed")
        return {"error": f"{type(exc).__name__}: {exc}"}

    _kick_canvas_pump(ctx.get("bot_id"))

    return {
        "success": True,
        "visual_id": str(artifact.id),
        "title": title,
        "message": "Visual queued — appears on the bot's tile within ~1 second.",
    }


def _kick_canvas_pump(bot_id: str | None) -> None:
    """Trigger an immediate one-shot canvas render+push for this bot.
    Best-effort — never raises. The Celery beat pump runs every ~1s anyway,
    this just shaves the worst-case wait."""
    if not bot_id:
        return
    try:
        from agent.canvas.pump import push_canvas_images_for_bot

        push_canvas_images_for_bot(bot_id)
    except Exception:
        log.exception("_kick_canvas_pump: failed bot=%s", bot_id)


def _update_visual(inp: dict, ctx: dict) -> dict:
    """
    Update an existing visual. Robust to hallucinated IDs: if the given
    ID doesn't exist, falls back to updating the most-recent visual for
    the series. If there's no visual at all, creates a new one.
    """
    from agent.models import Artifact

    spec = _coerce_spec(inp.get("spec"))
    if spec is None:
        return {"error": "spec must be an object or a JSON string"}
    visual_id = inp.get("visual_id")

    artifact = None
    if visual_id:
        try:
            artifact = Artifact.objects.get(id=visual_id, type="chart")
        except (Artifact.DoesNotExist, ValueError):
            log.info("update_visual: id %s not found, falling back to latest", visual_id)
            artifact = None

    if artifact is None:
        # Find the most recent chart for this bot's series
        from ._series_fallback import ensure_series_id

        series_id = ensure_series_id(inp, ctx)
        artifact = (
            Artifact.objects.filter(type="chart", series_id=series_id, is_deleted=False)
            .order_by("-updated_at")
            .first()
        )

    if artifact is None:
        # No existing visual — create one
        return _create_visual({"spec": spec, "title": inp.get("title", "Visual")}, ctx)

    artifact.content = json.dumps(spec)[:50000]
    if inp.get("title"):
        artifact.title = inp["title"][:255]
    artifact.save(update_fields=["content", "title", "updated_at"])

    _kick_canvas_pump(ctx.get("bot_id"))

    return {
        "success": True,
        "updated": True,
        "visual_id": str(artifact.id),
        "message": "Visual updated — re-renders within ~1 second.",
    }


_SPEC_DESCRIPTION = (
    "JSON spec describing what to render on the bot's video tile. STRONGLY PREFER simple\n"
    "server-rendered shapes — they appear in <500ms. The `html` shape is a last-resort\n"
    "fallback for genuinely custom layouts and adds 2-5s of headless-Chrome overhead.\n"
    "\n"
    "FAST shapes (use these for ~90% of cases):\n"
    '  - {"type":"list","items":["foo","bar", ...]}                     — bullet list (use for "show me bullet points / 5 things / a list")\n'
    '  - {"type":"bar","data":[{"label":"Q1","value":120}, ...]}        — bar chart\n'
    '  - {"type":"table","rows":[["Name","Status"],["Foo","OK"], ...]}  — first row is header\n'
    '  - {"type":"text","text":"..."}                                   — single text card\n'
    "\n"
    "SLOW shape (only when the above genuinely cannot express what was asked):\n"
    '  - {"type":"html","html":"<!DOCTYPE html><html>...</html>","title":"..."}\n'
    "      Full self-contained HTML page (inline CSS+SVG, no JS, no external resources).\n"
    "      Theme: dark bg #0a0b0f, light text #e5e7eb, accent #a5b4fc.\n"
    "      DO NOT compose long HTML inside this arg yourself; if you go this route,\n"
    "      first call `call_model` to draft the HTML, then pass it here.\n"
    "\n"
    "Keep data small (≤12 items). Always include a `type` field. For 'show 5 bullet "
    "points', the right answer is `{\"type\":\"list\",\"items\":[...]}` — never html."
)


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="create_visual",
        description=(
            "Render a visual on the bot's video tile in the meeting. The canvas "
            "updates within ~500ms when using simple types (list/bar/table/text). "
            "Use this any time the user asks to 'show', 'display', 'put up', 'draw', "
            "'visualize', or anything similar. PREFER `type: list/bar/table/text` "
            "for speed — only fall back to `type: html` for genuinely custom layouts."
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
        description=(
            "Replace the spec of an existing visual by ID. Same shape as create_visual.spec. "
            "If the ID is wrong, falls back to updating the most recent visual for this bot."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "visual_id": {
                    "type": "string",
                    "description": "Visual artifact UUID returned by create_visual.",
                },
                "spec": {"type": "object", "description": _SPEC_DESCRIPTION},
                "title": {"type": "string", "description": "Optional updated title."},
            },
            required=["visual_id", "spec"],
        ),
        handler=_update_visual,
    ),
]
