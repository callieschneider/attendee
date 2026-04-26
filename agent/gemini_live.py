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


# Tools that Gemini Live is allowed to call DIRECTLY. Live IS Clever Star —
# the user-facing entity — so it owns every tool the user could verbally
# request. The Turn Processor (Haiku 4.5) only runs in the background to
# capture missed action items (decisions, URLs, side-channel notes) when
# Live didn't already act.
#
# Earlier versions split tools between Live (read-only) and Turn Processor
# (writes). That created the "voice says On it but nothing happens" bug:
# Live narrated an acknowledgement, then the brain frequently failed to
# fire the actual tool. The single-decision-path model below fixes that.
#
# MUST stay in sync with `_LIVE_ALLOWED_TOOLS` in
# `agent/live_session/manager.py` (the runtime gate).
_LIVE_VISIBLE_TOOL_NAMES = {
    # Read / lookup
    "list_tasks",
    "list_series",
    "list_upcoming_meetings",
    "get_recent_occurrences",
    "get_occurrence_transcript",
    "get_meeting_notes",
    "get_series_context_bundle",
    "search_artifacts",
    "get_artifact",
    "semantic_search",
    "read_recent_chat",
    "web_search",
    "fetch_url",
    # Visuals — must be sub-second. Live owns these.
    "create_visual",
    "update_visual",
    # Tasks & artifacts — user verbally asks Clever Star to capture them.
    "create_task",
    "update_task_status",
    "create_artifact",
    "save_artifact_from_url",
    "promote_meeting_task",
    # Chat / email — secondary channels Live drives directly.
    "send_chat_message",
    "send_email_summary",
    # Heavier reasoning when a simple visual won't do.
    "call_model",
    # Voice state — Live calls these directly when the user signals
    # sleep/wake intent. Required for sub-second responsiveness.
    "voice_sleep",
    "voice_wake",
}


def _gather_tool_schemas_for_gemini_live() -> list[dict]:
    """
    Expose every user-facing tool to Gemini Live so it can act on requests
    immediately rather than narrating and hoping the Turn Processor follows
    through. Same BLOCKING behavior pattern as abstrakt's working setup.
    The Turn Processor stays in the loop only for background capture.
    """
    decls = []
    for t in TOOL_REGISTRY.values():
        if t.name not in _LIVE_VISIBLE_TOOL_NAMES:
            continue
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
    model = getattr(settings, "AGENT_LIVE_MODEL", "gemini-3.1-flash-live-preview")
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
        # Aggressive VAD so the user can interrupt mid-sentence and so the
        # first utterance after silence registers immediately. Defaults are
        # tuned for human-to-human pacing; for a meeting bot we want
        # snappier turn-taking.
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "disabled": False,
                "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                "prefixPaddingMs": 20,
                "silenceDurationMs": 100,
            },
        },
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
