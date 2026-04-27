"""
canvas_v2 — Multi-tab canvas web app served from Django.

Phase 2 of the canvas-rebuild plan replaces the PIL-rendered PNG canvas
with a real HTML/React UI rendered in a browser. The bot's video tile
(Phase 3) becomes a CDP screenshot of this same web app, and Phase 4
shares it via Meet's Present-mode for full WebRTC quality.

Endpoints (registered under `/agent/canvas/v2/`):
  GET  /<bot_id>/                — HTML shell (the Next.js / single-file React app)
  GET  /<bot_id>/state.json      — Full canvas snapshot (used on join)
  GET  /<bot_id>/stream          — SSE stream of state deltas (Redis pubsub)
  POST /<bot_id>/navigate        — User-driven tab switch (Phase 4)

All real-time fan-out is via Redis pubsub channels:
  canvas:state:{bot_id}                — full-state invalidation events
  canvas:stream:{bot_id}:{tab}         — chunked text streaming (think_deep)

The agent writes into `agent.models.CanvasState` and publishes the change.
The SSE consumer subscribes to both channels and forwards events to the
client.
"""
