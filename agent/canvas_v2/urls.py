from django.urls import path

from . import views

urlpatterns = [
    path("<str:bot_id>/", views.canvas_shell, name="canvas_v2_shell"),
    path("<str:bot_id>/state.json", views.canvas_state_json, name="canvas_v2_state"),
    path("<str:bot_id>/stream", views.canvas_stream, name="canvas_v2_stream"),
    path("<str:bot_id>/navigate", views.canvas_navigate, name="canvas_v2_navigate"),
    path("<str:bot_id>/user-role", views.canvas_user_role, name="canvas_v2_user_role"),
]
