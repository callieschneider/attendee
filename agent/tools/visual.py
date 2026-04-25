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

    # Pre-render HTML specs immediately so the canvas updates right away
    # (instead of waiting for the 3s pump cycle to re-render).
    if spec.get("type") == "html" and spec.get("html"):
        _render_html_and_push(artifact, ctx.get("bot_id"))

    return {
        "success": True,
        "visual_id": str(artifact.id),
        "rendered": True,
        "message": "Done. Visual is up.",
    }


def _render_html_and_push(artifact, bot_id: str | None) -> None:
    """
    Immediately render an HTML spec artifact to PNG and push it to the
    bot's video feed. Best-effort — never raises.
    """
    if not bot_id:
        return
    try:
        import base64
        import requests
        from django.conf import settings
        from agent.canvas.html_renderer import render_html_to_png
        import json

        spec = json.loads(artifact.content or "{}")
        html = spec.get("html", "")
        if not html:
            return

        png = render_html_to_png(html)
        if not png:
            log.warning("_render_html_and_push: renderer returned None bot=%s", bot_id)
            return

        api_key = getattr(settings, "ATTENDEE_API_KEY", "")
        api_base = getattr(settings, "AGENT_APP_URL", "").rstrip("/")
        resp = requests.post(
            f"{api_base}/api/v1/bots/{bot_id}/output_image",
            headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
            json={"type": "image/png", "data": base64.b64encode(png).decode()},
            timeout=10,
        )
        if resp.status_code >= 400:
            log.warning("_render_html_and_push: HTTP %s bot=%s", resp.status_code, bot_id)
    except Exception:
        log.exception("_render_html_and_push: failed bot=%s", bot_id)


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

    # Immediately push HTML renders
    if spec.get("type") == "html" and spec.get("html"):
        _render_html_and_push(artifact, ctx.get("bot_id"))

    return {
        "success": True,
        "updated": True,
        "visual_id": str(artifact.id),
        "message": "Done. Visual updated.",
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
