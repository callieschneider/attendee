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
# GOOGLE_API_KEY: only used for Gemini Live WebSocket (voice path).
# OPENROUTER_API_KEY: used for ALL other LLM calls (turn processor,
# classifier, summarizer, horizon compression).
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
AGENT_EMBEDDING_MODEL = "text-embedding-3-small"
AGENT_EMBEDDING_DIMS = 1536
AGENT_SUMMARIZER_MODEL = os.getenv("AGENT_SUMMARIZER_MODEL", "google/gemini-2.5-flash")
AGENT_LIVE_MODEL = os.getenv("AGENT_LIVE_MODEL", "gemini-3.1-flash-live-preview")
AGENT_DEFAULT_VOICE = os.getenv("AGENT_DEFAULT_VOICE", "Zephyr")

# ---- Phase 5 agent core rewrite ----
# Model IDs use OpenRouter's `<provider>/<model>` format.
AGENT_NAME = os.getenv("AGENT_NAME", "Clever Star")
AGENT_TURN_MODEL = os.getenv("AGENT_TURN_MODEL", "google/gemini-2.5-flash")
AGENT_CLASSIFIER_MODEL = os.getenv("AGENT_CLASSIFIER_MODEL", "google/gemini-2.5-flash-lite")
AGENT_TURN_WINDOW_SECONDS = float(os.getenv("AGENT_TURN_WINDOW_SECONDS", "4"))
AGENT_PAUSE_THRESHOLD_SECONDS = float(os.getenv("AGENT_PAUSE_THRESHOLD_SECONDS", "1.0"))
AGENT_PAUSE_MIN_CONTENT_SECONDS = float(os.getenv("AGENT_PAUSE_MIN_CONTENT_SECONDS", "2.5"))

# How long the audio gate stays open by default after session start /
# wake phrase. Refreshed on every speech event.
AGENT_GATE_DEFAULT_TTL_SECONDS = int(os.getenv("AGENT_GATE_DEFAULT_TTL_SECONDS", "1800"))

# Canvas pump runs as a daemon thread inside the web (gunicorn) container,
# NOT as a celery beat task. The worker container has
# CELERY_WORKER_MAX_TASKS_PER_CHILD=1, which forks a brand-new Python
# process per task — completely incompatible with a 0.5s tick rate (we
# observed 1700+ message backlog with no progress). The daemon-thread
# version reuses one chromedriver across iterations and uses a Redis
# lock so only one of the N gunicorn workers actually drives the pump.
# See agent/canvas/pump_daemon.py.
CELERY_BEAT_SCHEDULE = {}
AGENT_MAX_TURN_BUDGET_USD = float(os.getenv("AGENT_MAX_TURN_BUDGET_USD", "10.00"))
AGENT_SEMANTIC_TOKEN_BUDGET = int(os.getenv("AGENT_SEMANTIC_TOKEN_BUDGET", "10500"))
AGENT_MMR_LAMBDA = float(os.getenv("AGENT_MMR_LAMBDA", "0.7"))

# Agent tasks run on the default 'celery' queue — same worker handles everything.

# ---- Attendee webhook secret (for HMAC verification) ----
ATTENDEE_WEBHOOK_SECRET = os.getenv("ATTENDEE_WEBHOOK_SECRET", "")

# ---- Bridge + Calendar settings ----
BRIDGE_DOMAIN = os.getenv("BRIDGE_DOMAIN", "")
AGENT_APP_URL = os.getenv("AGENT_APP_URL", "https://meeting-agent-web-production.up.railway.app")
ATTENDEE_API_KEY = os.getenv("ATTENDEE_API_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
