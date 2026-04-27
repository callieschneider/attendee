"""
think_deep — Gemini Live's escape hatch to a smarter model for synthesis,
explanation, comparison, or research. The result streams into the active
canvas tab (default: focus) so the user sees progressive output while
Gemini Live verbally narrates "one moment, thinking…".

Phase 1 implementation:
- Issues a streaming chat completion against Anthropic Claude Haiku 4.5
  via OpenRouter (override-able via input).
- Each chunk is published to two Redis channels so the canvas can render
  them as they arrive:
    * `canvas:stream:{bot_id}:{tab}`        (per-bot per-tab stream)
    * `canvas:focus:{bot_id}`               (latest snapshot key, JSON-encoded)
- The full accumulated text is stored under
  `canvas:focus_text:{bot_id}:{tab}` (TTL 1h) so a late-joining canvas
  client can backfill.
- Returns the full text plus token/cost metadata to the caller (Gemini
  Live), which uses it to read back a conversational summary.

Phase 2 will replace the Redis-pubsub bridge with Django Channels
fan-out into the Next.js canvas-app. Until then, the publish is a no-op
on the consumer side but the data still lands in the snapshot key, which
is enough for the existing PIL renderer to surface the focus content.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Iterable, Optional

from django.conf import settings

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.think_deep")


_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
_DEFAULT_TAB = "focus"
_VALID_TABS = ("dashboard", "notes", "tasks", "focus")
_FOCUS_TTL_SECONDS = 60 * 60  # 1 hour — long enough for a meeting


# ── Redis helpers ─────────────────────────────────────────────────────────────


_REDIS = None


def _redis():
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    try:
        import redis
    except Exception:
        log.exception("think_deep: redis package missing")
        return None
    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    try:
        _REDIS = redis.from_url(url, decode_responses=True)
        return _REDIS
    except Exception:
        log.exception("think_deep: redis.from_url failed")
        return None


def _publish(bot_id: str, tab: str, payload: dict) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.publish(f"canvas:stream:{bot_id}:{tab}", json.dumps(payload))
    except Exception:
        log.exception("think_deep: publish failed bot=%s tab=%s", bot_id, tab)


def _store_snapshot(bot_id: str, tab: str, session_id: str, text: str, done: bool) -> None:
    # Redis snapshot key (fast lookup for transient consumers).
    r = _redis()
    if r is not None:
        try:
            r.set(
                f"canvas:focus_text:{bot_id}:{tab}",
                json.dumps({
                    "session_id": session_id,
                    "text": text,
                    "done": done,
                    "updated_at": time.time(),
                }),
                ex=_FOCUS_TTL_SECONDS,
            )
        except Exception:
            log.exception("think_deep: redis snapshot store failed bot=%s tab=%s", bot_id, tab)

    # Persistent CanvasState row + state-event publish (so the canvas web
    # app can surface the focus content immediately).
    if tab == "focus":
        try:
            from agent.canvas_v2 import state as canvas_state
            canvas_state.update_focus(
                bot_id, session_id=session_id, text=text, done=done,
            )
        except Exception:
            log.exception("think_deep: canvas_state.update_focus failed bot=%s", bot_id)


# ── Streaming primitive ───────────────────────────────────────────────────────


def _stream_chunks(model: str, prompt: str, system_prompt: Optional[str]) -> Iterable[str]:
    """
    Yield raw text chunks from OpenRouter as they arrive. Falls back to a
    single non-streaming chunk if the SDK refuses to stream for any reason.
    """
    from agent.llm_client import _get_client

    client = _get_client()
    if client is None:
        raise RuntimeError("OpenRouter client unavailable (missing OPENROUTER_API_KEY?)")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.4,
        max_tokens=2048,
        stream=True,
        timeout=60.0,
    )
    for chunk in stream:
        try:
            delta = chunk.choices[0].delta
        except Exception:
            continue
        text = getattr(delta, "content", None)
        if text:
            yield text


# ── Tool handler ──────────────────────────────────────────────────────────────


_DEFAULT_SYSTEM_PROMPT = (
    "You are the Clever Star deep-thinking assistant. You're being called by "
    "the live voice agent to draft a thoughtful, structured answer that will "
    "stream onto the user's canvas. Use clean markdown: a short title (one "
    "line, no leading hashes), 4-8 bullets or short paragraphs, no preamble, "
    "no apologies. Be specific and useful. Plain text only — no code fences "
    "unless code is genuinely required."
)


def _think_deep(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}

    prompt = (inp.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt required"}

    tab = (inp.get("target_tab") or _DEFAULT_TAB).strip()
    if tab not in _VALID_TABS:
        tab = _DEFAULT_TAB

    model = (inp.get("model") or "").strip() or getattr(
        settings, "AGENT_THINK_DEEP_MODEL", _DEFAULT_MODEL
    )
    system_prompt = (inp.get("system_prompt") or "").strip() or _DEFAULT_SYSTEM_PROMPT

    session_id = str(uuid.uuid4())
    started_at = time.time()

    _publish(bot_id, tab, {"event": "start", "session_id": session_id, "model": model, "tab": tab})
    _store_snapshot(bot_id, tab, session_id, "", done=False)

    accumulated: list[str] = []
    last_publish = 0.0
    PUBLISH_HZ = 8.0  # cap pubsub at 8 Hz to keep Redis happy
    try:
        for piece in _stream_chunks(model, prompt, system_prompt):
            accumulated.append(piece)
            now = time.time()
            if (now - last_publish) >= (1.0 / PUBLISH_HZ):
                joined = "".join(accumulated)
                _publish(bot_id, tab, {"event": "delta", "session_id": session_id, "text": joined})
                _store_snapshot(bot_id, tab, session_id, joined, done=False)
                last_publish = now
    except Exception as exc:
        log.exception("think_deep: stream failed bot=%s tab=%s", bot_id, tab)
        return {"error": f"{type(exc).__name__}: {exc}"}

    full_text = "".join(accumulated).strip()
    _publish(bot_id, tab, {"event": "done", "session_id": session_id, "text": full_text})
    _store_snapshot(bot_id, tab, session_id, full_text, done=True)

    elapsed_ms = int((time.time() - started_at) * 1000)
    log.info(
        "think_deep: bot=%s tab=%s model=%s ms=%d chars=%d",
        bot_id, tab, model, elapsed_ms, len(full_text),
    )

    return {
        "ok": True,
        "session_id": session_id,
        "tab": tab,
        "content": full_text,
        "elapsed_ms": elapsed_ms,
        "model": model,
    }


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="think_deep",
        description=(
            "Call a smarter model (Claude Haiku 4.5 by default) for synthesis, "
            "explanation, comparison, or research. Use whenever you need depth "
            "beyond a quick voice reply — explaining a concept, summarizing a "
            "doc, drafting a structured answer. The result streams onto the "
            "user's canvas in the chosen tab. Tell the user 'one moment, "
            "thinking' BEFORE calling so they know to look at the canvas, "
            "then read a short conversational summary of the result back to "
            "them when it returns. Default tab is 'focus'."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "prompt": {
                    "type": "string",
                    "description": (
                        "What to think about. Be specific — write the prompt "
                        "as if you were briefing a smart colleague."
                    ),
                },
                "target_tab": {
                    "type": "string",
                    "description": (
                        "Canvas tab to stream into. 'focus' for explanations "
                        "and analysis (default), 'notes' to append to running "
                        "meeting notes, 'dashboard' or 'tasks' rarely."
                    ),
                    "enum": list(_VALID_TABS),
                },
            },
            required=["prompt"],
        ),
        handler=_think_deep,
    ),
]
