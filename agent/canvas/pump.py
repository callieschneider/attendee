"""
Canvas pump — Celery task that screenshots the canvas web app for every
active bot and POSTs the PNG to Attendee's `/output_image` endpoint so
the bot's video tile shows the canvas.

Phase 3 of the canvas-rebuild plan replaced the PIL renderer with a
headless-Chrome screenshot of the actual canvas-v2 URL. The URL is the
same page a user can open in their own browser, so the bot's video tile
and the user's browser show pixel-identical content.

Scheduled via Celery Beat to run every ~3s. Only sends an image if the
state has meaningfully changed.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time

import requests
from celery import shared_task
from django.conf import settings

log = logging.getLogger("agent.canvas.pump")


_LAST_DIGEST: dict[str, str] = {}


# ── Long-lived headless Chrome process ────────────────────────────────────────
#
# Spinning up a fresh chromedriver + visiting the canvas URL on every tick is
# slow (~2-3s cold start) and burns CPU. We keep one chromedriver process per
# worker, navigate it to the canvas URL once, and leave it open. The canvas
# page is a live SSE-driven SPA, so the same loaded tab stays current.

_DRIVER = None
_DRIVER_BOT_ID: str | None = None


CANVAS_W = 1920
CANVAS_H = 1080


def _get_driver():
    global _DRIVER
    if _DRIVER is not None:
        try:
            _ = _DRIVER.title  # liveness ping
            return _DRIVER
        except Exception:
            try:
                _DRIVER.quit()
            except Exception:
                pass
            _DRIVER = None

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except Exception:
        log.exception("canvas_pump: selenium import failed")
        return None

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"--window-size={CANVAS_W},{CANVAS_H}")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--force-device-scale-factor=1")

    for driver_path in [
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
        "chromedriver",
    ]:
        if os.path.exists(driver_path) or driver_path == "chromedriver":
            try:
                service = Service(executable_path=driver_path)
                _DRIVER = webdriver.Chrome(service=service, options=options)
                _DRIVER.set_window_size(CANVAS_W, CANVAS_H)
                return _DRIVER
            except Exception:
                continue
    log.warning("canvas_pump: no chromedriver found")
    return None


def _navigate_if_needed(driver, bot_id: str) -> bool:
    global _DRIVER_BOT_ID
    if _DRIVER_BOT_ID == bot_id:
        return True
    api_base = getattr(settings, "AGENT_APP_URL", "").rstrip("/")
    if not api_base:
        return False
    url = f"{api_base}/agent/canvas/v2/{bot_id}/"
    try:
        driver.get(url)
        time.sleep(0.6)  # let SSE backfill the snapshot + first paint
        _DRIVER_BOT_ID = bot_id
        return True
    except Exception:
        log.exception("canvas_pump: navigate failed bot=%s", bot_id)
        return False


@shared_task(
    name="agent.canvas.pump.push_canvas_images",
    bind=True,
    time_limit=30,
    soft_time_limit=25,
)
def push_canvas_images(self) -> dict:
    """
    Iterate over every bot in a live state, screenshot its canvas tab,
    and POST the PNG to Attendee. Called by Celery Beat every ~3s.
    """
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
    from agent.models import MeetingCursor
    from bots.models import Bot, BotStates

    playable_states = (
        BotStates.JOINED_RECORDING,
        BotStates.JOINED_NOT_RECORDING,
        BotStates.JOINED_RECORDING_PERMISSION_DENIED,
        BotStates.JOINED_RECORDING_PAUSED,
    )
    cursor_bot_ids = set(MeetingCursor.objects.values_list("bot_id", flat=True))
    if not cursor_bot_ids:
        return []
    return list(
        Bot.objects.filter(
            object_id__in=cursor_bot_ids,
            state__in=playable_states,
        ).values_list("object_id", flat=True)
    )


def push_canvas_images_for_bot(bot_id: str) -> dict:
    """Synchronously screenshot + push for a single bot."""
    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    api_base = getattr(settings, "AGENT_APP_URL", "").rstrip("/")
    if not api_key or not api_base:
        return {"error": "missing config"}
    sent, _ = _push_one(bot_id, api_base, api_key)
    return {"pushed": int(sent)}


def _capture_canvas_png(bot_id: str) -> bytes | None:
    driver = _get_driver()
    if driver is None:
        return None
    if not _navigate_if_needed(driver, bot_id):
        return None
    try:
        return driver.get_screenshot_as_png()
    except Exception:
        log.exception("canvas_pump: screenshot failed bot=%s", bot_id)
        return None


def _push_one(bot_id: str, api_base: str, api_key: str) -> tuple[bool, bool]:
    """Screenshot + POST a canvas image for one bot. Returns (sent, skipped_same)."""
    png = _capture_canvas_png(bot_id)
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
        log.info(
            "push_canvas_images: HTTP %s bot=%s — %s",
            resp.status_code, bot_id, resp.text[:160],
        )
        return False, False

    _LAST_DIGEST[bot_id] = digest
    return True, False
