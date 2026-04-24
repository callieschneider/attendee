from django.urls import path

from . import views

urlpatterns = [
    # Session-keyed routes (what create_meeting_bot uses — Attendee resolves)
    path("session/<str:session_id>", views.session_canvas_view, name="session_canvas_view"),
    path("session/<str:session_id>/stream", views.session_canvas_stream, name="session_canvas_stream"),
    path("session/<str:session_id>/state.json", views.session_canvas_state, name="session_canvas_state"),
    # Direct bot_id routes
    path("<str:bot_id>/", views.canvas_view, name="canvas_view"),
    path("<str:bot_id>/stream", views.canvas_stream, name="canvas_stream"),
    path("<str:bot_id>/state.json", views.canvas_state, name="canvas_state"),
]
