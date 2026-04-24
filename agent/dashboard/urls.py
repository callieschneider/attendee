from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard_home, name="dashboard_home"),
    path("meeting/<str:bot_id>/", views.meeting_view, name="dashboard_meeting"),
    path(
        "meeting/<str:bot_id>/events-stream",
        views.meeting_events_stream,
        name="dashboard_meeting_stream",
    ),
    path(
        "meeting/<str:bot_id>/events.json",
        views.meeting_events_json,
        name="dashboard_meeting_events_json",
    ),
    path("series/", views.series_list, name="dashboard_series_list"),
    path(
        "series/<uuid:series_id>/config",
        views.series_config,
        name="dashboard_series_config",
    ),
]
