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


def build_live_setup(system_prompt: str, voice: str = "Zephyr") -> dict:
    """
    Build the bidiGenerateContentSetup message.
    This is sent as the first frame when opening a Gemini Live WebSocket.
    """
    model = getattr(settings, "AGENT_LIVE_MODEL", "gemini-3.1-flash-live-preview")

    tool_declarations = [to_gemini_declaration(t) for t in TOOL_REGISTRY.values()]

    return {
        "setup": {
            "model": f"models/{model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice}
                    }
                },
            },
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            "tools": [{"functionDeclarations": tool_declarations}],
        }
    }


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
