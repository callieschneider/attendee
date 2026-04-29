"""
Add `theme` to CanvasState (dark | light).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0005_canvas_state_browser"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvasstate",
            name="theme",
            field=models.CharField(
                max_length=8,
                choices=[("dark", "Dark"), ("light", "Light")],
                default="dark",
            ),
        ),
    ]
