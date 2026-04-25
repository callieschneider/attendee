"""
Phase 4 utility tools:
- web_search: search the web via DuckDuckGo (no API key required)
- fetch_url: fetch and summarize a webpage
- save_artifact_from_url: fetch a URL and save it as an Artifact
- promote_meeting_task: promote a MeetingTask to a full Task
- send_email_summary: send a post-meeting email summary via Gmail (requires OAuth)
"""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.utility")


# ── Web search via DuckDuckGo ─────────────────────────────────────────────────

def _web_search(inp: dict, ctx: dict) -> dict:
    """Search the web via Firecrawl /v1/search."""
    import requests
    from django.conf import settings

    query = inp.get("query", "").strip()
    limit = min(int(inp.get("limit", 5)), 10)

    if not query:
        return {"error": "query required"}

    api_key = getattr(settings, "FIRECRAWL_API_KEY", "") or ""
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY not configured"}

    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "limit": limit},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.HTTPError as exc:
        log.warning("web_search: firecrawl HTTP %s — %s", exc.response.status_code, exc.response.text[:200])
        return {"error": f"Search failed: HTTP {exc.response.status_code}"}
    except Exception as exc:
        log.exception("web_search: firecrawl call failed")
        return {"error": f"Search failed: {exc}"}

    raw = body.get("data") or []
    # Firecrawl's /v1/search returns a list with url, title, description
    results = []
    for r in raw[:limit]:
        results.append({
            "title": r.get("title", "") or r.get("metadata", {}).get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("description", "") or r.get("content", ""))[:300],
        })
    return {"results": results, "count": len(results), "query": query}


def _fetch_url(inp: dict, ctx: dict) -> dict:
    """Fetch a URL via Firecrawl /v1/scrape, with plain-requests fallback."""
    import re
    import requests
    from django.conf import settings

    url = inp.get("url", "").strip()
    if not url:
        return {"error": "url required"}

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    api_key = getattr(settings, "FIRECRAWL_API_KEY", "") or ""
    if api_key:
        try:
            fcr = requests.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                timeout=30,
            )
            fcr.raise_for_status()
            body = fcr.json()
            data = body.get("data") or {}
            text = (data.get("markdown") or "").strip()
            if text:
                return {
                    "url": url,
                    "title": (data.get("metadata") or {}).get("title", ""),
                    "content": text[:8000],
                    "truncated": len(text) > 8000,
                }
        except Exception:
            log.exception("fetch_url: firecrawl call failed; falling back to plain GET url=%s", url)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingAgent/1.0)"},
            timeout=10,
            allow_redirects=True,
        )
        resp.raise_for_status()

        # Strip HTML tags for a readable text version
        text = resp.text
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # Limit to first 4000 chars for the agent
        return {
            "url": url,
            "content": text[:4000],
            "length": len(text),
            "truncated": len(text) > 4000,
        }
    except Exception as exc:
        log.exception("fetch_url failed for: %s", url)
        return {"error": f"Failed to fetch {url}: {exc}"}


# ── Artifact tools ────────────────────────────────────────────────────────────

def _save_artifact_from_url(inp: dict, ctx: dict) -> dict:
    """Fetch a URL and save it as an Artifact in the current series."""
    import re
    import requests as req
    from agent.models import Artifact, MeetingOccurrence
    from agent.tasks import embed_entity_async

    from ._series_fallback import ensure_series_id

    url = inp.get("url", "").strip()
    title = inp.get("title", "").strip()

    if not url:
        return {"error": "url required"}

    series_id = ensure_series_id(inp, ctx)

    # Fetch the page
    fetch_result = _fetch_url({"url": url}, ctx)
    if "error" in fetch_result:
        return fetch_result

    content = fetch_result.get("content", "")
    if not title:
        # Try to extract page title from content
        import re as re2
        title_match = re2.search(r'(?:^|\n)\s*([A-Z][^\n]{5,80})', content[:500])
        title = title_match.group(1).strip() if title_match else url[:80]

    art = Artifact.objects.create(
        series_id=series_id,
        title=title[:255],
        type="link",
        content=content[:8000],
        url=url,
        tags=inp.get("tags", []),
    )

    # Async embed
    embed_entity_async.delay(
        entity_table="agent_artifact",
        entity_id=str(art.id),
        text=f"{art.title}\n\n{art.content[:2000]}",
    )

    return {"saved": True, "artifact_id": str(art.id), "title": art.title, "url": url}


# ── Task promotion ────────────────────────────────────────────────────────────

def _promote_meeting_task(inp: dict, ctx: dict) -> dict:
    """Promote a MeetingTask (lightweight extraction) to a full persistent Task."""
    from agent.models import MeetingTask, Task

    meeting_task_id = inp.get("meeting_task_id")
    if not meeting_task_id:
        return {"error": "meeting_task_id required"}

    import uuid as _uuid
    try:
        _uuid.UUID(str(meeting_task_id))
    except ValueError:
        return {"error": f"'{meeting_task_id}' is not a valid UUID"}

    try:
        mt = MeetingTask.objects.select_related("occurrence__series").get(id=meeting_task_id)
    except MeetingTask.DoesNotExist:
        return {"error": f"meeting_task {meeting_task_id} not found"}

    if mt.promoted_task:
        return {"already_promoted": True, "task_id": str(mt.promoted_task.id), "title": mt.promoted_task.title}

    task = Task.objects.create(
        series=mt.occurrence.series,
        title=mt.title[:512],
        description=f"Promoted from meeting: {mt.occurrence.title or mt.occurrence.started_at}",
        priority=inp.get("priority", "medium"),
        owner=mt.assignee or inp.get("owner", ""),
        status="todo",
    )

    mt.promoted_task = task
    mt.save(update_fields=["promoted_task"])

    return {"promoted": True, "task_id": str(task.id), "title": task.title, "series": mt.occurrence.series.title}


# ── Email summary ─────────────────────────────────────────────────────────────

def _send_email_summary(inp: dict, ctx: dict) -> dict:
    """
    Send a post-meeting summary email via SMTP (uses EMAIL_HOST settings).
    Requires Django email settings to be configured in production_with_agent.py.
    """
    from django.core.mail import send_mail
    from django.conf import settings
    from agent.models import MeetingOccurrence

    occurrence_id = inp.get("occurrence_id") or ctx.get("occurrence_id")
    to_email = inp.get("to_email", "").strip()

    if not occurrence_id:
        return {"error": "occurrence_id required"}
    if not to_email:
        return {"error": "to_email required"}

    import uuid as _uuid
    try:
        _uuid.UUID(str(occurrence_id))
    except ValueError:
        return {"error": f"'{occurrence_id}' is not a valid UUID. Use get_recent_occurrences to find the ID."}

    try:
        occ = MeetingOccurrence.objects.select_related("series").get(id=occurrence_id)
    except MeetingOccurrence.DoesNotExist:
        return {"error": f"occurrence {occurrence_id} not found"}

    if not occ.summary:
        return {"error": "This meeting has no summary yet. Wait for post-processing to complete."}

    from agent.models import MeetingTask
    tasks = MeetingTask.objects.filter(occurrence=occ)
    tasks_text = ""
    if tasks.exists():
        tasks_text = "\n\nAction items:\n" + "\n".join(f"• {t.title}" + (f" ({t.assignee})" if t.assignee else "") for t in tasks)

    subject_date = occ.started_at.strftime("%b %d") if occ.started_at else "recent"
    subject = f"Meeting notes: {occ.title or occ.series.title} ({subject_date})"
    body = f"""Meeting notes from {occ.series.title}
{"=" * 50}

{occ.summary}{tasks_text}

---
Sent by Meeting Agent
"""

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "meetingagent@latentspaceco.com"),
            recipient_list=[to_email],
            fail_silently=False,
        )
        return {"sent": True, "to": to_email, "subject": subject}
    except Exception as exc:
        log.exception("send_email_summary: failed to send to %s", to_email)
        return {"error": f"Email send failed: {exc}. Check Django email settings (EMAIL_HOST, etc.)"}


# ── Tool registry ─────────────────────────────────────────────────────────────

TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="web_search",
        description="Search the web for current information. Use this for news, documentation, company information, or any question that benefits from a web search.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Number of results (default 5, max 10)"},
            },
            required=["query"],
        ),
        handler=_web_search,
    ),
    ToolDefinition(
        name="fetch_url",
        description="Fetch and read the content of a web page or URL. Returns the readable text content.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "url": {"type": "string", "description": "URL to fetch"},
            },
            required=["url"],
        ),
        handler=_fetch_url,
    ),
    ToolDefinition(
        name="save_artifact_from_url",
        description="Fetch a URL and save its content as an Artifact in the current meeting series for future reference.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "url": {"type": "string", "description": "URL to save"},
                "series_id": {"type": "string", "description": "UUID of the MeetingSeries (uses current series if not provided)"},
                "title": {"type": "string", "description": "Title for the artifact (optional, auto-detected if blank)"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
            },
            required=["url"],
        ),
        handler=_save_artifact_from_url,
    ),
    ToolDefinition(
        name="promote_meeting_task",
        description="Promote a lightweight meeting action item (MeetingTask) to a full persistent Task that will be tracked across sessions. Use this when someone commits to a task during a meeting.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "meeting_task_id": {"type": "string", "description": "UUID of the MeetingTask to promote — get from get_meeting_notes"},
                "priority": {
                    "type": "string",
                    "description": "Task priority",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "owner": {"type": "string", "description": "Person responsible if not already set"},
            },
            required=["meeting_task_id"],
        ),
        handler=_promote_meeting_task,
    ),
    ToolDefinition(
        name="send_email_summary",
        description="Send a post-meeting summary email with the meeting notes and action items to a specified email address.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "occurrence_id": {"type": "string", "description": "UUID of the MeetingOccurrence — use get_recent_occurrences to find it"},
                "to_email": {"type": "string", "description": "Email address to send the summary to"},
            },
            required=["occurrence_id", "to_email"],
        ),
        handler=_send_email_summary,
    ),
]
