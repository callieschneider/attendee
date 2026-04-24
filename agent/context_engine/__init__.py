"""
Agent context engine — retrieval layers, scoring, fusion, diversity, token budget.
Ported from abstraKt's context-builder.ts + token-counter.ts, adapted for the
Meeting Agent's real-time turn-based architecture.
"""
from .budget import count_message_tokens, count_tokens, truncate_messages
from .dedup import deduplicate_items, jaccard_similarity, trigrams
from .fusion import rrf_fuse
from .mmr import mmr_rerank
from .scoring import blend_score, recency_score

__all__ = [
    "count_message_tokens",
    "count_tokens",
    "truncate_messages",
    "deduplicate_items",
    "jaccard_similarity",
    "trigrams",
    "rrf_fuse",
    "mmr_rerank",
    "blend_score",
    "recency_score",
]
