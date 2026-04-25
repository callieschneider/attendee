"""
Canvas pump — Celery task that renders the canvas PNG for every active
bot and POSTs it to Attendee's /api/v1/bots/<id>/output_image endpoint.

Scheduled via Celery Beat to run every 3 seconds. Only sends an image
if the state has meaningfully changed (otherwise Attendee would re-decode
the same frame every tick).
"""
from __future__ import annotations

import base64
import hashlib
import logging

import requests
from celery import shared_task
from django.conf import settings

log = logging.getLogger("agent.canvas.pump")


# Cache of last-sent image digest per bot, to avoid wasteful re-posts.
# In practice this lives per-worker-process — acceptable because Attendee
# gracefully handles re-posts anyway.
_LAST_DIGEST: dict[str, str] = {}


@shared_task(
    name="agent.canvas.pump.push_canvas_images",
    bind=True,
    time_limit=30,
    soft_time_limit=25,
)
def push_canvas_images(self) -> dict:
    """
    Iterate over every bot in a live state, render its canvas, and POST
    the PNG to Attendee. Called by Celery Beat every ~3s.
    """
    from bots.models import Bot

    # Bot states considered "live" — a crude filter; Bot.state is a bitfield
    # mapped to an int. Simpler: filter MeetingCursor which exists per active bot.
    live_bots = _live_bot_ids()
    if not live_bots:
        return {"pushed": 0}

    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    api_base = getattr(settings, "AGENT_APP_URL", "").rstrip("/")
    if not api_key or not api_base:
        log.warning("push_canvas_images: ATTENDEE_API_KEY / AGENT_APP_URL not set")
        return {"pushed": 0, "error": "missing config"}

    sent = 0
    skipped = 0
    for bot_id in live_bots:
        ok, was_skipped = _push_one(bot_id, api_base, api_key)
        sent += int(ok)
        skipped += int(was_skipped)
    return {"pushed": sent, "skipped": skipped, "scanned": len(live_bots)}


def _live_bot_ids() -> list[str]:
    """Return bot_ids that should get canvas updates."""
    from agent.models import MeetingCursor

    # Active = cursor updated in the last ~10 min
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(minutes=10)
    return list(
        MeetingCursor.objects.filter(updated_at__gte=cutoff).values_list("bot_id", flat=True)
    )


def _push_one(bot_id: str, api_base: str, api_key: str) -> tuple[bool, bool]:
    """Render + POST a canvas image for one bot. Returns (sent, skipped_same)."""
    from .renderer import render_canvas_png

    try:
        png = render_canvas_png(bot_id)
    except Exception:
        log.exception("push_canvas_images: render failed bot=%s", bot_id)
        return False, False

    if not png:
        return False, False

    digest = hashlib.sha256(png).hexdigest()
    if _LAST_DIGEST.get(bot_id) == digest:
        return False, True

    try:
        resp = requests.post(
            f"{api_base}/api/v1/bots/{bot_id}/output_image",
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
            },
            json={"type": "image/png", "data": base64.b64encode(png).decode()},
            timeout=10,
        )
    except Exception:
        log.exception("push_canvas_images: POST failed bot=%s", bot_id)
        return False, False

    if resp.status_code >= 400:
        # Don't spam on expected 400s (e.g., bot no longer in state_that_can_play_media)
        log.info(
            "push_canvas_images: HTTP %s bot=%s — %s",
            resp.status_code, bot_id, resp.text[:160],
        )
        return False, False

    _LAST_DIGEST[bot_id] = digest
    return True, False
