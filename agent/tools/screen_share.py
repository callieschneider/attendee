"""
Phase 4: screen_share_canvas tool.

Lets Gemini Live tell the bot to start (or stop) presenting its canvas tab
in Google Meet via getDisplayMedia + Chrome's
--auto-select-desktop-capture-source flag.

Wire:
  agent/tools/screen_share.py
    -> bots.bots_api_utils.send_sync_command(bot, command="start_canvas_screenshare")
        -> redis pubsub channel "bot_{bot.id}"
            -> bots.bot_controller.bot_controller.handle_redis_message
                -> _toggle_canvas_screenshare(on=True)
                    -> Selenium JS click into Meet's "Share screen" button.

The auto-select flag is set in web_bot_adapter.init_driver to match the
canvas page's <title>. So the screen-source dialog is invisible: Chrome
silently picks the canvas tab and pipes it through Meet's Present mode.
"""
from __future__ import annotations

import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.screen_share")


def _screen_share_canvas(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return {"error": "bot_id required (must run inside a live meeting)"}

    action = (inp.get("action") or "start").strip().lower()
    if action not in ("start", "stop"):
        return {"error": "action must be 'start' or 'stop'"}

    try:
        from bots.models import Bot
        from bots.bots_api_utils import send_sync_command
    except Exception as exc:
        log.exception("screen_share_canvas: import failed")
        return {"error": f"server import error: {exc}"}

    try:
        bot = Bot.objects.filter(object_id=bot_id).first()
    except Exception as exc:
        log.exception("screen_share_canvas: db lookup failed")
        return {"error": f"db error: {exc}"}
    if bot is None:
        return {"error": f"bot not found: {bot_id}"}

    command = "start_canvas_screenshare" if action == "start" else "stop_canvas_screenshare"
    try:
        send_sync_command(bot, command=command)
    except Exception as exc:
        log.exception("screen_share_canvas: send_sync_command failed")
        return {"error": f"redis publish error: {exc}"}

    log.info("screen_share_canvas: dispatched %s for bot=%s", command, bot_id)
    return {
        "ok": True,
        "action": action,
        "bot_id": bot_id,
        "note": (
            "Sent. The bot will click Meet's Share-screen button and "
            "auto-select its canvas tab. Tell the user to look for the "
            "shared screen in Meet's main view."
        ),
    }


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="screen_share_canvas",
        description=(
            "Start (or stop) sharing the canvas web app to the meeting via "
            "Google Meet's screen-share. The shared image is pixel-perfect "
            "WebRTC, much higher quality than your video tile. Use when the "
            "user says 'screen share', 'present that', 'put it on the big "
            "screen', 'stop sharing', etc. Tell the user 'sharing now' "
            "before calling. Action is 'start' by default."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "action": {
                    "type": "string",
                    "description": "'start' begins the share; 'stop' ends it.",
                    "enum": ["start", "stop"],
                },
            },
            required=[],
        ),
        handler=_screen_share_canvas,
    ),
]
