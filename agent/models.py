"""
Agent app data models.

Isolated from abstraKt. FKs INTO bots.* are allowed (same DB).
FKs OUT FROM bots.* to agent.* are NOT (don't modify upstream).
"""
import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from pgvector.django import VectorField


class MeetingSeries(models.Model):
    """A recurring meeting series or project — the top-level grouping concept."""

    AGENT_VERBOSITY_CHOICES = [("terse", "Terse"), ("normal", "Normal"), ("chatty", "Chatty")]
    AGENT_PROACTIVITY_CHOICES = [
        ("silent", "Silent"),
        ("reactive", "Reactive"),
        ("proactive", "Proactive"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    tags = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    is_active = models.BooleanField(default=True)

    # Calendar integration
    rrule = models.CharField(max_length=512, blank=True, default="")
    google_calendar_id = models.CharField(max_length=255, blank=True, default="")
    attendee_calendar_id = models.CharField(max_length=64, blank=True, default="")

    # Phase 5: per-series agent behavior overrides
    agent_name_override = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Override the default agent name for this series. Blank = use global AGENT_NAME.",
    )
    agent_verbosity = models.CharField(
        max_length=16, choices=AGENT_VERBOSITY_CHOICES, default="normal"
    )
    agent_proactivity = models.CharField(
        max_length=16, choices=AGENT_PROACTIVITY_CHOICES, default="reactive"
    )
    allowed_tool_categories = ArrayField(
        models.CharField(max_length=32),
        default=list,
        blank=True,
        help_text=(
            "Empty list = all categories allowed. Categories: meetings, series, tasks, "
            "artifacts, search, utility, voice, chat, visual."
        ),
    )
    max_cost_usd_per_meeting = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Per-meeting USD budget cap. Null = use AGENT_MAX_TURN_BUDGET_USD default.",
    )

    class Meta:
        db_table = "agent_meeting_series"
        indexes = [models.Index(fields=["is_active", "-updated_at"])]
        verbose_name = "Meeting Series"
        verbose_name_plural = "Meeting Series"

    def __str__(self):
        return self.title


class MeetingOccurrence(models.Model):
    """A single real meeting instance — links back to the Attendee Bot that ran it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    series = models.ForeignKey(
        "MeetingSeries", on_delete=models.CASCADE, related_name="occurrences"
    )
    # FK to bots.Bot using string ref to avoid import coupling
    bot = models.ForeignKey(
        "bots.Bot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        to_field="object_id",
    )

    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    title = models.CharField(max_length=255, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    transcript_text = models.TextField(blank=True, default="")
    attendees = ArrayField(models.CharField(max_length=255), default=list, blank=True)

    # Calendar event linkage
    calendar_event_object_id = models.CharField(max_length=64, blank=True, default="")
    google_event_id = models.CharField(max_length=1024, blank=True, default="")
    google_recurring_event_id = models.CharField(max_length=1024, blank=True, default="")

    class Meta:
        db_table = "agent_meeting_occurrence"
        indexes = [
            models.Index(fields=["series", "-started_at"]),
            models.Index(fields=["bot"]),
        ]
        verbose_name = "Meeting Occurrence"

    def __str__(self):
        return f"{self.series.title} — {self.started_at or self.created_at:%Y-%m-%d}"


class Task(models.Model):
    """Full task tracker — persistent, owned by a MeetingSeries."""

    STATUS_CHOICES = [
        ("backlog", "Backlog"),
        ("todo", "Todo"),
        ("in_progress", "In Progress"),
        ("in_review", "In Review"),
        ("done", "Done"),
        ("cancelled", "Cancelled"),
    ]
    PRIORITY_CHOICES = [
        ("critical", "Critical"),
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    series = models.ForeignKey("MeetingSeries", on_delete=models.CASCADE, related_name="tasks")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="subtasks"
    )

    title = models.CharField(max_length=512)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="todo")
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default="medium")
    owner = models.CharField(max_length=255, blank=True, default="")

    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    labels = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    position = models.IntegerField(default=0)

    class Meta:
        db_table = "agent_task"
        indexes = [
            models.Index(fields=["series", "status", "-updated_at"]),
            models.Index(fields=["due_date"]),
        ]

    def __str__(self):
        return self.title


class MeetingTask(models.Model):
    """Lightweight action item extracted from a meeting. Can be promoted to a full Task."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    occurrence = models.ForeignKey(
        "MeetingOccurrence", on_delete=models.CASCADE, related_name="meeting_tasks"
    )
    promoted_task = models.ForeignKey(
        "Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    title = models.CharField(max_length=512)
    assignee = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, default="pending")  # pending | done | cancelled

    class Meta:
        db_table = "agent_meeting_task"
        constraints = [
            models.UniqueConstraint(
                fields=["occurrence", "title"], name="unique_meeting_task_title_per_occ"
            ),
        ]
        verbose_name = "Meeting Task"

    def __str__(self):
        return self.title


class Artifact(models.Model):
    """Document, link, chart, or file attached to a meeting series or occurrence."""

    TYPE_CHOICES = [
        ("note", "Note"),
        ("link", "Link"),
        ("file", "File"),
        ("chart", "Chart"),
        ("image", "Image"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    series = models.ForeignKey("MeetingSeries", on_delete=models.CASCADE, related_name="artifacts")
    occurrence = models.ForeignKey(
        "MeetingOccurrence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="artifacts",
    )

    title = models.CharField(max_length=255)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES, default="note")
    content = models.TextField(blank=True, default="")
    url = models.URLField(blank=True, default="")
    tags = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        db_table = "agent_artifact"
        indexes = [models.Index(fields=["series", "is_deleted", "-updated_at"])]

    def __str__(self):
        return self.title


class ProjectIntelligence(models.Model):
    """LLM-generated briefing for a meeting series — the series' 'memory of what happened'."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    series = models.ForeignKey(
        "MeetingSeries", on_delete=models.CASCADE, related_name="intelligence"
    )
    occurrence = models.ForeignKey(
        "MeetingOccurrence", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    title = models.CharField(max_length=255)
    summary = models.TextField()
    content = models.TextField()
    decisions_noted = ArrayField(models.TextField(), default=list, blank=True)
    open_questions = ArrayField(models.TextField(), default=list, blank=True)
    tags = ArrayField(models.CharField(max_length=64), default=list, blank=True)

    class Meta:
        db_table = "agent_project_intelligence"
        indexes = [models.Index(fields=["series", "-created_at"])]
        verbose_name = "Project Intelligence"
        verbose_name_plural = "Project Intelligence"

    def __str__(self):
        return self.title


class ContextItem(models.Model):
    """Pinned context snippet always injected into the system prompt for a series/conversation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    series = models.ForeignKey(
        "MeetingSeries",
        on_delete=models.CASCADE,
        related_name="context_items",
        null=True,
        blank=True,
    )
    conversation = models.ForeignKey(
        "Conversation",
        on_delete=models.CASCADE,
        related_name="context_items",
        null=True,
        blank=True,
    )

    label = models.CharField(max_length=255, blank=True, default="")
    content = models.TextField()
    order = models.IntegerField(default=0)
    is_pinned = models.BooleanField(default=True)

    class Meta:
        db_table = "agent_context_item"
        ordering = ["order"]
        verbose_name = "Context Item"

    def __str__(self):
        return self.label or self.content[:60]


class EmbeddingChunk(models.Model):
    """Polymorphic chunked embeddings — one row per chunk of any embeddable entity."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    entity_table = models.CharField(max_length=64)   # e.g. "agent_artifact"
    entity_id = models.UUIDField()
    chunk_index = models.IntegerField(default=0)
    content = models.TextField()
    embedding = VectorField(dimensions=1536)

    class Meta:
        db_table = "agent_embedding_chunk"
        indexes = [models.Index(fields=["entity_table", "entity_id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["entity_table", "entity_id", "chunk_index"],
                name="unique_embedding_chunk_per_entity",
            ),
        ]
        verbose_name = "Embedding Chunk"

    def __str__(self):
        return f"{self.entity_table}/{self.entity_id} chunk {self.chunk_index}"


class Conversation(models.Model):
    """A Gemini Live or chat session — links to a series/occurrence for context."""

    SOURCE_CHOICES = [
        ("gemini_live", "Gemini Live"),
        ("chat", "Chat"),
        ("webhook", "Webhook"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    series = models.ForeignKey(
        "MeetingSeries",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    occurrence = models.ForeignKey(
        "MeetingOccurrence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )

    title = models.CharField(max_length=255, blank=True, default="")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default="gemini_live")

    class Meta:
        db_table = "agent_conversation"
        indexes = [models.Index(fields=["-updated_at"])]

    def __str__(self):
        return self.title or f"{self.source} {self.created_at:%Y-%m-%d %H:%M}"


class Message(models.Model):
    """A single message in a Conversation — user, assistant, system, or tool call."""

    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
        ("system", "System"),
        ("tool", "Tool"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    conversation = models.ForeignKey(
        "Conversation", on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField(blank=True, default="")
    tool_name = models.CharField(max_length=128, blank=True, default="")
    tool_input = models.JSONField(null=True, blank=True)
    tool_output = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "agent_message"
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self):
        return f"{self.role}: {self.content[:60]}"


class SeriesRule(models.Model):
    """
    Maps a matching rule to a MeetingSeries.
    When a CalendarEvent is assigned, these rules are checked in priority order.
    """

    RULE_TYPES = [
        ("recurring_uid", "Google Calendar recurring event UID (ical_uid)"),
        ("series_tag", "Description tag (#series:slug)"),
        ("attendee_set", "Attendee email set match (comma-separated)"),
        ("title_contains", "Event title contains string"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    series = models.ForeignKey("MeetingSeries", on_delete=models.CASCADE, related_name="rules")
    rule_type = models.CharField(max_length=32, choices=RULE_TYPES)
    rule_value = models.CharField(max_length=1024)
    priority = models.IntegerField(default=0)  # higher = checked first
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "agent_series_rule"
        ordering = ["-priority", "created_at"]
        indexes = [models.Index(fields=["rule_type", "rule_value", "is_active"])]

    def __str__(self):
        return f"{self.rule_type}:{self.rule_value} → {self.series.title}"


# ── Phase 5: agent core rewrite — durable meeting state ────────────────────────


class TranscriptEvent(models.Model):
    """
    Append-only log of everything that happens in a live meeting.
    Speech utterances, chat messages, agent action markers, and system events
    all flow into this timeline so the Turn Processor can operate on a single
    durable stream.
    """

    KIND_CHOICES = [
        ("speech", "Speech utterance"),
        ("chat", "Meet chat message"),
        ("action", "Agent action marker (details in ActionLogEntry)"),
        ("system", "System event (bot joined/left, gate change, etc.)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    bot = models.ForeignKey(
        "bots.Bot",
        on_delete=models.CASCADE,
        to_field="object_id",
        related_name="transcript_events",
        db_index=True,
    )
    occurrence = models.ForeignKey(
        "MeetingOccurrence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transcript_events",
    )

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    event_time = models.DateTimeField(db_index=True)

    speaker = models.CharField(max_length=255, blank=True, default="")
    speaker_uuid = models.CharField(max_length=255, blank=True, default="")
    text = models.TextField(blank=True, default="")
    raw = models.JSONField(default=dict, blank=True)
    utterance_ref = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        db_table = "agent_transcript_event"
        ordering = ["event_time", "created_at"]
        indexes = [
            models.Index(fields=["bot", "event_time"]),
            models.Index(fields=["bot", "kind", "event_time"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["bot", "utterance_ref"],
                condition=models.Q(utterance_ref__gt=""),
                name="unique_transcript_event_utterance_ref_per_bot",
            ),
        ]
        verbose_name = "Transcript Event"

    def __str__(self):
        ts = self.event_time.strftime("%H:%M:%S") if self.event_time else "??"
        preview = (self.text or "").replace("\n", " ")[:60]
        return f"{self.kind}@{ts} {self.speaker}: {preview}"


class ActionLogEntry(models.Model):
    """
    One row per tool call made by the Turn Processor during a meeting.
    Provides the agent with durable memory of "what I have already done"
    across turns and across session drops.
    """

    STATUS_CHOICES = [("pending", "Pending"), ("ok", "OK"), ("error", "Error")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    bot = models.ForeignKey(
        "bots.Bot",
        on_delete=models.CASCADE,
        to_field="object_id",
        related_name="action_log",
    )
    occurrence = models.ForeignKey(
        "MeetingOccurrence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="action_log",
    )
    turn_id = models.UUIDField(db_index=True)

    tool_name = models.CharField(max_length=128, db_index=True)
    tool_input = models.JSONField(default=dict)
    tool_result = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    latency_ms = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    # Horizon summarization — allow compressing old entries into synthetic rows
    is_archived = models.BooleanField(default=False, db_index=True)

    # What transcript event range triggered this action (for replay / debug)
    trigger_start_event = models.ForeignKey(
        "TranscriptEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    trigger_end_event = models.ForeignKey(
        "TranscriptEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "agent_action_log_entry"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["bot", "created_at"]),
            models.Index(fields=["turn_id"]),
        ]
        verbose_name = "Action Log Entry"
        verbose_name_plural = "Action Log Entries"

    def __str__(self):
        return f"{self.tool_name} [{self.status}] @ {self.created_at:%H:%M:%S}"


class VoiceContextPush(models.Model):
    """
    Audit log of text briefings pushed into Gemini Live via realtimeInput.text.
    Lets us debug "did the voice agent know about X?" after the fact.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    bot = models.ForeignKey(
        "bots.Bot",
        on_delete=models.CASCADE,
        to_field="object_id",
        related_name="voice_context_pushes",
    )
    session_handle = models.CharField(max_length=512, blank=True, default="")
    text = models.TextField()
    triggered_by_turn_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "agent_voice_context_push"
        ordering = ["-created_at"]
        verbose_name = "Voice Context Push"

    def __str__(self):
        return f"{self.bot_id} @ {self.created_at:%H:%M:%S}: {self.text[:60]}"


class CanvasState(models.Model):
    """
    Per-bot UI state for the multi-tab canvas web app.

    The canvas (Next.js / browser) reads this to render the dashboard, notes,
    tasks list, focus stream, and debug view. The agent writes to it via the
    `navigate_canvas`, `update_notes`, `update_dashboard`, and `think_deep`
    tools. Real-time fan-out to connected canvas clients happens via Redis
    pubsub (`canvas:state:{bot_id}` and `canvas:stream:{bot_id}:{tab}`); this
    model is the durable / late-join snapshot.

    Single row per active bot. Safe to leave around after a meeting ends —
    the canvas just goes stale.
    """

    TAB_DASHBOARD = "dashboard"
    TAB_NOTES = "notes"
    TAB_TASKS = "tasks"
    TAB_FOCUS = "focus"
    TAB_BROWSER = "browser"
    TAB_HISTORY = "history"
    TAB_DEBUG = "debug"
    TAB_CHOICES = [
        (TAB_DASHBOARD, "Dashboard"),
        (TAB_NOTES, "Notes"),
        (TAB_TASKS, "Tasks"),
        (TAB_FOCUS, "Focus"),
        (TAB_BROWSER, "Browser"),
        (TAB_HISTORY, "History"),
        (TAB_DEBUG, "Debug"),
    ]

    bot = models.OneToOneField(
        "bots.Bot",
        to_field="object_id",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="canvas_state",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    active_tab = models.CharField(
        max_length=16,
        choices=TAB_CHOICES,
        default=TAB_DASHBOARD,
    )

    # Markdown body of the agent's running notes for this meeting. Append-only
    # by convention; the agent uses `update_notes` to mutate it.
    notes_md = models.TextField(blank=True, default="")

    # Identifier of the most recent `think_deep` streaming session. The canvas
    # focus tab subscribes to `canvas:stream:<bot_id>:focus` and matches by
    # session_id so a stale stream doesn't overwrite a fresh one.
    focus_session_id = models.CharField(max_length=64, blank=True, default="")
    focus_text = models.TextField(blank=True, default="")
    focus_done = models.BooleanField(default=True)

    # Free-form JSON the agent populates for the dashboard cards.
    dashboard_payload = models.JSONField(default=dict, blank=True)

    # Browser tab state — the agent can put a URL on the canvas via the
    # `open_url` tool. The canvas renders it in an iframe; sites that
    # send X-Frame-Options: DENY won't load (the canvas shows a "this
    # site can't be embedded" fallback with an open-in-new-tab link).
    # Navigation within the iframe is read-only by design — for full
    # interactive automation see Phase 2 (headless Chrome controlled
    # by the bridge).
    browser_url = models.URLField(max_length=2048, blank=True, default="")
    browser_title = models.CharField(max_length=255, blank=True, default="")

    # Visual theme — light or dark. Default dark since the canvas
    # historically lived in dark mode. Switchable via the
    # `set_canvas_theme` tool or any user-driving canvas client.
    THEME_DARK = "dark"
    THEME_LIGHT = "light"
    THEME_CHOICES = [(THEME_DARK, "Dark"), (THEME_LIGHT, "Light")]
    theme = models.CharField(
        max_length=8, choices=THEME_CHOICES, default=THEME_DARK,
    )

    # When a non-bot WS client (the user's own browser) is connected the agent
    # treats canvas navigation as user-driven and stops auto-switching tabs.
    user_driving = models.BooleanField(default=False)
    user_driving_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "agent_canvas_state"
        verbose_name = "Canvas State"

    def __str__(self):
        return f"canvas bot={self.bot_id} tab={self.active_tab}"


class MeetingCursor(models.Model):
    """
    Live meeting state snapshot — one row per active bot.
    Holds the transcript cursor, voice session handle, audio gate state,
    and cost tracking. Purgeable after meeting ends.
    """

    bot = models.OneToOneField(
        "bots.Bot",
        to_field="object_id",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="agent_cursor",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Transcript cursor (inclusive — last event processed)
    cursor_event_time = models.DateTimeField(null=True, blank=True)
    cursor_event_created_at = models.DateTimeField(null=True, blank=True)
    last_turn_id = models.UUIDField(null=True, blank=True)
    last_turn_at = models.DateTimeField(null=True, blank=True)

    # Voice session state
    voice_session_handle = models.CharField(max_length=512, blank=True, default="")
    voice_session_opened_at = models.DateTimeField(null=True, blank=True)

    # Audio gate state
    audio_gate_open = models.BooleanField(default=False)
    audio_gate_opened_at = models.DateTimeField(null=True, blank=True)
    audio_gate_reason = models.CharField(max_length=64, blank=True, default="")

    # Cost tracking
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    budget_cap_usd = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    budget_exceeded = models.BooleanField(default=False)

    class Meta:
        db_table = "agent_meeting_cursor"
        verbose_name = "Meeting Cursor"

    def __str__(self):
        return f"cursor bot={self.bot_id} turn={self.last_turn_id}"


class BrowserPageVisit(models.Model):
    """
    Append-only log of every URL the agent navigates the bot's headless
    Chrome to (via `page_navigate` / `open_url`), keyed to the meeting
    series so the agent can recall pages across multiple meetings in
    the same series.

    NOT a substitute for the per-meeting transcript — this tracks the
    bot's *web-browsing* history, not its conversational history.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    series = models.ForeignKey(
        "MeetingSeries",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="browser_visits",
    )
    bot = models.ForeignKey(
        "bots.Bot",
        on_delete=models.CASCADE,
        to_field="object_id",
        related_name="browser_visits",
        db_index=True,
    )
    url = models.URLField(max_length=2048)
    title = models.CharField(max_length=512, blank=True, default="")
    # 'open_url' = display-only iframe; 'page_navigate' = interactive Chrome.
    source = models.CharField(max_length=16, default="page_navigate")

    class Meta:
        db_table = "agent_browser_page_visit"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["series", "-created_at"]),
            models.Index(fields=["bot", "-created_at"]),
        ]
        verbose_name = "Browser Page Visit"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.url}"


class Bookmark(models.Model):
    """
    Saved URL the agent (or the user, via the agent) wants to be able
    to pull up again later. Lives at the series level so the same
    bookmark surfaces across every meeting in the series.

    series=NULL means "global to this bot's project" — for stuff the
    user wants accessible from any meeting.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    series = models.ForeignKey(
        "MeetingSeries",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="bookmarks",
    )
    # Track who/what created it for provenance — bot_id of the agent
    # that saved it. Optional because manually-created bookmarks (e.g.
    # via admin) won't have one.
    created_by_bot = models.ForeignKey(
        "bots.Bot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        to_field="object_id",
        related_name="+",
    )

    url = models.URLField(max_length=2048)
    label = models.CharField(max_length=255)
    notes = models.TextField(blank=True, default="")
    tags = ArrayField(models.CharField(max_length=64), default=list, blank=True)

    class Meta:
        db_table = "agent_bookmark"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["series", "-updated_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "url"],
                name="unique_bookmark_per_series",
            ),
        ]
        verbose_name = "Bookmark"

    def __str__(self):
        return f"{self.label} → {self.url}"
