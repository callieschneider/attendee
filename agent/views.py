"""
Agent app views:
- create_meeting_bot: creates an Attendee bot wired to the audio bridge
- attendee_webhook: receives Attendee bot webhooks, verifies HMAC, dispatches Celery tasks
- live_voice_token: mints an ephemeral Gemini Live token
- live_voice_tool: executes a tool call from a live session
"""
import base64
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

log = logging.getLogger("agent.views")


def _verify_webhook_signature(payload_dict: dict, signature_header: str, secret_bytes: bytes) -> bool:
    """
    Verify Attendee webhook signature.
    Attendee signs canonical JSON (sort_keys=True, no spaces) of the full webhook_data dict,
    then base64-encodes the HMAC-SHA256 result.
    Header: X-Webhook-Signature.
    See bots/webhook_utils.py:sign_payload for the exact implementation.
    """
    if not secret_bytes or not signature_header:
        return False

    # Reconstruct canonical JSON exactly as Attendee does in sign_payload()
    canonical = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    expected = base64.b64encode(
        hmac.new(secret_bytes, canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    return hmac.compare_digest(signature_header, expected)


def _get_project_secret(bot_object_id: str) -> bytes | None:
    """
    Look up the webhook secret for the project associated with this bot.
    Returns bytes or None if not found.
    """
    try:
        from bots.models import Bot
        bot = Bot.objects.select_related("project").get(object_id=bot_object_id)
        secret_obj = bot.project.webhook_secrets.order_by("-created_at").first()
        if secret_obj:
            return secret_obj.get_secret()
    except Exception:
        log.exception("_get_project_secret: failed to retrieve secret for bot %s", bot_object_id)
    return None


@csrf_exempt
@require_POST
def create_meeting_bot(request):
    """
    Create a bot pre-configured with the audio bridge WebSocket URL.
    This is the primary way to create bots that use Gemini Live.

    Request body:
    {"meeting_url": "https://meet.google.com/xxx", "series_id": "uuid?", "bot_name": "?"}
    """
    import requests as req

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed"}, status=400)

    meeting_url = body.get("meeting_url", "")
    if not meeting_url:
        return JsonResponse({"error": "meeting_url required"}, status=400)

    bridge_domain = getattr(settings, "BRIDGE_DOMAIN", "")
    if not bridge_domain:
        return JsonResponse({"error": "BRIDGE_DOMAIN not configured — deploy bridge service first"}, status=500)

    bot_name = body.get("bot_name", "Meeting Agent")
    series_id = body.get("series_id")
    api_key = getattr(settings, "ATTENDEE_API_KEY", "")
    agent_app_url = getattr(settings, "AGENT_APP_URL", "https://meeting-agent-web-production.up.railway.app")

    # websocket_settings can only be set at creation time.
    # Strategy: create a stable session_id first, use it as the WS path,
    # then create the bot with that URL embedded from the start.
    import uuid as _uuid
    session_id = str(_uuid.uuid4())
    ws_url = f"wss://{bridge_domain}/audio/{session_id}"

    # NOTE: voice_agent_settings was removed — Attendee's webpage streamer
    # requires a k8s sidecar container that doesn't exist on Railway. The
    # /agent/canvas/ endpoint still serves a live debug view you can open
    # in your own browser. Bringing the bot's video feed online requires
    # deploying a standalone webpage-streamer service; tracked separately.
    bot_payload = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "google_meet_settings": {"use_login": True},
        "websocket_settings": {
            "audio": {
                "url": ws_url,
                "sample_rate": 16000,
            }
        },
        # Always set bridge_session_id so the bridge can look up the bot by its
        # session_id path segment. See agent/bridge.py::_resolve_bot_id.
        "metadata": {"bridge_session_id": session_id},
    }
    if series_id:
        bot_payload["metadata"]["series_id"] = series_id

    try:
        resp = req.post(
            f"{agent_app_url}/api/v1/bots",
            json=bot_payload,
            headers={"Authorization": f"Token {api_key}"},
            timeout=60,
        )
        resp.raise_for_status()
        bot_data = resp.json()
    except Exception as exc:
        log.exception("create_meeting_bot: failed to create bot")
        return JsonResponse({"error": str(exc)}, status=500)

    bot_id = bot_data.get("id", "")
    log.info("create_meeting_bot: bot %s created, bridge session %s at %s", bot_id, session_id, ws_url)
    return JsonResponse({**bot_data, "bridge_url": ws_url, "bridge_session_id": session_id})


@csrf_exempt
@require_POST
def attendee_webhook(request):
    """
    Receive Attendee bot webhooks.

    Payload shape (from deliver_webhook_task.py):
    {
        "idempotency_key": "...",
        "bot_id": "bot_xxx",
        "trigger": "bot.state_change",
        "data": {
            "event_type": "...",
            "old_state": "...",
            "new_state": "ended",
            ...
        }
    }

    Signature: X-Webhook-Signature: base64(HMAC-SHA256(canonical_json, project_secret))
    """
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        log.warning("attendee_webhook: malformed JSON from %s", request.META.get("REMOTE_ADDR"))
        return JsonResponse({"error": "malformed"}, status=400)

    trigger = payload.get("trigger", "")
    bot_id = payload.get("bot_id", "")
    data = payload.get("data", {})

    sig_header = request.headers.get("X-Webhook-Signature", "")

    # Verify signature using the project's webhook secret
    # We need bot_id to look up the project
    if bot_id:
        secret_bytes = _get_project_secret(bot_id)
        if secret_bytes:
            if not _verify_webhook_signature(payload, sig_header, secret_bytes):
                log.warning(
                    "attendee_webhook: invalid signature for bot %s from %s",
                    bot_id, request.META.get("REMOTE_ADDR"),
                )
                return JsonResponse({"error": "invalid signature"}, status=401)
        else:
            # No secret configured — log but allow (bot may not have had webhook subscription configured yet)
            log.info("attendee_webhook: no webhook secret found for bot %s — skipping signature check", bot_id)
    else:
        log.warning("attendee_webhook: no bot_id in payload, skipping signature check")

    log.info("attendee_webhook: trigger=%s bot_id=%s", trigger, bot_id)

    # Dispatch by trigger type
    if trigger == "bot.state_change":
        new_state = data.get("new_state", "")
        # Fire when the bot reaches ended state (after post_processing completes)
        if new_state == "ended" and bot_id:
            from .tasks import process_finished_meeting
            process_finished_meeting.delay(bot_id=bot_id)
            return JsonResponse({"ok": True, "dispatched": "process_finished_meeting"})
        return JsonResponse({"ok": True, "ignored": f"state={new_state}"})

    elif trigger == "transcript.update":
        from .ingestion import ingest_transcript_update
        try:
            result = ingest_transcript_update(bot_id, data)
        except Exception:
            log.exception("attendee_webhook: transcript.update ingestion failed")
            return JsonResponse({"error": "ingestion failed"}, status=500)
        return JsonResponse({"ok": True, **result})

    elif trigger == "chat_messages.update":
        from .ingestion import ingest_chat_message
        try:
            result = ingest_chat_message(bot_id, data)
        except Exception:
            log.exception("attendee_webhook: chat_messages.update ingestion failed")
            return JsonResponse({"error": "ingestion failed"}, status=500)
        return JsonResponse({"ok": True, **result})

    elif trigger == "calendar.events_update":
        # Calendar sync completed — find and schedule upcoming events
        calendar_id = payload.get("calendar_id") or payload.get("id")
        if not calendar_id:
            # Calendar is in the top-level of the delivery (from deliver_webhook_task)
            # The calendar_id comes from the outer wrapper as calendar_id field
            log.info("calendar.events_update received, scheduling upcoming event check")
            from .tasks import sync_upcoming_calendar_events
            sync_upcoming_calendar_events.delay()
            return JsonResponse({"ok": True, "dispatched": "sync_upcoming_calendar_events"})
        from .series_manager import schedule_bot_for_upcoming_events
        from celery import current_app
        current_app.send_task("agent.tasks.sync_upcoming_calendar_events")
        return JsonResponse({"ok": True, "dispatched": "sync_upcoming_calendar_events"})

    elif trigger == "calendar.state_change":
        log.info("calendar.state_change received: %s", str(payload.get("data", {}))[:100])
        return JsonResponse({"ok": True, "ignored": "calendar.state_change"})

    return JsonResponse({"ok": True, "ignored": trigger})


@csrf_exempt
@require_POST
def live_voice_token(request):
    """
    Mint an ephemeral Gemini Live token for an audio streaming session.

    Request body:
    {
        "series_id": "uuid (optional)",
        "occurrence_id": "uuid (optional)",
        "voice": "Zephyr (optional)"
    }

    Returns:
    {
        "token": "auth_tokens/...",
        "client_setup_message": {...}
    }
    """
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed"}, status=400)

    from .context_builder import build_context
    from .gemini_live import build_live_setup, mint_ephemeral_token

    series_id = body.get("series_id")
    occurrence_id = body.get("occurrence_id")
    voice = body.get("voice") or getattr(settings, "AGENT_DEFAULT_VOICE", "Zephyr")

    try:
        system_prompt = build_context(series_id=series_id, occurrence_id=occurrence_id)
        setup = build_live_setup(system_prompt, voice=voice)
        minted = mint_ephemeral_token(setup)
    except Exception as exc:
        log.exception("live_voice_token: failed")
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse(minted)


@csrf_exempt
@require_POST
def live_voice_tool(request):
    """
    Execute a tool call from a Gemini Live session.

    Request body:
    {
        "name": "tool_name",
        "input": {...},
        "series_id": "uuid (optional)",
        "occurrence_id": "uuid (optional)",
        "bot_id": "bot_xxx (optional)"
    }

    Returns:
    {"result": ...}
    """
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed"}, status=400)

    from .tools import execute_tool

    tool_name = body.get("name", "")
    tool_input = body.get("input", {})
    ctx = {
        "series_id": body.get("series_id"),
        "occurrence_id": body.get("occurrence_id"),
        "bot_id": body.get("bot_id"),
    }

    if not tool_name:
        return JsonResponse({"error": "name required"}, status=400)

    result = execute_tool(tool_name, tool_input, ctx)
    return JsonResponse({"result": result})


# ── Calendar OAuth ─────────────────────────────────────────────────────────────

def calendar_connect(request):
    """
    GET /agent/api/calendar/connect
    Renders a page with the Google OAuth link for the calendar authorization flow.
    Requires admin login.
    """
    from django.contrib.admin.views.decorators import staff_member_required
    from django.shortcuts import render, redirect

    if not request.user.is_authenticated or not request.user.is_staff:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())

    from .calendar_oauth import get_oauth_url
    try:
        oauth_url = get_oauth_url(request)
    except ValueError as e:
        return JsonResponse({"error": str(e), "hint": "Set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET env vars and redeploy"}, status=500)

    # Simple HTML page with the link — no template needed
    html = f"""<!DOCTYPE html>
<html><head><title>Connect Google Calendar</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;padding:20px">
<h2>Connect Google Calendar</h2>
<p>Click the button below to authorize <strong>Meeting Agent</strong> to read
<code>meetingagent@latentspaceco.com</code>'s Google Calendar.</p>
<p><a href="{oauth_url}" style="background:#4285f4;color:#fff;padding:12px 24px;border-radius:4px;text-decoration:none;display:inline-block">
Authorize Google Calendar Access</a></p>
<p style="color:#666;font-size:14px">This grants read-only access to the calendar. No events will be modified.</p>
</body></html>"""
    from django.http import HttpResponse
    return HttpResponse(html)


def calendar_callback(request):
    """
    GET /agent/api/calendar/callback?code=...
    Handles the OAuth redirect, exchanges code for tokens, registers calendar with Attendee.
    """
    from django.shortcuts import redirect
    from .calendar_oauth import exchange_code_for_tokens, get_google_user_info, register_calendar_with_attendee

    code = request.GET.get("code")
    error = request.GET.get("error")

    if error:
        return JsonResponse({"error": f"OAuth denied: {error}"}, status=400)
    if not code:
        return JsonResponse({"error": "No code in callback"}, status=400)

    try:
        tokens = exchange_code_for_tokens(code, request)
    except Exception as exc:
        log.exception("calendar_callback: token exchange failed")
        return JsonResponse({"error": f"Token exchange failed: {exc}"}, status=500)

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        return JsonResponse({
            "error": "No refresh_token returned. Try visiting /agent/api/calendar/connect again."
        }, status=400)

    # Get account info for confirmation
    try:
        user_info = get_google_user_info(access_token)
    except Exception:
        user_info = {}

    # Register calendar with Attendee
    client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")

    try:
        calendar_data = register_calendar_with_attendee(client_id, client_secret, refresh_token)
        log.info("calendar_callback: registered calendar %s for %s",
                 calendar_data.get("id"), user_info.get("email"))
    except Exception as exc:
        log.exception("calendar_callback: failed to register calendar with Attendee")
        # Store tokens in env/log for manual recovery
        log.info("calendar_callback: refresh_token starts with %s", refresh_token[:8])
        return JsonResponse({"error": f"Calendar registration failed: {exc}", "refresh_token_prefix": refresh_token[:8]}, status=500)

    # Trigger initial sync of upcoming events
    from .series_manager import schedule_bot_for_upcoming_events
    calendar_object_id = calendar_data.get("id", "")
    if calendar_object_id:
        import threading
        threading.Thread(
            target=lambda: schedule_bot_for_upcoming_events(calendar_object_id),
            daemon=True,
        ).start()

    from django.http import HttpResponse
    email = user_info.get("email", "unknown")
    html = f"""<!DOCTYPE html>
<html><head><title>Calendar Connected</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;padding:20px">
<h2>✅ Google Calendar Connected</h2>
<p>Successfully connected calendar for <strong>{email}</strong>.</p>
<p>Calendar ID: <code>{calendar_object_id}</code></p>
<p>Attendee will sync upcoming events and automatically schedule bots for Google Meet calls.</p>
<p><a href="/admin/">← Back to admin</a></p>
</body></html>"""
    return HttpResponse(html)


# ── Test harness: utterance injection ─────────────────────────────────────────
@csrf_exempt
@require_POST
def inject_utterance(request):
    """
    Test-harness endpoint. Pushes a synthetic user utterance into a live
    Gemini session so the harness can verify tool calls and canvas
    deltas without going through the audio path.

    Auth: header `X-Debug-Token` must equal env `AGENT_DEBUG_TOKEN`. Endpoint
    does nothing useful unless that env var is set, so prod is opt-in.

    Body: {"bot_id": "...", "text": "...", "speaker": "Tester" (optional)}
    """
    debug_token = getattr(settings, "AGENT_DEBUG_TOKEN", "") or ""
    if not debug_token:
        return JsonResponse({"error": "debug endpoints disabled"}, status=403)
    sent_token = request.headers.get("X-Debug-Token", "")
    if not hmac.compare_digest(sent_token, debug_token):
        return JsonResponse({"error": "bad token"}, status=403)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed json"}, status=400)

    bot_id = (body.get("bot_id") or "").strip()
    text = (body.get("text") or "").strip()
    speaker = (body.get("speaker") or "Tester").strip()
    if not bot_id or not text:
        return JsonResponse({"error": "bot_id and text required"}, status=400)

    from .live_session import signals as _sig

    ok = _sig.publish_inject_utterance(bot_id, text, speaker=speaker)
    return JsonResponse({"ok": ok, "bot_id": bot_id, "text": text, "speaker": speaker})
