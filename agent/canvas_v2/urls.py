from django.urls import path

from . import views

urlpatterns = [
    # Stable per-meeting redirect — the URL stays the same across bot
    # respawns. Users keep this tab open; behind the scenes it bounces
    # to whichever bot_id is currently active for that meeting code.
    path("m/<str:meet_code>/", views.canvas_by_meeting, name="canvas_v2_by_meeting"),
    path("<str:bot_id>/", views.canvas_shell, name="canvas_v2_shell"),
    path("<str:bot_id>/state.json", views.canvas_state_json, name="canvas_v2_state"),
    path("<str:bot_id>/stream", views.canvas_stream, name="canvas_v2_stream"),
    path("<str:bot_id>/navigate", views.canvas_navigate, name="canvas_v2_navigate"),
    path("<str:bot_id>/user-role", views.canvas_user_role, name="canvas_v2_user_role"),
]
