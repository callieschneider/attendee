"""
Auto-respawn supervisor — bring a bot back when it dies unintentionally.

Hooked into the Attendee bot.state_change webhook. When a bot reaches
the `ended` state, this checks:

  - Did the agent's leave_meeting / respawn_bot tool run? (Redis flag
    set by agent.tools.bot_lifecycle.) If yes, the exit was
    intentional — do nothing.
  - Did the bot ever reach JOINED_RECORDING? If never, the join failed
    cleanly (auth issue, host removed before admit, etc.) and we
    don't auto-retry — likely the user will spawn a new bot manually
    after fixing the cause.
  - Have we already auto-respawned this meeting recently? Cap at 2
    auto-respawns per meeting URL within a 10-minute window so a
    cascade of failures doesn't make us spam.

If all checks pass, we replicate the same flow as the respawn_bot
tool: spawn a fresh bot, copy CanvasState, publish a `moved` event
on the dead bot's canvas channel so any open browser tab follows.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("agent.bot_supervisor")


_AUTO_RESPAWN_CAP = 2          # max auto-spawns per meeting URL per window
_AUTO_RESPAWN_WINDOW_S = 600   # 10 minutes


def _redis():
    try:
        import redis
    except Exception:
        return None
    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    try:
        return redis.from_url(url, decode_responses=True)
    except Exception:
        return None


def _meet_key(meeting_url: str) -> str:
    """Redis key for tracking auto-respawns per meeting URL."""
    import hashlib
    h = hashlib.sha256((meeting_url or "").encode()).hexdigest()[:16]
    return f"agent:bot:auto_respawn:{h}"


def _bump_respawn_counter(meeting_url: str) -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        key = _meet_key(meeting_url)
        n = r.incr(key)
        # Set TTL on first increment.
        if n == 1:
            r.expire(key, _AUTO_RESPAWN_WINDOW_S)
        return int(n)
    except Exception:
        log.exception("bot_supervisor: redis incr failed for %s", meeting_url)
        return 0


def _read_respawn_counter(meeting_url: str) -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        v = r.get(_meet_key(meeting_url))
        return int(v) if v else 0
    except Exception:
        return 0


def _bot_ever_recorded(bot_id: str) -> bool:
    """True if this bot ever reached JOINED_RECORDING (state 4)."""
    try:
        from bots.models import BotEvent
        return BotEvent.objects.filter(
            bot__object_id=bot_id,
            event_type=3,  # BOT_RECORDING_PERMISSION_GRANTED
        ).exists()
    except Exception:
        log.exception("bot_supervisor: BotEvent lookup failed bot=%s", bot_id)
        return False


def _bot_was_removed_by_user(bot_id: str) -> bool:
    """
    True if the bot's exit was caused by a user/host action rather
    than an internal failure. Suppresses auto-respawn for:

      - MEETING_ENDED (type 4): the host ended the meeting OR
        kicked the bot. Either way, "the meeting is over for us"
        and respawning is rude.
      - LEAVE_REQUESTED (type 8): an explicit leave request was
        issued — by the agent's own leave_meeting tool, by the
        Attendee API, or by an automatic-leave rule. Re-spawning
        in any of those cases would directly contradict the
        request.
    """
    try:
        from bots.models import BotEvent
        return BotEvent.objects.filter(
            bot__object_id=bot_id,
            event_type__in=[4, 8],  # MEETING_ENDED, LEAVE_REQUESTED
        ).exists()
    except Exception:
        log.exception("bot_supervisor: removed-by-user lookup failed bot=%s", bot_id)
        return False


def maybe_auto_respawn(bot_id: str) -> dict:
    """
    Called when a bot reaches `ended` state. Returns a small status
    dict for logging — never raises. Performs the respawn synchronously
    if all gates pass, since this runs from the webhook handler which
    is already async-friendly via Celery.
    """
    if not bot_id:
        return {"action": "skip", "reason": "no bot_id"}
    try:
        from agent.tools.bot_lifecycle import is_intentional_leave
        if is_intentional_leave(bot_id):
            return {"action": "skip", "reason": "intentional leave"}
    except Exception:
        log.exception("bot_supervisor: is_intentional_leave failed bot=%s", bot_id)

    # User/host action — meeting ended, bot kicked, or explicit
    # leave-requested. Don't respawn against the user's wishes.
    if _bot_was_removed_by_user(bot_id):
        return {"action": "skip", "reason": "user/host removed bot"}

    # Did the bot ever actually start recording? If never, the join
    # itself failed and respawning will likely fail the same way.
    if not _bot_ever_recorded(bot_id):
        return {"action": "skip", "reason": "never reached JOINED_RECORDING"}

    # Look up the meeting URL and previous bot's state.
    try:
        from bots.models import Bot
        bot = Bot.objects.filter(object_id=bot_id).first()
    except Exception:
        log.exception("bot_supervisor: Bot lookup failed bot=%s", bot_id)
        return {"action": "skip", "reason": "DB lookup failed"}

    if bot is None:
        return {"action": "skip", "reason": "bot row missing"}

    meeting_url = bot.meeting_url
    if not meeting_url:
        return {"action": "skip", "reason": "no meeting_url"}

    # Respect the per-meeting auto-respawn cap.
    so_far = _read_respawn_counter(meeting_url)
    if so_far >= _AUTO_RESPAWN_CAP:
        return {
            "action": "skip",
            "reason": f"auto-respawn cap reached ({so_far}/{_AUTO_RESPAWN_CAP})",
        }

    # Trigger the respawn flow. This is the same logic as the
    # respawn_bot tool, but invoked without a Gemini context — we
    # synthesize the dispatch directly.
    log.info(
        "bot_supervisor: auto-respawning bot=%s meeting=%s (count %d/%d)",
        bot_id, meeting_url, so_far + 1, _AUTO_RESPAWN_CAP,
    )
    _bump_respawn_counter(meeting_url)
    result = _do_respawn(bot_id, meeting_url)
    return {"action": "respawn", **result}


def _do_respawn(old_bot_id: str, meeting_url: str) -> dict:
    """Replicates respawn_bot tool's flow without a tool ctx."""
    import requests as req
    from django.conf import settings

    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    app_url = (getattr(settings, "AGENT_APP_URL", "") or "").rstrip("/")
    if not api_key or not app_url:
        return {"ok": False, "error": "ATTENDEE_API_KEY / AGENT_APP_URL not configured"}

    bot_name = getattr(settings, "AGENT_NAME", "Clever Star")
    try:
        spawn_resp = req.post(
            f"{app_url}/agent/api/create-meeting-bot",
            json={"meeting_url": meeting_url, "bot_name": bot_name},
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "error": f"spawn failed: {exc}"}
    if spawn_resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"spawn HTTP {spawn_resp.status_code}: {spawn_resp.text[:200]}",
        }
    new = spawn_resp.json()
    new_bot_id = new.get("id")
    if not new_bot_id:
        return {"ok": False, "error": "spawn response had no id"}

    # Copy CanvasState across.
    try:
        from django.db import transaction
        from agent.models import CanvasState
        with transaction.atomic():
            old_state = CanvasState.objects.filter(bot_id=old_bot_id).first()
            new_state, _ = CanvasState.objects.get_or_create(bot_id=new_bot_id)
            if old_state:
                new_state.active_tab = old_state.active_tab
                new_state.notes_md = old_state.notes_md
                new_state.focus_session_id = old_state.focus_session_id
                new_state.focus_text = old_state.focus_text
                new_state.focus_done = old_state.focus_done
                new_state.dashboard_payload = old_state.dashboard_payload
                new_state.browser_url = old_state.browser_url
                new_state.browser_title = old_state.browser_title
                new_state.save()
    except Exception:
        log.exception(
            "bot_supervisor: canvas-state copy failed old=%s new=%s",
            old_bot_id, new_bot_id,
        )

    # Tell any open canvas tab on the OLD URL to migrate.
    try:
        from agent.canvas_v2 import state as _cstate
        new_canvas_url = f"{app_url}/agent/canvas/v2/{new_bot_id}/"
        _cstate.publish_state_event(old_bot_id, {
            "event": "moved",
            "new_bot_id": new_bot_id,
            "new_url": new_canvas_url,
            "reason": "auto_respawn",
        })
    except Exception:
        log.exception("bot_supervisor: moved-event publish failed old=%s", old_bot_id)

    return {"ok": True, "old_bot_id": old_bot_id, "new_bot_id": new_bot_id}
