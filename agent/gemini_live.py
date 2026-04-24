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
}


def _gather_tool_schemas_for_gemini_live() -> list[dict]:
    """
    Only READ-ONLY tools are exposed directly to Gemini Live. All mutating
    tools go through the Turn Processor to preserve ActionLogEntry integrity.
    See plan §11 (Landmines / tool split).
    """
    return [
        to_gemini_declaration(t)
        for name, t in TOOL_REGISTRY.items()
        if name in _LIVE_READ_ONLY_TOOL_NAMES
    ]


def build_live_setup(
    system_prompt: str,
    voice: str = "Zephyr",
    session_resumption_handle: str | None = None,
    enable_transcriptions: bool = True,
) -> dict:
    """
    Build the bidiGenerateContentSetup message.
    This is sent as the first frame when opening a Gemini Live WebSocket.

    Optional `session_resumption_handle` lets us reopen an existing session
    transparently after the ~10-minute cap (the value comes from Gemini's
    own sessionResumptionUpdate messages).
    """
    model = getattr(settings, "AGENT_LIVE_MODEL", "gemini-3.1-flash-live-preview")

    tool_declarations = _gather_tool_schemas_for_gemini_live()

    setup: dict = {
        "model": f"models/{model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
        # Gemini Live handles VAD and interrupts natively when audio is streamed
        # continuously — no explicit config needed for v1alpha.
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "tools": [{"functionDeclarations": tool_declarations}] if tool_declarations else [],
    }

    # Session resumption — always included so Gemini tells us the handle
    setup["sessionResumption"] = (
        {"handle": session_resumption_handle} if session_resumption_handle else {}
    )

    # Native transcriptions (useful for verification + debugging)
    if enable_transcriptions:
        setup["inputAudioTranscription"] = {}
        setup["outputAudioTranscription"] = {}

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
