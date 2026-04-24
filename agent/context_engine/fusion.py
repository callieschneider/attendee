"""
Reciprocal Rank Fusion — merge ranked retrieval lists by summing 1/(k+rank).
Ported from abstraKt's context-builder.ts rrfFuse.
"""

from typing import Callable, Hashable, TypeVar

T = TypeVar("T")

def rrf_fuse(
    ranked_lists: list[list[T]],
    get_key: Callable[[T], Hashable],
    k: int = 60,
) -> list[tuple[T, float]]:
    """Reciprocal Rank Fusion.

    Merge multiple ranked result lists into a single list where each item's
    final score is the sum of 1/(k + rank) across all lists. The first
    encountered item for a given key is kept as the representative.

    Args:
        ranked_lists: Lists of ranked items.
        get_key: Function extracting a hashable key from an item.
        k: Fusion constant (default 60).

    Returns:
        List of (item, score) tuples sorted by descending score.
    """
    scores: dict[Hashable, list] = {}
    for lst in ranked_lists:
        for idx, item in enumerate(lst):
            key = get_key(item)
            contrib = 1.0 / (k + idx + 1)
            if key in scores:
                scores[key][1] += contrib
            else:
                scores[key] = [item, contrib]
    return sorted(
        ((item, score) for item, score in scores.values()),
        key=lambda x: x[1],
        reverse=True,
    )
