"""
Diagnostics aggregator — gives the agent (and humans) one place to see
what's working and what's broken.

Pulls together:
- Recent failed tool calls (last 10 ActionLogEntry rows with status=error)
- Recent successful tool calls (last 10 ok ones — useful for "did X
  actually run?")
- Browser session state (alive / dead / which URL / last error)
- Voice / audio gate state (open/closed/suspended)
- Recent system transcript events (bot joined, gate change, etc.)

Returns a compact dict the `get_diagnostics` tool can hand back to
Gemini Live so it can troubleshoot in-call.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("agent.diagnostics")


_RECENT_LIMIT = 10
_TEXT_LIMIT = 600


def collect(bot_id: str, *, scope: str = "all") -> dict:
    """Build a diagnostics snapshot. `scope` filters to one section."""
    if not bot_id:
        return {"error": "bot_id required"}
    out: dict[str, Any] = {"bot_id": bot_id, "scope": scope}
    try:
        if scope in ("all", "tools"):
            out["tools"] = _tool_section(bot_id)
        if scope in ("all", "browser"):
            out["browser"] = _browser_section(bot_id)
        if scope in ("all", "session", "voice"):
            out["session"] = _session_section(bot_id)
        if scope in ("all", "events"):
            out["events"] = _events_section(bot_id)
    except Exception as exc:
        log.exception("diagnostics: collect failed bot=%s", bot_id)
        out["collect_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _tool_section(bot_id: str) -> dict:
    from agent.models import ActionLogEntry

    failures = list(
        ActionLogEntry.objects.filter(bot_id=bot_id, status="error")
        .order_by("-created_at")[:_RECENT_LIMIT]
    )
    successes = list(
        ActionLogEntry.objects.filter(bot_id=bot_id, status="ok")
        .order_by("-created_at")[:_RECENT_LIMIT]
    )

    def _row(a):
        return {
            "t": a.created_at.isoformat(),
            "tool": a.tool_name,
            "status": a.status,
            "latency_ms": a.latency_ms,
            "error": (a.error_message or "")[:_TEXT_LIMIT],
            "input_summary": _summarize(a.tool_input),
        }

    return {
        "recent_failures": [_row(a) for a in failures],
        "recent_successes": [_row(a) for a in successes],
        "failure_count_lifetime": ActionLogEntry.objects.filter(
            bot_id=bot_id, status="error"
        ).count(),
    }


def _browser_section(bot_id: str) -> dict:
    """Inspect the per-bot BrowserSession if any."""
    try:
        from agent import browser_session as _bs
        bs = _bs.get(bot_id)
    except Exception:
        log.exception("diagnostics: browser_session import failed")
        return {"error": "browser_session module unavailable"}
    if bs is None:
        return {"active": False, "note": "no headless Chrome started for this bot yet"}
    return {
        "active": True,
        "closed": bs._closed,
        "has_driver": bs._driver is not None,
        "screencast_running": bool(bs._screencast_task and not bs._screencast_task.done()),
    }


def _session_section(bot_id: str) -> dict:
    """Voice gate, suspension, resumption handle freshness."""
    try:
        from agent.live_session import signals as _sig
    except Exception:
        return {"error": "signals unavailable"}
    return {
        "gate_open": _sig.is_gate_open(bot_id),
        "voice_suspended": _sig.is_voice_suspended(bot_id),
    }


def _events_section(bot_id: str) -> dict:
    """Last few system events from the transcript timeline."""
    try:
        from agent.models import TranscriptEvent
    except Exception:
        return {"error": "models unavailable"}
    events = list(
        TranscriptEvent.objects.filter(bot_id=bot_id, kind__in=["system", "action"])
        .order_by("-event_time")[:_RECENT_LIMIT]
    )
    events.reverse()
    return {
        "recent": [
            {
                "t": (e.event_time.isoformat() if e.event_time else None),
                "kind": e.kind,
                "speaker": e.speaker or "",
                "text": (e.text or "")[:_TEXT_LIMIT],
            }
            for e in events
        ]
    }


def _summarize(payload: Any) -> str:
    if payload is None:
        return ""
    try:
        import json as _json
        s = _json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        s = str(payload)
    return s[:_TEXT_LIMIT]
