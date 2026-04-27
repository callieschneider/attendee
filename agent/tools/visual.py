"""
Chart-only visual tool.

Phase 2 of the canvas-rebuild plan retired the 8-spec visual system
(list / text / table / html / etc). The canvas web app now renders
markdown notes, focus content, and dashboards natively, so the only
remaining job for this module is genuine data visualisations: bar /
line / pie / KPI / flow charts.

The chart still lands as an Artifact row with type=chart; the dashboard
tab of the canvas web app picks it up and renders it.

For everything that ISN'T a chart (explanations, notes, lists, free-
form text), use `think_deep` (smart streaming text into the focus tab),
`update_notes`, or `navigate_canvas` instead.
"""
from __future__ import annotations

import json
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.visual")


_VALID_CHART_TYPES = ("bar", "line", "pie", "kpi", "flow")


def _coerce_spec(spec) -> dict | None:
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
    from agent.canvas_v2 import state as canvas_state
    from agent.models import Artifact

    from ._series_fallback import ensure_series_id

    spec = _coerce_spec(inp.get("spec"))
    if spec is None:
        return {"error": "spec must be an object or a JSON string"}
    chart_type = (spec.get("type") or "").strip().lower()
    if chart_type not in _VALID_CHART_TYPES:
        return {
            "error": (
                f"Unsupported chart type {chart_type!r}. Allowed: "
                f"{', '.join(_VALID_CHART_TYPES)}. For non-chart visuals "
                f"(explanations, notes, lists, text), call think_deep, "
                f"update_notes, or navigate_canvas instead."
            ),
        }

    title = (inp.get("title") or "Chart").strip()
    series_id = ensure_series_id(inp, ctx)

    try:
        artifact = Artifact.objects.create(
            series_id=series_id,
            title=title[:255],
            type="chart",
            content=json.dumps(spec)[:50000],
        )
    except Exception as exc:
        log.exception("create_visual: failed")
        return {"error": f"{type(exc).__name__}: {exc}"}

    bot_id = ctx.get("bot_id")
    if bot_id:
        try:
            canvas_state.update_dashboard(
                bot_id,
                {
                    "latest_chart": {
                        "id": str(artifact.id),
                        "title": title,
                        "spec": spec,
                    },
                },
            )
            canvas_state.navigate(bot_id, "dashboard", source="agent")
        except Exception:
            log.exception("create_visual: canvas_state push failed bot=%s", bot_id)

    return {
        "success": True,
        "visual_id": str(artifact.id),
        "title": title,
        "message": "Chart created — visible on the dashboard tab.",
    }


def _update_visual(inp: dict, ctx: dict) -> dict:
    from agent.canvas_v2 import state as canvas_state
    from agent.models import Artifact

    spec = _coerce_spec(inp.get("spec"))
    if spec is None:
        return {"error": "spec must be an object or a JSON string"}
    chart_type = (spec.get("type") or "").strip().lower()
    if chart_type not in _VALID_CHART_TYPES:
        return {
            "error": (
                f"Unsupported chart type {chart_type!r}. Allowed: "
                f"{', '.join(_VALID_CHART_TYPES)}."
            ),
        }

    visual_id = inp.get("visual_id")
    artifact = None
    if visual_id:
        try:
            artifact = Artifact.objects.get(id=visual_id, type="chart")
        except (Artifact.DoesNotExist, ValueError):
            artifact = None

    if artifact is None:
        from ._series_fallback import ensure_series_id
        series_id = ensure_series_id(inp, ctx)
        artifact = (
            Artifact.objects.filter(type="chart", series_id=series_id, is_deleted=False)
            .order_by("-updated_at")
            .first()
        )

    if artifact is None:
        return _create_visual({"spec": spec, "title": inp.get("title", "Chart")}, ctx)

    artifact.content = json.dumps(spec)[:50000]
    if inp.get("title"):
        artifact.title = inp["title"][:255]
    artifact.save(update_fields=["content", "title", "updated_at"])

    bot_id = ctx.get("bot_id")
    if bot_id:
        try:
            canvas_state.update_dashboard(
                bot_id,
                {
                    "latest_chart": {
                        "id": str(artifact.id),
                        "title": artifact.title,
                        "spec": spec,
                    },
                },
            )
        except Exception:
            log.exception("update_visual: canvas_state push failed bot=%s", bot_id)

    return {
        "success": True,
        "updated": True,
        "visual_id": str(artifact.id),
        "message": "Chart updated.",
    }


_SPEC_DESCRIPTION = (
    "JSON spec describing the chart. ONLY use for genuine data visualisations.\n"
    "For explanations, notes, lists, or free-form text use `think_deep`,\n"
    "`update_notes`, or `navigate_canvas` instead.\n"
    "\n"
    'Allowed shapes:\n'
    '  - {"type":"bar","data":[{"label":"Q1","value":120}, ...]}                         — bar chart, ≤12 bars\n'
    '  - {"type":"line","series":[{"label":"Revenue","data":[{"x":"Jan","y":100}, ...]}]} — line chart, up to 4 series\n'
    '  - {"type":"pie","data":[{"label":"iOS","value":60}, ...]}                          — pie chart, ≤8 slices\n'
    '  - {"type":"kpi","items":[{"label":"MRR","value":"$42k","delta":"+8%","delta_dir":"up"}, ...]} — KPI cards, ≤8 items\n'
    '  - {"type":"flow","nodes":[{"id":"a","label":"Discover"}],"edges":[{"from":"a","to":"b"}]}      — flowchart/diagram, ≤6 nodes\n'
)


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="create_visual",
        description=(
            "Create a chart on the dashboard tab. ONLY for explicit chart / "
            "graph / diagram / KPI requests — for everything else (notes, "
            "explanations, lists, text), use `think_deep`, `update_notes`, "
            "or `navigate_canvas`. Auto-switches the canvas to the dashboard "
            "tab so the user sees the chart immediately."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "title": {"type": "string", "description": "Short title shown above the chart."},
                "spec": {"type": "object", "description": _SPEC_DESCRIPTION},
            },
            required=["spec"],
        ),
        handler=_create_visual,
    ),
    ToolDefinition(
        name="update_visual",
        description=(
            "Replace the spec of an existing chart by ID. If the ID is wrong, "
            "falls back to updating the most recent chart for this bot. Charts "
            "only — see `create_visual` for the allowed types."
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
