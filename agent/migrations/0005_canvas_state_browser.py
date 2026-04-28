"""
Add browser tab + browser_url/browser_title to CanvasState.

Lets the agent open a URL on the canvas via the `open_url` tool. The
canvas renders it in an iframe; users see it via the bot's video tile
and any active screenshare.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0004_canvas_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvasstate",
            name="browser_url",
            field=models.URLField(blank=True, default="", max_length=2048),
        ),
        migrations.AddField(
            model_name="canvasstate",
            name="browser_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
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
                    ("debug", "Debug"),
                ],
                default="dashboard",
                max_length=16,
            ),
        ),
    ]
