"""
Series assignment logic — maps a CalendarEvent to a MeetingSeries.

Priority:
  1. #series:slug tag in event description
  2. SeriesRule match by recurring_uid (ical_uid)
  3. SeriesRule match by title_contains
  4. Auto-create a series from recurring_uid if recurring event
  5. Fallback: Inbox series
"""
import logging
import re
from typing import Optional

log = logging.getLogger("agent.series_manager")

SERIES_TAG_PATTERN = re.compile(r"#series:([\w-]+)", re.I)


def extract_series_tag(text: str) -> Optional[str]:
    """Extract #series:slug from event description or name."""
    if not text:
        return None
    m = SERIES_TAG_PATTERN.search(text)
    return m.group(1).lower() if m else None


def assign_series(calendar_event) -> "agent.models.MeetingSeries":  # type: ignore
    """
    Given a bots.CalendarEvent, return the appropriate agent.MeetingSeries.
    Side effects: may create a new MeetingSeries or SeriesRule for first-seen events.
    """
    from agent.models import MeetingSeries, SeriesRule

    raw = calendar_event.raw or {}
    description = raw.get("description", "") or ""
    name = calendar_event.name or ""
    ical_uid = calendar_event.ical_uid or ""

    # Extract #series: tag from description or name
    tag = extract_series_tag(description) or extract_series_tag(name)

    # 1. Tag match or tag-based auto-create
    if tag:
        rule = (
            SeriesRule.objects.filter(rule_type="series_tag", rule_value=tag, is_active=True)
            .select_related("series")
            .first()
        )
        if rule:
            log.info("assign_series: event %s matched tag rule → %s", calendar_event.object_id, rule.series.title)
            return rule.series

        # New tag — auto-create a series and a rule for it
        series_title = tag.replace("-", " ").title()
        series, _ = MeetingSeries.objects.get_or_create(
            title=series_title,
            defaults={"description": f"Auto-created from #series:{tag} tag"},
        )
        SeriesRule.objects.get_or_create(
            rule_type="series_tag",
            rule_value=tag,
            defaults={"series": series, "priority": 10},
        )
        log.info("assign_series: event %s auto-created series '%s' from tag #series:%s",
                 calendar_event.object_id, series_title, tag)
        return series

    # 2. Recurring UID match
    if ical_uid:
        rule = (
            SeriesRule.objects.filter(rule_type="recurring_uid", rule_value=ical_uid, is_active=True)
            .select_related("series")
            .first()
        )
        if rule:
            log.info("assign_series: event %s matched recurring_uid rule → %s",
                     calendar_event.object_id, rule.series.title)
            return rule.series

    # 3. Title-contains match
    if name:
        for rule in SeriesRule.objects.filter(rule_type="title_contains", is_active=True).select_related("series"):
            if rule.rule_value.lower() in name.lower():
                log.info("assign_series: event %s matched title_contains rule → %s",
                         calendar_event.object_id, rule.series.title)
                return rule.series

    # 4. For recurring events — auto-create a named series from the event title
    if ical_uid and name:
        series_title = name[:255]
        series, created = MeetingSeries.objects.get_or_create(
            title=series_title,
            defaults={"description": f"Auto-created from recurring event: {name}"},
        )
        # Register the recurring UID so future instances hit this series
        SeriesRule.objects.get_or_create(
            rule_type="recurring_uid",
            rule_value=ical_uid,
            defaults={"series": series, "priority": 5},
        )
        if created:
            log.info("assign_series: event %s auto-created series '%s' from recurring event",
                     calendar_event.object_id, series_title)
        return series

    # 5. Inbox fallback
    inbox, _ = MeetingSeries.objects.get_or_create(
        title="Inbox",
        defaults={"description": "Auto-created for unassigned meeting occurrences"},
    )
    log.info("assign_series: event %s → Inbox (no rule matched)", calendar_event.object_id)
    return inbox


def schedule_bot_for_upcoming_events(calendar_object_id: str) -> list:
    """
    After a calendar sync, find all upcoming CalendarEvents with Meet URLs
    and schedule bots for them if not already done.
    Returns list of scheduled event IDs.
    """
    from bots.models import Calendar, CalendarEvent, BotStates
    from django.utils import timezone
    import datetime

    try:
        calendar = Calendar.objects.get(object_id=calendar_object_id)
    except Calendar.DoesNotExist:
        log.warning("schedule_bot_for_upcoming_events: calendar %s not found", calendar_object_id)
        return []

    now = timezone.now()
    look_ahead = now + datetime.timedelta(days=7)

    upcoming = CalendarEvent.objects.filter(
        calendar=calendar,
        start_time__gte=now,
        start_time__lte=look_ahead,
        is_deleted=False,
    ).exclude(meeting_url__isnull=True).exclude(meeting_url="")

    scheduled = []
    for event in upcoming:
        # Don't schedule if a bot already exists for this event
        existing = event.bots.filter(
            state__in=[
                "ready", "joining", "waiting_room", "joined_not_recording",
                "joined_recording", "scheduled", "staged",
            ]
        ).exists()
        if not existing:
            from agent.tasks import process_calendar_event
            process_calendar_event.delay(event.object_id)
            scheduled.append(event.object_id)
            log.info("schedule_bot_for_upcoming_events: scheduled %s (%s) at %s",
                     event.object_id, event.name, event.start_time)

    return scheduled
