"""
Lightweight classifiers — currently just `is_addressed`.

`is_addressed` answers: "Given the last utterance, is the speaker directly
addressing the agent?" Uses a two-stage gate:
  1. Cheap lexical check (agent name appears at all; if not → NO).
  2. Gemini Flash Lite boolean classifier (if name matches).

Tuned for high specificity: false positives cost us a spoken interruption.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from django.conf import settings

log = logging.getLogger("agent.classifiers")


_PROMPT_TEMPLATE = """You are a boolean classifier. Decide whether the speaker in the given utterance is directly addressing the AI meeting agent named "{agent_name}".

ADDRESSED = YES cases:
- Vocative use: "{agent_name}, what do we have on the schedule?"
- Direct command: "{agent_name} pull up that doc"
- Clear question to an AI assistant in this context

ADDRESSED = NO cases:
- Third-person mention: "I think {agent_name} would probably say..."
- Quotation / reading
- Talking about the agent to another person

Respond with exactly one token: either YES or NO. No other text."""


def _name_appears(text: str, agent_name: str) -> bool:
    if not text or not agent_name:
        return False
    pattern = re.escape(agent_name)
    # Word-boundary match on normalized whitespace
    return bool(re.search(rf"\b{pattern}\b", text, flags=re.IGNORECASE))


def is_addressed(
    utterance_text: str, agent_name: Optional[str] = None
) -> bool:
    """
    Return True if the utterance is directly addressing the agent.
    Short-circuits to False if the agent name does not appear.
    """
    agent_name = agent_name or getattr(settings, "AGENT_NAME", "Clever Star")
    if not utterance_text or not _name_appears(utterance_text, agent_name):
        return False

    try:
        import google.generativeai as genai

        api_key = getattr(settings, "GOOGLE_API_KEY", "")
        if not api_key:
            return _heuristic_fallback(utterance_text, agent_name)

        genai.configure(api_key=api_key)
        model_name = getattr(
            settings, "AGENT_CLASSIFIER_MODEL", "gemini-2.5-flash-lite"
        )
        model = genai.GenerativeModel(
            model_name,
            system_instruction=_PROMPT_TEMPLATE.format(agent_name=agent_name),
        )
        resp = model.generate_content(
            f"Utterance: {utterance_text!r}",
            generation_config={"max_output_tokens": 3, "temperature": 0.0},
        )
        text = (getattr(resp, "text", "") or "").strip().upper()
        return text.startswith("Y")
    except Exception:
        log.exception("is_addressed: classifier call failed; falling back to heuristic")
        return _heuristic_fallback(utterance_text, agent_name)


def _heuristic_fallback(text: str, agent_name: str) -> bool:
    """
    Conservative fallback: treat the utterance as addressed if the agent name
    appears adjacent to sentence boundaries (i.e., looks vocative).
    """
    escaped = re.escape(agent_name)
    return bool(
        re.search(
            rf"(?:^|[.!?,\s]){escaped}(?:[.!?,\s:]|$)",
            text,
            flags=re.IGNORECASE,
        )
    )
