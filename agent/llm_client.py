"""
Unified LLM client — all text/tool LLM calls go through OpenRouter.

OpenRouter exposes an OpenAI-compatible Chat Completions API. We use the
`openai` SDK with a custom `base_url` so tool-calling, token counting, and
cost tracking all work identically to the native OpenAI client.

Scope:
- Turn Processor (tool-using Gemini 2.5 Flash)
- Direct-address classifier (Gemini 2.5 Flash Lite)
- Horizon summarizer (Gemini 2.5 Flash, text-only)

NOT in scope: Gemini Live WebSocket (voice path) — that uses Google's
native `generativelanguage.googleapis.com` WS endpoint directly. See
`agent/gemini_live.py` and `agent/live_session/manager.py`.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings

log = logging.getLogger("agent.llm_client")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Very rough cost estimate per 1K tokens — OpenRouter re-bills at the
# provider's rate; these are used only for our in-process budget tracker.
# Values for google/gemini-2.5-flash as of 2026-04: input $0.30/M, output $2.50/M.
# We conservatively include OpenRouter's ~5% markup.
_COST_PER_1K_INPUT = {
    "google/gemini-2.5-flash": Decimal("0.00031"),
    "google/gemini-2.5-flash-lite": Decimal("0.00015"),
}
_COST_PER_1K_OUTPUT = {
    "google/gemini-2.5-flash": Decimal("0.00262"),
    "google/gemini-2.5-flash-lite": Decimal("0.0006"),
}


_CLIENT = None


def _get_client():
    """Lazy-init singleton OpenAI client pointed at OpenRouter."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        from openai import OpenAI
    except Exception:
        log.exception("openai SDK unavailable")
        return None
    api_key = getattr(settings, "OPENROUTER_API_KEY", "") or ""
    if not api_key:
        log.warning("llm_client: OPENROUTER_API_KEY not configured")
        return None
    try:
        _CLIENT = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            default_headers={
                # OpenRouter recommends these headers so usage shows up in their dashboard
                "HTTP-Referer": "https://meeting-agent-web-production.up.railway.app",
                "X-Title": "Meeting Agent",
            },
        )
        return _CLIENT
    except Exception:
        log.exception("llm_client: OpenAI client init failed")
        return None


def chat_completion(
    model: str,
    messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    tool_choice: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: float = 30.0,
) -> dict:
    """
    OpenAI-compatible chat completion via OpenRouter.

    Returns a normalized dict:
        {
            "text": str,                      # accumulated assistant text
            "tool_calls": [                   # list of tool calls requested
                {"id": str, "name": str, "args": dict},
                ...
            ],
            "usage": {"prompt": int, "completion": int},
            "cost_usd": Decimal,
            "error": Optional[str],
        }
    Never raises.
    """
    client = _get_client()
    if client is None:
        return _err("openrouter client unavailable")

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
        if tool_choice is None:
            tool_choice = "auto"
        kwargs["tool_choice"] = tool_choice

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        return _err(f"openrouter call failed: {type(exc).__name__}: {exc}")

    text_out = ""
    tool_calls: list[dict] = []

    try:
        for choice in resp.choices or []:
            msg = choice.message
            content = getattr(msg, "content", "") or ""
            if content:
                text_out += content
            for tc in getattr(msg, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                if not fn:
                    continue
                name = getattr(fn, "name", "") or ""
                raw_args = getattr(fn, "arguments", "") or "{}"
                try:
                    import json

                    args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {"_raw": raw_args}
                tool_calls.append({
                    "id": getattr(tc, "id", "") or "",
                    "name": name,
                    "args": args,
                })
    except Exception:
        log.exception("chat_completion: response parse failed")

    # Cost estimate
    prompt_toks = 0
    completion_toks = 0
    try:
        usage = getattr(resp, "usage", None)
        if usage:
            prompt_toks = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_toks = int(getattr(usage, "completion_tokens", 0) or 0)
    except Exception:
        pass

    cost = _estimate_cost(model, prompt_toks, completion_toks)

    return {
        "text": text_out,
        "tool_calls": tool_calls,
        "usage": {"prompt": prompt_toks, "completion": completion_toks},
        "cost_usd": cost,
        "error": None,
    }


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    in_rate = _COST_PER_1K_INPUT.get(model, Decimal("0.0003"))
    out_rate = _COST_PER_1K_OUTPUT.get(model, Decimal("0.0015"))
    return (
        (Decimal(prompt_tokens) / Decimal("1000")) * in_rate
        + (Decimal(completion_tokens) / Decimal("1000")) * out_rate
    )


def _err(msg: str) -> dict:
    return {
        "text": "",
        "tool_calls": [],
        "usage": {"prompt": 0, "completion": 0},
        "cost_usd": Decimal("0"),
        "error": msg,
    }
