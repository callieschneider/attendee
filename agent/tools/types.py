"""
Tool framework types — ported from abstraKt's lib/types.ts:ToolDefinition.
Single source of truth for all tool definitions; adapters convert to each LLM's format.
"""
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolSchema:
    type: str  # always "object"
    properties: dict[str, dict[str, Any]]
    required: list[str] = field(default_factory=list)


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: ToolSchema
    handler: Callable[..., Any]
    silent: bool = False  # If True, hide from LLM tool-use display in UI
