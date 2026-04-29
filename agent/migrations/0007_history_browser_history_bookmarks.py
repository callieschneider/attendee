"""
Add the History tab to CanvasState and create the BrowserPageVisit
and Bookmark tables for series-wide recall.
"""
import uuid

import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0006_canvas_state_theme"),
        ("bots", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="canvasstate",
            name="active_tab",
            field=models.CharField(
                choices=[
                    ("dashboard", "Dashboard"),
                    ("notes", "Notes"),
                    ("tasks", "Tasks"),
                    ("focus", "Focus"),
                    ("browser", "Browser"),
                    ("history", "History"),
                    ("debug", "Debug"),
                ],
                default="dashboard",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="BrowserPageVisit",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("url", models.URLField(max_length=2048)),
                ("title", models.CharField(blank=True, default="", max_length=512)),
                ("source", models.CharField(default="page_navigate", max_length=16)),
                (
                    "series",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="browser_visits",
                        to="agent.meetingseries",
                    ),
                ),
                (
                    "bot",
                    models.ForeignKey(
                        db_index=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="browser_visits",
                        to="bots.bot",
                        to_field="object_id",
                    ),
                ),
            ],
            options={
                "verbose_name": "Browser Page Visit",
                "db_table": "agent_browser_page_visit",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["series", "-created_at"],
                        name="agent_brows_series__d29ae0_idx",
                    ),
                    models.Index(
                        fields=["bot", "-created_at"],
                        name="agent_brows_bot_id_4cf3df_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Bookmark",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("url", models.URLField(max_length=2048)),
                ("label", models.CharField(max_length=255)),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "tags",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=64),
                        blank=True,
                        default=list,
                        size=None,
                    ),
                ),
                (
                    "series",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bookmarks",
                        to="agent.meetingseries",
                    ),
                ),
                (
                    "created_by_bot",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="bots.bot",
                        to_field="object_id",
                    ),
                ),
            ],
            options={
                "verbose_name": "Bookmark",
                "db_table": "agent_bookmark",
                "ordering": ["-updated_at"],
                "indexes": [
                    models.Index(
                        fields=["series", "-updated_at"],
                        name="agent_bookm_series__52a8a1_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("series", "url"), name="unique_bookmark_per_series"
                    )
                ],
            },
        ),
    ]
