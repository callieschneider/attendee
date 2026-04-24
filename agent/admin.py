from django.contrib import admin

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
    Task,
)


@admin.register(MeetingSeries)
class MeetingSeriesAdmin(admin.ModelAdmin):
    list_display = ("title", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "description")


@admin.register(MeetingOccurrence)
class MeetingOccurrenceAdmin(admin.ModelAdmin):
    list_display = ("series", "title", "started_at", "ended_at")
    list_filter = ("series",)
    search_fields = ("title", "summary")
    raw_id_fields = ("bot",)


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
