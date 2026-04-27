"""
Add CanvasState — per-bot UI state for the multi-tab canvas web app.

Introduced as part of the canvas-rebuild-and-one-brain refactor (Phase 2).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0003_agent_core_rewrite"),
        ("bots", "0081_remove_botevent_valid_event_type_event_sub_type_combinations_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="CanvasState",
            fields=[
                (
                    "bot",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="canvas_state",
                        serialize=False,
                        to="bots.bot",
                        to_field="object_id",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "active_tab",
                    models.CharField(
                        choices=[
                            ("dashboard", "Dashboard"),
                            ("notes", "Notes"),
                            ("tasks", "Tasks"),
                            ("focus", "Focus"),
                            ("debug", "Debug"),
                        ],
                        default="dashboard",
                        max_length=16,
                    ),
                ),
                ("notes_md", models.TextField(blank=True, default="")),
                ("focus_session_id", models.CharField(blank=True, default="", max_length=64)),
                ("focus_text", models.TextField(blank=True, default="")),
                ("focus_done", models.BooleanField(default=True)),
                ("dashboard_payload", models.JSONField(blank=True, default=dict)),
                ("user_driving", models.BooleanField(default=False)),
                ("user_driving_since", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Canvas State",
                "db_table": "agent_canvas_state",
            },
        ),
    ]
