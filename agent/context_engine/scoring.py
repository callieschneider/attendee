"""
Blended scoring for hybrid retrieval — similarity × recency.
Ported from abstraKt's context-builder.ts blendScore + recencyScore.
"""
import datetime
import math
from typing import Optional


def recency_score(
    created_at: Optional[datetime.datetime],
    now: Optional[datetime.datetime] = None,
    half_life_days: float = 14.0,
) -> float:
    """
    Exponential decay by age in days.
    Returns 1.0 for items created right now; ~0.5 after half_life_days.

    Returns 0.0 if created_at is None.
    Uses `now or datetime.datetime.now(datetime.timezone.utc)` as the reference.
    Both timestamps are assumed tz-aware; if created_at is naive, treat it as UTC.
    """
    if created_at is None:
        return 0.0
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    age_seconds = (now - created_at).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    return math.exp(-math.log(2.0) * age_days / half_life_days)


def blend_score(
    similarity: float,
    created_at: Optional[datetime.datetime],
    now: Optional[datetime.datetime] = None,
    half_life_days: float = 14.0,
    similarity_weight: float = 0.7,
    recency_weight: float = 0.3,
) -> float:
    """
    Blended similarity + recency score in [0, 1].
    similarity is expected to be in [0, 1] (cosine-derived).

    Clamps similarity to [0, 1] defensively.
    """
    sim = min(max(similarity, 0.0), 1.0)
    rec = recency_score(created_at, now, half_life_days)
    return similarity_weight * sim + recency_weight * rec
