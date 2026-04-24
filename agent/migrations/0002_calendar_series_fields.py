"""
Add calendar integration fields to MeetingSeries + MeetingOccurrence,
and add the SeriesRule model.
"""
import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0001_initial_schema"),
    ]

    operations = [
        # MeetingSeries — swap old google_calendar_event_id for new calendar fields
        migrations.RemoveField(
            model_name="meetingseries",
            name="google_calendar_event_id",
        ),
        migrations.AddField(
            model_name="meetingseries",
            name="google_calendar_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="meetingseries",
            name="attendee_calendar_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),

        # MeetingOccurrence — add calendar event linkage fields
        migrations.AddField(
            model_name="meetingoccurrence",
            name="calendar_event_object_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="meetingoccurrence",
            name="google_event_id",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="meetingoccurrence",
            name="google_recurring_event_id",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),

        # SeriesRule model
        migrations.CreateModel(
            name="SeriesRule",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("rule_type", models.CharField(
                    choices=[
                        ("recurring_uid", "Google Calendar recurring event UID (ical_uid)"),
                        ("series_tag", "Description tag (#series:slug)"),
                        ("attendee_set", "Attendee email set match (comma-separated)"),
                        ("title_contains", "Event title contains string"),
                    ],
                    max_length=32,
                )),
                ("rule_value", models.CharField(max_length=1024)),
                ("priority", models.IntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("series", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="rules",
                    to="agent.meetingseries",
                )),
            ],
            options={
                "db_table": "agent_series_rule",
                "ordering": ["-priority", "created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="seriesrule",
            index=models.Index(
                fields=["rule_type", "rule_value", "is_active"],
                name="agent_serie_rule_ty_b7e4d4_idx",
            ),
        ),
    ]
