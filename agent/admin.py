from django.contrib import admin
from django.contrib import messages

from .models import (
    Artifact,
    Conversation,
    ContextItem,
    EmbeddingChunk,
    MeetingOccurrence,
    MeetingSeries,
    MeetingTask,
    Message,
    ProjectIntelligence,
    SeriesRule,
    Task,
)


class SeriesRuleInline(admin.TabularInline):
    model = SeriesRule
    extra = 1
    fields = ("rule_type", "rule_value", "priority", "is_active")


@admin.register(MeetingSeries)
class MeetingSeriesAdmin(admin.ModelAdmin):
    list_display = ("title", "is_active", "google_calendar_id", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "description")
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
