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
