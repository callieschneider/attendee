"""Cross-entity semantic search tool."""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.search")


def _semantic_search(inp: dict, ctx: dict) -> dict:
    from agent.embeddings import cross_entity_semantic_search

    query = inp.get("query", "").strip()
    limit = min(int(inp.get("limit", 8)), 20)

    if not query:
        return {"error": "query required"}

    results = cross_entity_semantic_search(query, limit=limit)
    return {"results": results, "count": len(results), "query": query}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="semantic_search",
        description="Search across all meeting occurrences, artifacts, and intelligence briefs using natural language. Use this to answer questions about past meetings or find relevant context.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "query": {"type": "string", "description": "Natural language search query"},
                "limit": {"type": "integer", "description": "Max results (default 8)"},
            },
            required=["query"],
        ),
        handler=_semantic_search,
    ),
]
