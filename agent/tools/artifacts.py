"""Artifact management tools with semantic search."""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.artifacts")


def _search_artifacts(inp: dict, ctx: dict) -> dict:
    from agent.embeddings import generate_embedding, vector_search_chunked
    from agent.models import Artifact

    query = inp.get("query", "").strip()
    series_id = inp.get("series_id") or ctx.get("series_id")
    limit = min(int(inp.get("limit", 5)), 20)

    if not query:
        return {"error": "query required"}

    try:
        emb = generate_embedding(query)
    except Exception as e:
        log.exception("search_artifacts: embedding generation failed")
        return {"error": f"embedding failed: {e}"}

    results = vector_search_chunked("agent_artifact", emb, limit=limit * 2, threshold=0.25)

    # Hydrate with full artifact data + apply series filter
    artifacts = []
    seen = set()
    for r in results:
        eid = r["entity_id"]
        if eid in seen:
            continue
        seen.add(eid)
        try:
            art = Artifact.objects.get(id=eid, is_deleted=False)
        except Artifact.DoesNotExist:
            continue
        if series_id and str(art.series_id) != str(series_id):
            continue
        artifacts.append({
            "id": str(art.id),
            "title": art.title,
            "type": art.type,
            "content": art.content[:400],
            "url": art.url,
            "tags": art.tags,
            "similarity": r["similarity"],
        })
        if len(artifacts) >= limit:
            break

    return {"artifacts": artifacts, "count": len(artifacts), "query": query}


def _create_artifact(inp: dict, ctx: dict) -> dict:
    from agent.models import Artifact, MeetingSeries

    series_id = inp.get("series_id") or ctx.get("series_id")
    if not series_id:
        return {"error": "series_id required"}
    try:
        series = MeetingSeries.objects.get(id=series_id)
    except MeetingSeries.DoesNotExist:
        return {"error": f"series {series_id} not found"}

    art = Artifact.objects.create(
        series=series,
        title=inp.get("title", "Untitled")[:255],
        type=inp.get("type", "note"),
        content=inp.get("content", ""),
        url=inp.get("url", ""),
        tags=inp.get("tags", []),
    )

    # Trigger async embedding
    from agent.tasks import embed_entity_async
    embed_entity_async.delay(
        entity_table="agent_artifact",
        entity_id=str(art.id),
        text=f"{art.title}\n\n{art.content}",
    )

    return {"created": True, "artifact_id": str(art.id), "title": art.title}


def _get_artifact(inp: dict, ctx: dict) -> dict:
    from agent.models import Artifact

    artifact_id = inp.get("artifact_id")
    if not artifact_id:
        return {"error": "artifact_id required"}
    try:
        art = Artifact.objects.get(id=artifact_id, is_deleted=False)
    except Artifact.DoesNotExist:
        return {"error": f"artifact {artifact_id} not found"}

    return {
        "id": str(art.id),
        "title": art.title,
        "type": art.type,
        "content": art.content,
        "url": art.url,
        "tags": art.tags,
        "created_at": art.created_at.isoformat(),
    }


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="search_artifacts",
        description="Semantically search artifacts (notes, links, files, charts) using natural language. Returns the most relevant results.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "query": {"type": "string", "description": "Natural language search query"},
                "series_id": {"type": "string", "description": "Limit to a specific meeting series (optional)"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            },
            required=["query"],
        ),
        handler=_search_artifacts,
    ),
    ToolDefinition(
        name="create_artifact",
        description="Save a note, link, or other artifact to a meeting series for future reference.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "series_id": {"type": "string", "description": "UUID of the MeetingSeries"},
                "title": {"type": "string", "description": "Artifact title"},
                "type": {
                    "type": "string",
                    "description": "Artifact type",
                    "enum": ["note", "link", "file", "chart", "image"],
                },
                "content": {"type": "string", "description": "Text content of the artifact"},
                "url": {"type": "string", "description": "URL if this is a link artifact"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags",
                },
            },
            required=["series_id", "title"],
        ),
        handler=_create_artifact,
    ),
    ToolDefinition(
        name="get_artifact",
        description="Get the full content of a specific artifact by ID.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "artifact_id": {"type": "string", "description": "UUID of the Artifact"},
            },
            required=["artifact_id"],
        ),
        handler=_get_artifact,
    ),
]
