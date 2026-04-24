"""
Maximal Marginal Relevance (MMR) diversity reranker.
Ported from abstraKt's context-builder.ts mmrRerank.
"""
from typing import Callable, TypeVar

T = TypeVar("T")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def mmr_rerank(
    items: list[T],
    query_embedding: list[float],
    get_embedding: Callable[[T], list[float] | None],
    lambda_: float = 0.7,
    top_k: int | None = None,
) -> list[T]:
    """
    Maximal Marginal Relevance rerank.

    Greedy selection: at each step pick the item that maximizes
        lambda * sim(item, query) - (1 - lambda) * max_sim(item, selected)

    Higher lambda = prioritize similarity to query.
    Lower lambda = prioritize diversity.

    Items whose embedding is None are appended at the end in their
    original order (cannot contribute to MMR math).
    """
    with_emb: list[tuple[T, list[float]]] = []
    no_emb: list[T] = []
    for it in items:
        emb = get_embedding(it)
        if emb is None:
            no_emb.append(it)
        else:
            with_emb.append((it, emb))

    if not with_emb:
        return no_emb

    query_sims = [_cosine(e, query_embedding) for _, e in with_emb]

    selected: list[T] = []
    sel_embs: list[list[float]] = []
    remaining = set(range(len(with_emb)))
    limit = top_k if top_k is not None else len(with_emb)

    while len(selected) < limit and remaining:
        best_idx = None
        best_score = -float("inf")
        for i in remaining:
            sim_q = query_sims[i]
            if sel_embs:
                max_sim = max(_cosine(with_emb[i][1], s) for s in sel_embs)
            else:
                max_sim = 0.0
            score = lambda_ * sim_q - (1 - lambda_) * max_sim
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx is None:
            break
        selected.append(with_emb[best_idx][0])
        sel_embs.append(with_emb[best_idx][1])
        remaining.remove(best_idx)

    return selected + no_emb
