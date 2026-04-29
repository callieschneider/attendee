"""
HTTP endpoints for canvas_v2.

- canvas_shell           — HTML page with the React app (single-file, no build)
- canvas_state_json      — JSON snapshot, used on page load + SSE backfill
- canvas_stream          — Server-Sent Events stream of state deltas
- canvas_navigate        — POST endpoint for user-driven navigation (Phase 4)

The SSE consumer subscribes to two Redis pubsub channels:
  - canvas:state:{bot_id}            (state-change notifications)
  - canvas:stream:{bot_id}:{tab}     (think_deep streaming chunks for any tab)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator

from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
    HttpResponseNotFound,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .state import (
    VALID_TABS,
    navigate,
    set_user_driving,
    snapshot,
)


log = logging.getLogger("agent.canvas_v2.views")


def canvas_shell(request: HttpRequest, bot_id: str) -> HttpResponse:
    """Serve the canvas web app. Single-file React + Tailwind via CDN."""
    return render(request, "agent/canvas_v2.html", {"bot_id": bot_id})


def canvas_by_meeting(request: HttpRequest, meet_code: str):
    """
    Stable per-meeting URL — redirect to whichever bot is currently
    active for that meeting code. Users keep this URL bookmarked /
    open; respawns don't break it.

    `meet_code` is the path component after meet.google.com/, e.g.
    'krf-poen-qne'.
    """
    from django.shortcuts import redirect
    from bots.models import Bot

    code = (meet_code or "").strip().lower()
    if not code:
        return HttpResponseNotFound("missing meet code")

    bot_id = _resolve_active_bot_for_meet_code(code)
    if not bot_id:
        return HttpResponseNotFound(
            f"no active bot for meet code {code!r}"
        )
    return redirect(f"/agent/canvas/v2/{bot_id}/", permanent=False)


def _resolve_active_bot_for_meet_code(meet_code: str) -> str | None:
    """Find the most recently-active bot whose meeting_url contains the code."""
    from bots.models import Bot

    # Active states: anything before LEFT/ENDED. We sort by updated_at
    # so a freshly-respawned bot wins over a stale one.
    bots = (
        Bot.objects.filter(
            meeting_url__contains=meet_code,
        )
        .order_by("-updated_at")
        .values_list("object_id", "state")[:5]
    )
    # Prefer JOINED_RECORDING / JOINING / etc. over ENDED if any.
    for object_id, state in bots:
        # 4 = JOINED_RECORDING, 2/3 = JOINING/ADMITTED, 5 = JOINED_NOT_RECORDING
        if state in (2, 3, 4, 5, 6):
            return object_id
    # Fall back to the most recent bot regardless of state — better to
    # show a stale canvas than 404 on a meeting the user is mid-test.
    if bots:
        return bots[0][0]
    return None


def canvas_state_json(request: HttpRequest, bot_id: str) -> HttpResponse:
    return JsonResponse(snapshot(bot_id))


def _redis_for_pubsub():
    try:
        import redis
    except Exception:
        return None
    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    return redis.from_url(url, decode_responses=True)


def _sse_stream(bot_id: str) -> Iterator[bytes]:
    yield b": connected\n\n"

    try:
        first = snapshot(bot_id)
        yield f"event: snapshot\ndata: {json.dumps(first)}\n\n".encode("utf-8")
    except Exception:
        log.exception("canvas_v2: initial snapshot failed bot=%s", bot_id)
        yield b": initial snapshot failed\n\n"

    r = _redis_for_pubsub()
    if r is None:
        # No Redis — just keep the stream open with heartbeats so the
        # client doesn't reconnect-storm in dev.
        while True:
            time.sleep(15)
            yield b": heartbeat\n\n"

    pubsub = r.pubsub(ignore_subscribe_messages=True)
    state_channel = f"canvas:state:{bot_id}"
    stream_pattern = f"canvas:stream:{bot_id}:*"
    browser_channel = f"canvas:browser:{bot_id}"
    try:
        pubsub.subscribe(state_channel, browser_channel)
        pubsub.psubscribe(stream_pattern)
    except Exception:
        log.exception("canvas_v2: pubsub subscribe failed bot=%s", bot_id)
        yield b": pubsub subscribe failed\n\n"
        return

    last_heartbeat = time.time()
    try:
        while True:
            msg = pubsub.get_message(timeout=5.0)
            now = time.time()
            if msg is None:
                if (now - last_heartbeat) > 15.0:
                    yield b": hb\n\n"
                    last_heartbeat = now
                continue
            mtype = msg.get("type")
            if mtype not in ("message", "pmessage"):
                continue
            channel = msg.get("channel") or ""
            data = msg.get("data") or "{}"
            if mtype == "pmessage":
                # canvas:stream:{bot_id}:{tab}
                try:
                    tab = channel.rsplit(":", 1)[-1]
                except Exception:
                    tab = ""
                payload = {"channel": "stream", "tab": tab, "data": _safe_json(data)}
                yield f"event: stream\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            elif channel == browser_channel:
                # Phase 2 browser screencast: forward as a dedicated event
                # so the Browser tab can render the latest PNG.
                payload = _safe_json(data)
                yield f"event: browser\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            else:
                payload = {"channel": "state", "data": _safe_json(data)}
                yield f"event: state\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            last_heartbeat = now
    except GeneratorExit:
        pass
    except Exception:
        log.exception("canvas_v2: SSE loop crashed bot=%s", bot_id)
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


def _safe_json(s):
    if isinstance(s, (dict, list)):
        return s
    if not isinstance(s, str):
        return {"raw": str(s)}
    try:
        return json.loads(s)
    except Exception:
        return {"raw": s}


def canvas_stream(request: HttpRequest, bot_id: str) -> HttpResponse:
    response = StreamingHttpResponse(
        _sse_stream(bot_id),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    return response


@csrf_exempt
@require_POST
def canvas_navigate(request: HttpRequest, bot_id: str) -> HttpResponse:
    """User-driven tab switch from the browser. (Phase 4 entry point.)"""
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponseBadRequest("invalid json")
    tab = (body.get("tab") or "").strip()
    if tab not in VALID_TABS:
        return HttpResponseBadRequest(f"invalid tab; expected one of {VALID_TABS}")
    result = navigate(bot_id, tab, source="user")
    if result.get("error"):
        return HttpResponseBadRequest(result["error"])
    return JsonResponse(result)


@csrf_exempt
@require_POST
def canvas_user_role(request: HttpRequest, bot_id: str) -> HttpResponse:
    """Mark a user as driving / leaving. Phase 4 hook."""
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponseBadRequest("invalid json")
    driving = bool(body.get("driving"))
    set_user_driving(bot_id, driving)
    return JsonResponse({"ok": True, "driving": driving})
