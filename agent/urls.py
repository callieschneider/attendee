from django.urls import include, path

from . import views

app_name = "agent"

urlpatterns = [
    # Create a bot wired to the audio bridge
    path("api/create-meeting-bot", views.create_meeting_bot, name="create_meeting_bot"),

    # Attendee webhook receiver
    path("webhooks/attendee", views.attendee_webhook, name="attendee_webhook"),

    # Gemini Live session management
    path("api/live-voice/token", views.live_voice_token, name="live_voice_token"),
    path("api/live-voice/tool", views.live_voice_tool, name="live_voice_tool"),

    # Google Calendar OAuth flow
    path("api/calendar/connect", views.calendar_connect, name="calendar_connect"),
    path("api/calendar/callback", views.calendar_callback, name="calendar_callback"),

    # Phase 5 live-meeting dashboard
    path("dashboard/", include("agent.dashboard.urls")),
]
