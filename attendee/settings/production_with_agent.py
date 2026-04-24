"""
Production settings extended with the agent app.
DJANGO_SETTINGS_MODULE=attendee.settings.production_with_agent
"""
import os

from .production import *  # noqa: F401,F403
from .production import INSTALLED_APPS

# ---- Agent app ----
INSTALLED_APPS = list(INSTALLED_APPS) + [
    "pgvector.django",
    "agent",
]

# ---- Agent LLM settings ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
AGENT_EMBEDDING_MODEL = "text-embedding-3-small"
AGENT_EMBEDDING_DIMS = 1536
AGENT_SUMMARIZER_MODEL = os.getenv("AGENT_SUMMARIZER_MODEL", "gemini-2.5-flash")
AGENT_LIVE_MODEL = os.getenv("AGENT_LIVE_MODEL", "gemini-3.1-flash-live-preview")
AGENT_DEFAULT_VOICE = os.getenv("AGENT_DEFAULT_VOICE", "Zephyr")

# Agent tasks run on the default 'celery' queue — same worker handles everything.

# ---- Attendee webhook secret (for HMAC verification) ----
ATTENDEE_WEBHOOK_SECRET = os.getenv("ATTENDEE_WEBHOOK_SECRET", "")
