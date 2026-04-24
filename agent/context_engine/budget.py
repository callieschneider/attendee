"""
Token budget management using tiktoken cl100k_base.
Ported from abstraKt's token-counter.ts.

Drop-in compatible with OpenAI-style chat messages: {role, content, [tool_calls]}.
"""
import logging
import json
from typing import Any

log = logging.getLogger("agent.context_engine.budget")

_ENCODER = None
_FALLBACK_WARNED = False


def _get_encoder():
    global _ENCODER, _FALLBACK_WARNED
    if _ENCODER is None:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            if not _FALLBACK_WARNED:
                log.warning("tiktoken not available, falling back to heuristic token count")
                _FALLBACK_WARNED = True
            _ENCODER = None
    return _ENCODER


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """
    Fast token count using tiktoken cl100k_base encoding.
    Falls back to a rough len(text)//4 heuristic if tiktoken is unavailable.
    Safe to call with empty string (returns 0).
    """
    if not text:
        return 0
    enc = _get_encoder()
    if enc:
        return len(enc.encode(text))
    return len(text) // 4


def count_message_tokens(message: dict, model: str = "gpt-4o") -> int:
    """
    Count tokens for a single chat message dict.
    Counts role + content; for tool_calls, serializes the JSON and counts that too.
    Adds a small per-message overhead (+4) to approximate OpenAI's message framing.
    """
    total = 0
    role = message.get("role", "")
    total += count_tokens(str(role), model)

    content = message.get("content", "")
    if isinstance(content, str):
        total += count_tokens(content, model)
    elif isinstance(content, list):
        total += count_tokens(json.dumps(content), model)
    else:
        total += count_tokens(str(content), model)

    if "tool_calls" in message:
        total += count_tokens(json.dumps(message["tool_calls"]), model)

    total += 4
    return total


def truncate_messages(
    messages: list[dict],
    budget_tokens: int,
    keep_floor: int = 10,
    model: str = "gpt-4o",
) -> list[dict]:
    """
    Walk messages oldest-first. Drop oldest user/assistant/tool messages until
    total tokens ≤ budget_tokens, but always keep:
      - Every `system` role message (never dropped — they carry context).
      - At least the last `keep_floor` non-system messages (if we have that many).

    Returns a NEW list; does not mutate input.
    """
    token_counts = [count_message_tokens(m, model) for m in messages]
    total = sum(token_counts)

    non_system_idxs = [i for i, m in enumerate(messages) if m.get("role") != "system"]
    keep_set = set(non_system_idxs[-keep_floor:]) if len(non_system_idxs) > keep_floor else set(non_system_idxs)

    drop_set = set()
    for idx in non_system_idxs:
        if total <= budget_tokens:
            break
        if idx in keep_set:
            continue
        total -= token_counts[idx]
        drop_set.add(idx)

    return [m for i, m in enumerate(messages) if i not in drop_set]


def truncate_text_to_budget(text: str, budget_tokens: int, model: str = "gpt-4o") -> str:
    """
    Truncate a single text string to fit the given token budget.
    If already within budget, returns unchanged.
    Truncation strategy: remove whole lines from the END until under budget;
    if a single line exceeds budget, do word-level trim from the end.
    Appends "\n[…truncated…]" suffix when truncation actually occurred.
    """
    if count_tokens(text, model) <= budget_tokens:
        return text

    lines = text.splitlines(True)
    while lines and count_tokens("".join(lines), model) > budget_tokens:
        lines.pop()

    truncated = False
    if lines:
        result = "".join(lines)
        truncated = True
    else:
        words = text.split()
        while words and count_tokens(" ".join(words), model) > budget_tokens:
            words.pop()
        result = " ".join(words)
        truncated = True

    if truncated:
        result = result.rstrip("\n") + "\n[…truncated…]"
    return result
