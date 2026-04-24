"""
Agent app views:
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
        # Phase 2 — realtime transcript forwarding to Gemini Live
        # For Phase 1 we just acknowledge
        return JsonResponse({"ok": True, "ignored": "transcript.update (Phase 2)"})

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
