"""
call_model tool — let Gemini Live escalate to a smarter model when needed.

Ported from abstrakt's src/lib/server/tools/models.ts. The Gemini Live voice
model handles conversation and simple tools; for heavy reasoning (analyzing
data, writing rich HTML for visuals, complex decisions) it calls
`call_model` with model="anthropic/claude-haiku-4.5" or similar.

Returns the smarter model's text output. Gemini Live then summarizes for the
user or uses the result as input for further tool calls.
"""
from __future__ import annotations

import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.models")


def _call_model(inp: dict, ctx: dict) -> dict:
    from agent.llm_client import chat_completion

    model = (inp.get("model") or "").strip()
    message = (inp.get("message") or "").strip()
    if not model:
        return {"error": "model is required (e.g. 'anthropic/claude-haiku-4.5')"}
    if not message:
        return {"error": "message is required"}

    system_prompt = (inp.get("system_prompt") or "").strip()
    temperature = inp.get("temperature")
    max_tokens = inp.get("max_tokens")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})

    try:
        result = chat_completion(
            model=model,
            messages=messages,
            temperature=float(temperature) if temperature is not None else 0.3,
            max_tokens=int(max_tokens) if max_tokens else 4096,
            timeout=60.0,
        )
    except Exception as exc:
        log.exception("call_model: failed model=%s", model)
        return {"error": f"{type(exc).__name__}: {exc}"}

    if result.get("error"):
        return {"error": result["error"]}

    text = result.get("text", "") or ""
    usage = result.get("usage") or {}
    return {
        "content": text,
        "model": model,
        "tokens": {
            "prompt": usage.get("prompt", 0),
            "completion": usage.get("completion", 0),
        },
        "cost_usd": str(result.get("cost_usd", 0)),
    }


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="call_model",
        description=(
            "Make an ephemeral call to a smarter AI model when you need heavy "
            "reasoning, complex analysis, or rich content generation that exceeds "
            "your own capacity. Use this for: writing rich HTML for visualizations, "
            "analyzing complex data, multi-step reasoning, or getting a second "
            "opinion. Returns the model's text output. Common models: "
            "'anthropic/claude-haiku-4.5' (fast + smart), "
            "'anthropic/claude-sonnet-4.5' (smartest), "
            "'google/gemini-2.5-pro' (long context). "
            "When asked to 'show', 'visualize', or 'create' anything substantial: "
            "call_model first to generate the HTML, then pass it to create_visual."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "model": {
                    "type": "string",
                    "description": "Full OpenRouter model ID (e.g. 'anthropic/claude-haiku-4.5').",
                },
                "message": {
                    "type": "string",
                    "description": "The prompt/question/task for the model. Be detailed.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Optional system prompt to set the model's persona/instructions.",
                },
                "temperature": {
                    "type": "number",
                    "description": "Sampling temperature 0–2 (default 0.3).",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max tokens in response (default 4096).",
                },
            },
            required=["model", "message"],
        ),
        handler=_call_model,
    ),
]
