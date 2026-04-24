"""
Embedding service — ported from abstraKt's embeddings.ts.

Uses OpenAI text-embedding-3-small (1536 dims) with chunked storage
in agent_embedding_chunk via pgvector cosine similarity.
"""
import logging
import uuid
from typing import Optional

from django.conf import settings
from django.db import connection

log = logging.getLogger("agent.embeddings")

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 400


def _client():
    from openai import OpenAI
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    if not text:
        return []
    text = text.strip()
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def generate_embedding(text: str) -> list[float]:
    """Generate a single embedding vector."""
    resp = _client().embeddings.create(
        model=settings.AGENT_EMBEDDING_MODEL,
        input=text.strip(),
    )
    return resp.data[0].embedding


def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts (more efficient than one-at-a-time)."""
    if not texts:
        return []
    resp = _client().embeddings.create(
        model=settings.AGENT_EMBEDDING_MODEL,
        input=[t.strip() for t in texts],
    )
    return [d.embedding for d in resp.data]


def store_entity_embedding(entity_table: str, entity_id: uuid.UUID, text: str) -> int:
    """
    Chunk text, generate embeddings, persist as EmbeddingChunk rows.
    Deletes existing chunks for (entity_table, entity_id) first (idempotent).
    Returns number of chunks written.
    """
    from .models import EmbeddingChunk

    chunks = chunk_text(text)
    if not chunks:
        EmbeddingChunk.objects.filter(entity_table=entity_table, entity_id=entity_id).delete()
        return 0

    try:
        vectors = generate_embeddings_batch(chunks)
    except Exception:
        log.exception("store_entity_embedding: embedding API call failed for %s/%s", entity_table, entity_id)
        return 0

    EmbeddingChunk.objects.filter(entity_table=entity_table, entity_id=entity_id).delete()
    EmbeddingChunk.objects.bulk_create([
        EmbeddingChunk(
            entity_table=entity_table,
            entity_id=entity_id,
            chunk_index=i,
            content=chunk,
            embedding=vector,
        )
        for i, (chunk, vector) in enumerate(zip(chunks, vectors))
    ])
    log.info(
        "store_entity_embedding: wrote %d chunks for %s/%s",
        len(chunks), entity_table, entity_id,
    )
    return len(chunks)


def vector_search_chunked(
    entity_table: str,
    query_embedding: list[float],
    limit: int = 10,
    threshold: float = 0.35,
) -> list[dict]:
    """
    Cosine similarity search over EmbeddingChunk.
    Returns one best-matching chunk per entity, with similarity score.
    Uses DISTINCT ON via CTE — mirrors abstraKt's vectorSearchChunked.
    """
    emb_str = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    sql = """
        WITH ranked AS (
            SELECT
                ec.entity_id,
                ec.content,
                1 - (ec.embedding <=> %s::vector) AS similarity,
                ROW_NUMBER() OVER (
                    PARTITION BY ec.entity_id
                    ORDER BY ec.embedding <=> %s::vector
                ) AS rn
            FROM agent_embedding_chunk ec
            WHERE ec.entity_table = %s
        )
        SELECT entity_id, content, similarity
        FROM ranked
        WHERE rn = 1 AND similarity >= %s
        ORDER BY similarity DESC
        LIMIT %s
    """
    with connection.cursor() as cur:
        cur.execute(sql, [emb_str, emb_str, entity_table, threshold, limit])
        rows = cur.fetchall()
    return [{"entity_id": str(r[0]), "content": r[1], "similarity": float(r[2])} for r in rows]


def cross_entity_semantic_search(
    query: str,
    entity_tables: Optional[list[str]] = None,
    limit: int = 10,
    threshold: float = 0.30,
) -> list[dict]:
    """
    Search across multiple entity tables.
    Returns combined results sorted by similarity, with entity_table and entity_id.
    """
    if entity_tables is None:
        entity_tables = [
            "agent_meeting_occurrence",
            "agent_artifact",
            "agent_project_intelligence",
        ]

    try:
        query_embedding = generate_embedding(query)
    except Exception:
        log.exception("cross_entity_semantic_search: embedding generation failed")
        return []

    emb_str = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    table_placeholders = ",".join(["%s"] * len(entity_tables))
    sql = f"""
        WITH ranked AS (
            SELECT
                ec.entity_table,
                ec.entity_id,
                ec.content,
                1 - (ec.embedding <=> %s::vector) AS similarity,
                ROW_NUMBER() OVER (
                    PARTITION BY ec.entity_table, ec.entity_id
                    ORDER BY ec.embedding <=> %s::vector
                ) AS rn
            FROM agent_embedding_chunk ec
            WHERE ec.entity_table IN ({table_placeholders})
        )
        SELECT entity_table, entity_id, content, similarity
        FROM ranked
        WHERE rn = 1 AND similarity >= %s
        ORDER BY similarity DESC
        LIMIT %s
    """
    params = [emb_str, emb_str] + list(entity_tables) + [threshold, limit]
    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "entity_table": r[0],
            "entity_id": str(r[1]),
            "content": r[2],
            "similarity": float(r[3]),
        }
        for r in rows
    ]
