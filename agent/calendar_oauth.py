"""
Google Calendar OAuth flow helpers.
Used by /agent/api/calendar/connect and /agent/api/calendar/callback.
"""
import logging
import urllib.parse

import requests as req
from django.conf import settings

log = logging.getLogger("agent.calendar_oauth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _redirect_uri(request=None) -> str:
    base = getattr(settings, "AGENT_APP_URL", "https://meeting-agent-web-production.up.railway.app")
    return f"{base}/agent/api/calendar/callback"


def get_oauth_url(request=None) -> str:
    """Build the Google OAuth authorization URL."""
    client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    if not client_id:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID not configured")

    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # always return refresh_token
        "login_hint": "meetingagent@latentspaceco.com",
    }
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_tokens(code: str, request=None) -> dict:
    """
    Exchange an OAuth authorization code for access_token + refresh_token.
    Returns the full token response dict.
    """
    client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID or GOOGLE_OAUTH_CLIENT_SECRET not configured")

    resp = req.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": _redirect_uri(request),
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_google_user_info(access_token: str) -> dict:
    """Retrieve basic profile info to confirm which account was authorized."""
    resp = req.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def register_calendar_with_attendee(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """
    Register the calendar with Attendee via POST /api/v1/calendars.
    Returns the created Calendar object.
    """
    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    agent_app_url = getattr(settings, "AGENT_APP_URL", "")

    payload = {
        "platform": "google",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "oauth_client_id": client_id,
        "deduplication_key": "meetingagent-calendar",
    }

    resp = req.post(
        f"{agent_app_url}/api/v1/calendars",
        json=payload,
        headers={"Authorization": f"Token {api_key}"},
        timeout=15,
    )
    if not resp.ok:
        log.error("register_calendar_with_attendee: %s %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    return resp.json()
