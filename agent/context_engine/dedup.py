"""
Trigram-based near-duplicate detection.
Ported from abstraKt's context-builder.ts deduplicateItems.
"""
import re
from typing import Callable, TypeVar

T = TypeVar("T")


def trigrams(text: str) -> set[str]:
    s = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if len(s) < 3:
        return set()
    return {s[i : i + 3] for i in range(len(s) - 2)}


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


def deduplicate_items(
    items: list[T],
    get_text: Callable[[T], str],
    threshold: float = 0.85,
) -> list[T]:
    kept: list[T] = []
    kept_trigrams: list[set[str]] = []
    for item in items:
        tg = trigrams(get_text(item))
        if not tg:
            kept.append(item)
            kept_trigrams.append(tg)
            continue
        if all(jaccard_similarity(tg, kt) < threshold for kt in kept_trigrams):
            kept.append(item)
            kept_trigrams.append(tg)
    return kept
