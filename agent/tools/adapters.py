"""
LLM schema adapters — convert ToolDefinition to each provider's wire format.
Mirrors abstraKt's approach: one source of truth, three adapters.
"""
from .types import ToolDefinition

_JSON_TYPE_TO_GEMINI = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _prop_to_gemini(prop: dict) -> dict:
    """Recursively convert a JSON Schema property to Gemini schema."""
    out: dict = {"type": _JSON_TYPE_TO_GEMINI.get(prop.get("type", "string"), "STRING")}
    if "description" in prop:
        out["description"] = prop["description"]
    if "enum" in prop:
        out["enum"] = prop["enum"]
    if out["type"] == "ARRAY" and "items" in prop:
        out["items"] = _prop_to_gemini(prop["items"])
    if out["type"] == "OBJECT" and "properties" in prop:
        out["properties"] = {k: _prop_to_gemini(v) for k, v in prop["properties"].items()}
    return out


def to_gemini_declaration(tool: ToolDefinition) -> dict:
    """Convert to Gemini Live bidiGenerateContent function declaration format."""
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": {
            "type": "OBJECT",
            "properties": {
                k: _prop_to_gemini(v) for k, v in tool.input_schema.properties.items()
            },
            "required": tool.input_schema.required,
        },
    }


def to_openai_function(tool: ToolDefinition) -> dict:
    """Convert to OpenAI function-calling format (also works for OpenRouter)."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": tool.input_schema.properties,
                "required": tool.input_schema.required,
            },
        },
    }


def to_claude_native(tool: ToolDefinition) -> dict:
    """Convert to Anthropic Claude native tool format (already our internal schema shape)."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": {
            "type": "object",
            "properties": tool.input_schema.properties,
            "required": tool.input_schema.required,
        },
    }
