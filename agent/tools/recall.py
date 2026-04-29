"""
Recall, bookmarks, and browser history tools.

Implements:
  - bookmark_url       — save a URL with a label (series-scoped by default).
  - list_bookmarks     — list bookmarks for the current series.
  - delete_bookmark    — remove a bookmark by id or URL.
  - browser_history    — list recent URLs the bot navigated to.
  - search_transcripts — full-text search across transcripts in the series.

All tools auto-derive series_id from the active bot via
resolve_series_id_for_bot(). Series-scoped data lets recall span
every meeting in the same recurring series, so the agent can pull
up "what we covered last week" without asking.
"""
from __future__ import annotations

import logging
from typing import Optional

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.recall")


def _series_id(ctx: dict, inp: dict) -> Optional[str]:
    """Resolve series_id from ctx, then bot_id, then inp.series_id."""
    sid = ctx.get("series_id") or inp.get("series_id")
    if sid:
        return str(sid)
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    if not bot_id:
        return None
    from agent.context_engine.layers import resolve_series_id_for_bot
    return resolve_series_id_for_bot(bot_id)


# ── Bookmarks ─────────────────────────────────────────────────────────────────


def _bookmark_url(inp: dict, ctx: dict) -> dict:
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    url = (inp.get("url") or "").strip()
    label = (inp.get("label") or "").strip()
    notes = (inp.get("notes") or "").strip()
    tags = inp.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    if not url:
        return {"error": "url required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    if not label:
        return {"error": "label required (short human-readable name)"}
    sid = _series_id(ctx, inp)

    from agent.models import Bookmark, MeetingSeries
    from bots.models import Bot

    series = None
    if sid:
        try:
            series = MeetingSeries.objects.filter(pk=sid).first()
        except Exception:
            series = None
    bot_obj = None
    if bot_id:
        bot_obj = Bot.objects.filter(object_id=bot_id).only("object_id").first()

    bm, created = Bookmark.objects.update_or_create(
        series=series,
        url=url,
        defaults={
            "label": label[:255],
            "notes": notes[:4000],
            "tags": [str(t)[:64] for t in tags][:16],
            "created_by_bot": bot_obj,
        },
    )
    return {
        "ok": True,
        "id": str(bm.id),
        "created": created,
        "url": bm.url,
        "label": bm.label,
        "series_id": str(series.id) if series else None,
    }


def _list_bookmarks(inp: dict, ctx: dict) -> dict:
    sid = _series_id(ctx, inp)
    query = (inp.get("query") or "").strip().lower()
    limit = max(1, min(int(inp.get("limit") or 50), 200))

    from agent.models import Bookmark
    from django.db.models import Q

    qs = Bookmark.objects.all()
    if sid:
        # Show series-scoped + globals (series IS NULL).
        qs = qs.filter(Q(series_id=sid) | Q(series__isnull=True))
    if query:
        qs = qs.filter(
            Q(label__icontains=query)
            | Q(notes__icontains=query)
            | Q(url__icontains=query)
            | Q(tags__contains=[query])
        )
    qs = qs.order_by("-updated_at")[:limit]
    items = [
        {
            "id": str(b.id),
            "url": b.url,
            "label": b.label,
            "notes": (b.notes or "")[:280],
            "tags": list(b.tags or []),
            "scope": "series" if b.series_id else "global",
            "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        }
        for b in qs
    ]
    return {"ok": True, "count": len(items), "bookmarks": items}


def _delete_bookmark(inp: dict, ctx: dict) -> dict:
    bm_id = (inp.get("id") or "").strip()
    url = (inp.get("url") or "").strip()
    if not bm_id and not url:
        return {"error": "id or url required"}
    sid = _series_id(ctx, inp)

    from agent.models import Bookmark
    from django.db.models import Q

    qs = Bookmark.objects.all()
    if sid:
        qs = qs.filter(Q(series_id=sid) | Q(series__isnull=True))
    if bm_id:
        qs = qs.filter(pk=bm_id)
    if url:
        qs = qs.filter(url=url)
    deleted, _ = qs.delete()
    return {"ok": True, "deleted": deleted}


# ── Browser history ───────────────────────────────────────────────────────────


def _browser_history(inp: dict, ctx: dict) -> dict:
    sid = _series_id(ctx, inp)
    bot_id = ctx.get("bot_id") or inp.get("bot_id")
    scope = (inp.get("scope") or "series").lower()  # 'series' | 'bot'
    query = (inp.get("query") or "").strip().lower()
    limit = max(1, min(int(inp.get("limit") or 50), 200))

    from agent.models import BrowserPageVisit
    from django.db.models import Q

    qs = BrowserPageVisit.objects.all()
    if scope == "bot" and bot_id:
        qs = qs.filter(bot_id=bot_id)
    elif sid:
        qs = qs.filter(series_id=sid)
    elif bot_id:
        qs = qs.filter(bot_id=bot_id)
    if query:
        qs = qs.filter(Q(url__icontains=query) | Q(title__icontains=query))
    qs = qs.order_by("-created_at")[:limit]
    items = [
        {
            "url": v.url,
            "title": v.title,
            "source": v.source,
            "visited_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in qs
    ]
    return {"ok": True, "count": len(items), "scope": scope, "visits": items}


# ── Series-wide transcript search ─────────────────────────────────────────────


def _search_transcripts(inp: dict, ctx: dict) -> dict:
    """
    Full-text-ish search across every TranscriptEvent in the series.
    Returns matching utterances with surrounding context so the agent
    can recall what was said in earlier meetings.
    """
    sid = _series_id(ctx, inp)
    if not sid:
        return {"error": "no series_id resolvable for this bot"}
    query = (inp.get("query") or "").strip()
    if not query:
        return {"error": "query required"}
    limit = max(1, min(int(inp.get("limit") or 20), 100))

    from agent.models import MeetingOccurrence, TranscriptEvent
    from django.db.models import Q

    occ_ids = list(
        MeetingOccurrence.objects.filter(series_id=sid).values_list("id", flat=True)
    )
    qs = TranscriptEvent.objects.filter(occurrence_id__in=occ_ids, kind="speech")
    qs = qs.filter(Q(text__icontains=query))
    qs = qs.select_related("occurrence").order_by("-event_time")[:limit]
    matches = []
    for ev in qs:
        occ = ev.occurrence
        matches.append({
            "occurrence_id": str(occ.id) if occ else None,
            "occurrence_title": (occ.title if occ else "") or "",
            "occurrence_date": (
                occ.started_at.isoformat() if occ and occ.started_at else None
            ),
            "speaker": ev.speaker or "",
            "text": (ev.text or "")[:600],
            "event_time": ev.event_time.isoformat() if ev.event_time else None,
        })
    return {
        "ok": True,
        "series_id": sid,
        "query": query,
        "match_count": len(matches),
        "matches": matches,
    }


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="bookmark_url",
        description=(
            "Save a URL as a bookmark so it can be quickly opened in any "
            "meeting in this series. Use when the user says 'bookmark "
            "this', 'save that link', 'remember this URL', or whenever "
            "you've found a page worth keeping. Bookmarks are scoped to "
            "the meeting series by default — they show up across every "
            "meeting in the same recurring series. Returns {ok, id, url, "
            "label}. Idempotent: bookmarking the same URL again just "
            "updates the label/notes/tags."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "url": {
                    "type": "string",
                    "description": "Full URL to save.",
                },
                "label": {
                    "type": "string",
                    "description": "Short human label, e.g. 'Q3 OKR draft'.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional longer note about why you saved it.",
                },
                "tags": {
                    "type": "array",
                    "description": "Optional short tag strings.",
                    "items": {"type": "string"},
                },
            },
            required=["url", "label"],
        ),
        handler=_bookmark_url,
    ),
    ToolDefinition(
        name="list_bookmarks",
        description=(
            "List bookmarks for the current series. Pass a query string "
            "to filter by label/notes/url/tag. Use when the user says "
            "'pull up our bookmarks', 'what links did we save', 'open "
            "the Q3 OKR doc'. Returns {ok, count, bookmarks}. After "
            "finding the right bookmark, call open_url or page_navigate "
            "with its url."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "query": {
                    "type": "string",
                    "description": "Substring filter (case-insensitive). Optional.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50, max 200).",
                },
            },
            required=[],
        ),
        handler=_list_bookmarks,
    ),
    ToolDefinition(
        name="delete_bookmark",
        description=(
            "Remove a bookmark by id (preferred) or by exact URL. Use "
            "when the user says 'remove that bookmark' or 'forget the X "
            "link'. Returns {ok, deleted}."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "id": {
                    "type": "string",
                    "description": "Bookmark UUID from list_bookmarks.",
                },
                "url": {
                    "type": "string",
                    "description": "Exact URL to remove (alternative to id).",
                },
            },
            required=[],
        ),
        handler=_delete_bookmark,
    ),
    ToolDefinition(
        name="browser_history",
        description=(
            "List URLs the bot has navigated to recently, scoped by "
            "default to the current meeting series. Use when the user "
            "says 'go back to that page we looked at', 'what site did "
            "we visit earlier', 'pull up that thing from last meeting'. "
            "scope='bot' restricts to just this current meeting; "
            "default 'series' spans every meeting in the series."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "query": {
                    "type": "string",
                    "description": "Substring filter on URL or title. Optional.",
                },
                "scope": {
                    "type": "string",
                    "description": "'series' (default) or 'bot' (current meeting only).",
                    "enum": ["series", "bot"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50, max 200).",
                },
            },
            required=[],
        ),
        handler=_browser_history,
    ),
    ToolDefinition(
        name="search_transcripts",
        description=(
            "Full-text search across every TranscriptEvent in the "
            "current meeting series. Use when the user asks 'what did "
            "we say about X last time', 'when did we discuss Y', 'find "
            "the part where Greg mentioned Z'. Returns matching speech "
            "utterances with speaker, occurrence (which past meeting), "
            "and timestamp. Only searches speech (not chat or system "
            "events). Does NOT search the current meeting's live "
            "transcript — use get_canvas_content for that."
        ),
        input_schema=ToolSchema(
            type="object",
            properties={
                "query": {
                    "type": "string",
                    "description": "Substring to search for (case-insensitive).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return (default 20, max 100).",
                },
            },
            required=["query"],
        ),
        handler=_search_transcripts,
    ),
]
