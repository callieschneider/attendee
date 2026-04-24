from django.contrib import admin
from django.contrib import messages

from .models import (
    ActionLogEntry,
    Artifact,
    Conversation,
    ContextItem,
    EmbeddingChunk,
    MeetingCursor,
    MeetingOccurrence,
    MeetingSeries,
    MeetingTask,
    Message,
    ProjectIntelligence,
    SeriesRule,
    Task,
    TranscriptEvent,
    VoiceContextPush,
)


class SeriesRuleInline(admin.TabularInline):
    model = SeriesRule
    extra = 1
    fields = ("rule_type", "rule_value", "priority", "is_active")


@admin.register(MeetingSeries)
class MeetingSeriesAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "is_active",
        "agent_verbosity",
        "agent_proactivity",
        "max_cost_usd_per_meeting",
        "google_calendar_id",
        "created_at",
    )
    list_filter = ("is_active", "agent_verbosity", "agent_proactivity")
    search_fields = ("title", "description")
    fieldsets = (
        (None, {"fields": ("title", "description", "tags", "is_active")}),
        ("Calendar", {"fields": ("rrule", "google_calendar_id", "attendee_calendar_id")}),
        (
            "Agent behavior",
            {
                "fields": (
                    "agent_name_override",
                    "agent_verbosity",
                    "agent_proactivity",
                    "allowed_tool_categories",
                    "max_cost_usd_per_meeting",
                )
            },
        ),
    )
    inlines = [SeriesRuleInline]


@admin.register(SeriesRule)
class SeriesRuleAdmin(admin.ModelAdmin):
    list_display = ("rule_type", "rule_value", "series", "priority", "is_active")
    list_filter = ("rule_type", "is_active")
    search_fields = ("rule_value",)


def _promote_to_series_action(modeladmin, request, queryset):
    """Admin action: trigger series re-assignment for selected occurrences."""
    from agent.series_manager import assign_series
    from bots.models import CalendarEvent

    count = 0
    for occ in queryset:
        if occ.calendar_event_object_id:
            try:
                cal_event = CalendarEvent.objects.get(object_id=occ.calendar_event_object_id)
                series = assign_series(cal_event)
                occ.series = series
                occ.save(update_fields=["series"])
                count += 1
            except Exception as e:
                modeladmin.message_user(request, f"Failed for {occ}: {e}", level=messages.ERROR)
    modeladmin.message_user(request, f"Re-assigned {count} occurrences.")


_promote_to_series_action.short_description = "Re-assign to series (from calendar event)"


@admin.register(MeetingOccurrence)
class MeetingOccurrenceAdmin(admin.ModelAdmin):
    list_display = ("series", "title", "started_at", "ended_at", "calendar_event_object_id")
    list_filter = ("series",)
    search_fields = ("title", "summary", "calendar_event_object_id")
    raw_id_fields = ("bot",)
    actions = [_promote_to_series_action]


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "series", "status", "priority", "due_date")
    list_filter = ("status", "priority", "series")
    search_fields = ("title", "description")


@admin.register(MeetingTask)
class MeetingTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "occurrence", "assignee", "status")
    list_filter = ("status",)
    search_fields = ("title",)


@admin.register(Artifact)
class ArtifactAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "series", "is_deleted", "created_at")
    list_filter = ("type", "is_deleted", "series")
    search_fields = ("title", "content")


@admin.register(ProjectIntelligence)
class ProjectIntelligenceAdmin(admin.ModelAdmin):
    list_display = ("title", "series", "created_at")
    list_filter = ("series",)
    search_fields = ("title", "summary", "content")


@admin.register(ContextItem)
class ContextItemAdmin(admin.ModelAdmin):
    list_display = ("label", "series", "is_pinned", "order")
    list_filter = ("is_pinned",)
    search_fields = ("label", "content")


@admin.register(EmbeddingChunk)
class EmbeddingChunkAdmin(admin.ModelAdmin):
    list_display = ("entity_table", "entity_id", "chunk_index", "created_at")
    list_filter = ("entity_table",)
    search_fields = ("entity_id",)
    readonly_fields = ("embedding",)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "series", "created_at")
    list_filter = ("source",)
    search_fields = ("title",)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("role", "conversation", "tool_name", "created_at")
    list_filter = ("role",)
    search_fields = ("content", "tool_name")


# ── Phase 5 admin registrations ────────────────────────────────────────────────


@admin.register(TranscriptEvent)
class TranscriptEventAdmin(admin.ModelAdmin):
    list_display = ("event_time", "bot", "kind", "speaker", "short_text", "created_at")
    list_filter = ("kind",)
    search_fields = ("text", "speaker", "utterance_ref")
    readonly_fields = ("created_at",)
    raw_id_fields = ("bot", "occurrence")

    def short_text(self, obj):
        return (obj.text or "")[:80]

    short_text.short_description = "text"


@admin.register(ActionLogEntry)
class ActionLogEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "bot", "tool_name", "status", "latency_ms", "is_archived")
    list_filter = ("status", "tool_name", "is_archived")
    search_fields = ("tool_name", "error_message")
    readonly_fields = ("created_at",)
    raw_id_fields = ("bot", "occurrence", "trigger_start_event", "trigger_end_event")


@admin.register(VoiceContextPush)
class VoiceContextPushAdmin(admin.ModelAdmin):
    list_display = ("created_at", "bot", "short_text", "triggered_by_turn_id")
    search_fields = ("text", "session_handle")
    readonly_fields = ("created_at",)
    raw_id_fields = ("bot",)

    def short_text(self, obj):
        return (obj.text or "")[:80]

    short_text.short_description = "text"


@admin.register(MeetingCursor)
class MeetingCursorAdmin(admin.ModelAdmin):
    list_display = (
        "bot",
        "cursor_event_time",
        "last_turn_at",
        "audio_gate_open",
        "total_cost_usd",
        "budget_cap_usd",
        "budget_exceeded",
    )
    list_filter = ("audio_gate_open", "budget_exceeded")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("bot",)
