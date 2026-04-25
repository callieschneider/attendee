"""
Gemini Live bridge — ephemeral token minting and setup message construction.
Ported from abstraKt's live-voice-setup.ts + routes/api/live-voice/token/+server.ts.
"""
import logging

import requests
from django.conf import settings

from .tools import TOOL_REGISTRY
from .tools.adapters import to_gemini_declaration

log = logging.getLogger("agent.gemini_live")

AUTH_TOKENS_URL = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"


_LIVE_READ_ONLY_TOOL_NAMES = {
    "get_recent_occurrences",
    "get_occurrence_transcript",
    "get_meeting_notes",
    "list_upcoming_meetings",
    "get_series_context_bundle",
    "list_series",
    "list_tasks",
    "search_artifacts",
    "get_artifact",
    "semantic_search",
    "web_search",
    "fetch_url",
    "read_recent_chat",
    # Visual tools — write-mutating but user-facing and idempotent.
    # The canvas pump picks up the new spec on its next tick (~3s).
    "create_visual",
    "update_visual",
}


def _gather_tool_schemas_for_gemini_live() -> list[dict]:
    """
    Expose ALL tools to Gemini Live with BLOCKING behavior — same pattern
    as abstrakt's working Gemini Live setup.
    """
    decls = []
    for t in TOOL_REGISTRY.values():
        d = to_gemini_declaration(t)
        d["behavior"] = "BLOCKING"
        decls.append(d)
    return decls


def build_live_setup(
    system_prompt: str,
    voice: str = "Kore",
    session_resumption_handle: str | None = None,
) -> dict:
    """
    Build the Gemini Live setup message — ports abstrakt's working
    configuration from src/lib/server/live-voice-setup.ts.

    Key features:
    - thinkingConfig.thinkingLevel HIGH → Gemini Live's native reasoning
    - contextWindowCompression slidingWindow → long-conversation memory
    - All tools available with BLOCKING behavior
    - Input + output transcriptions for observability
    - Session resumption for the ~10-min cap
    """
    model = getattr(settings, "AGENT_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025")
    thinking_level = getattr(settings, "AGENT_LIVE_THINKING_LEVEL", "HIGH").upper()

    tool_declarations = _gather_tool_schemas_for_gemini_live()

    setup: dict = {
        "model": f"models/{model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
            "thinkingConfig": {
                "thinkingLevel": thinking_level,
            },
        },
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "tools": [{"functionDeclarations": tool_declarations}] if tool_declarations else [],
        "sessionResumption": (
            {"handle": session_resumption_handle} if session_resumption_handle else {}
        ),
        "contextWindowCompression": {"slidingWindow": {}},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }

    return {"setup": setup}


def mint_ephemeral_token(setup_msg: dict, ttl_seconds: int = 1800) -> dict:
    """
    Mint a single-use ephemeral Google AI token for a Gemini Live session.
    The token allows the audio client to open a WebSocket directly to Google.

    Returns: {"token": "...", "client_setup_message": {...}}
    """
    api_key = settings.GOOGLE_API_KEY
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not configured")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    expire_time = (now + timedelta(seconds=ttl_seconds)).isoformat()
    new_session_expire_time = (now + timedelta(minutes=5)).isoformat()

    url = f"{AUTH_TOKENS_URL}?key={api_key}"
    body = {
        "uses": 1,
        "expireTime": expire_time,
        "newSessionExpireTime": new_session_expire_time,
        "bidiGenerateContentSetup": setup_msg["setup"],
    }

    try:
        resp = requests.post(url, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        log.error("mint_ephemeral_token HTTP error: %s %s", e.response.status_code, e.response.text[:500])
        raise
    except requests.exceptions.RequestException as e:
        log.error("mint_ephemeral_token request failed: %s", e)
        raise

    token = data.get("name") or data.get("token")
    if not token:
        log.error("mint_ephemeral_token: no token in response: %s", data)
        raise ValueError(f"No token returned from Google API: {data}")

    log.info("mint_ephemeral_token: token minted, expires in %ds", ttl_seconds)
    return {
        "token": token,
        "client_setup_message": setup_msg,
    }
