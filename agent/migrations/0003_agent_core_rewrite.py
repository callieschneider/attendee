"""
Phase 5 — Agent core rewrite.

Adds:
- Per-series agent behavior overrides on MeetingSeries
- TranscriptEvent (durable per-event log)
- ActionLogEntry (tool-call ledger)
- VoiceContextPush (audit log of text briefings to Gemini Live)
- MeetingCursor (live per-bot agent state)

Nothing destructive: all additions are additive.
"""
import uuid

import django.db.models.deletion
from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0002_calendar_series_fields"),
        # TranscriptEvent / ActionLogEntry / VoiceContextPush / MeetingCursor
        # FK bots.Bot via object_id; use a dependency that's certain to exist.
        ("bots", "0081_remove_botevent_valid_event_type_event_sub_type_combinations_and_more"),
    ]

    operations = [
        # ── MeetingSeries: per-series agent config fields ──
        migrations.AddField(
            model_name="meetingseries",
            name="agent_name_override",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Override the default agent name for this series. "
                    "Blank = use global AGENT_NAME."
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="meetingseries",
            name="agent_verbosity",
            field=models.CharField(
                choices=[("terse", "Terse"), ("normal", "Normal"), ("chatty", "Chatty")],
                default="normal",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="meetingseries",
            name="agent_proactivity",
            field=models.CharField(
                choices=[
                    ("silent", "Silent"),
                    ("reactive", "Reactive"),
                    ("proactive", "Proactive"),
                ],
                default="reactive",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="meetingseries",
            name="allowed_tool_categories",
            field=ArrayField(
                base_field=models.CharField(max_length=32),
                blank=True,
                default=list,
                help_text=(
                    "Empty list = all categories allowed. Categories: meetings, "
                    "series, tasks, artifacts, search, utility, voice, chat, visual."
                ),
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="meetingseries",
            name="max_cost_usd_per_meeting",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text=(
                    "Per-meeting USD budget cap. Null = use "
                    "AGENT_MAX_TURN_BUDGET_USD default."
                ),
                max_digits=10,
                null=True,
            ),
        ),

        # ── TranscriptEvent ──
        migrations.CreateModel(
            name="TranscriptEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("kind", models.CharField(
                    choices=[
                        ("speech", "Speech utterance"),
                        ("chat", "Meet chat message"),
                        ("action", "Agent action marker (details in ActionLogEntry)"),
                        ("system", "System event (bot joined/left, gate change, etc.)"),
                    ],
                    db_index=True,
                    max_length=16,
                )),
                ("event_time", models.DateTimeField(db_index=True)),
                ("speaker", models.CharField(blank=True, default="", max_length=255)),
                ("speaker_uuid", models.CharField(blank=True, default="", max_length=255)),
                ("text", models.TextField(blank=True, default="")),
                ("raw", models.JSONField(blank=True, default=dict)),
                ("utterance_ref", models.CharField(blank=True, default="", max_length=128)),
                ("bot", models.ForeignKey(
                    db_index=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="transcript_events",
                    to="bots.bot",
                    to_field="object_id",
                )),
                ("occurrence", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="transcript_events",
                    to="agent.meetingoccurrence",
                )),
            ],
            options={
                "verbose_name": "Transcript Event",
                "db_table": "agent_transcript_event",
                "ordering": ["event_time", "created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="transcriptevent",
            index=models.Index(fields=["bot", "event_time"], name="agent_trans_bot_id_e2f7a8_idx"),
        ),
        migrations.AddIndex(
            model_name="transcriptevent",
            index=models.Index(fields=["bot", "kind", "event_time"], name="agent_trans_bot_id_9c38b6_idx"),
        ),
        migrations.AddConstraint(
            model_name="transcriptevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(("utterance_ref__gt", "")),
                fields=("bot", "utterance_ref"),
                name="unique_transcript_event_utterance_ref_per_bot",
            ),
        ),

        # ── ActionLogEntry ──
        migrations.CreateModel(
            name="ActionLogEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("turn_id", models.UUIDField(db_index=True)),
                ("tool_name", models.CharField(db_index=True, max_length=128)),
                ("tool_input", models.JSONField(default=dict)),
                ("tool_result", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(
                    choices=[("pending", "Pending"), ("ok", "OK"), ("error", "Error")],
                    default="pending",
                    max_length=16,
                )),
                ("latency_ms", models.IntegerField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, default="")),
                ("is_archived", models.BooleanField(db_index=True, default=False)),
                ("bot", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="action_log",
                    to="bots.bot",
                    to_field="object_id",
                )),
                ("occurrence", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="action_log",
                    to="agent.meetingoccurrence",
                )),
                ("trigger_start_event", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+",
                    to="agent.transcriptevent",
                )),
                ("trigger_end_event", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+",
                    to="agent.transcriptevent",
                )),
            ],
            options={
                "verbose_name": "Action Log Entry",
                "verbose_name_plural": "Action Log Entries",
                "db_table": "agent_action_log_entry",
                "ordering": ["created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="actionlogentry",
            index=models.Index(fields=["bot", "created_at"], name="agent_actio_bot_id_a41b3c_idx"),
        ),
        migrations.AddIndex(
            model_name="actionlogentry",
            index=models.Index(fields=["turn_id"], name="agent_actio_turn_id_d92e7f_idx"),
        ),

        # ── VoiceContextPush ──
        migrations.CreateModel(
            name="VoiceContextPush",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("session_handle", models.CharField(blank=True, default="", max_length=512)),
                ("text", models.TextField()),
                ("triggered_by_turn_id", models.UUIDField(blank=True, null=True)),
                ("bot", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="voice_context_pushes",
                    to="bots.bot",
                    to_field="object_id",
                )),
            ],
            options={
                "verbose_name": "Voice Context Push",
                "db_table": "agent_voice_context_push",
                "ordering": ["-created_at"],
            },
        ),

        # ── MeetingCursor ──
        migrations.CreateModel(
            name="MeetingCursor",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("cursor_event_time", models.DateTimeField(blank=True, null=True)),
                ("cursor_event_created_at", models.DateTimeField(blank=True, null=True)),
                ("last_turn_id", models.UUIDField(blank=True, null=True)),
                ("last_turn_at", models.DateTimeField(blank=True, null=True)),
                ("voice_session_handle", models.CharField(blank=True, default="", max_length=512)),
                ("voice_session_opened_at", models.DateTimeField(blank=True, null=True)),
                ("audio_gate_open", models.BooleanField(default=False)),
                ("audio_gate_opened_at", models.DateTimeField(blank=True, null=True)),
                ("audio_gate_reason", models.CharField(blank=True, default="", max_length=64)),
                ("total_cost_usd", models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ("budget_cap_usd", models.DecimalField(decimal_places=2, default=10, max_digits=10)),
                ("budget_exceeded", models.BooleanField(default=False)),
                ("bot", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    primary_key=True,
                    related_name="agent_cursor",
                    serialize=False,
                    to="bots.bot",
                    to_field="object_id",
                )),
            ],
            options={
                "verbose_name": "Meeting Cursor",
                "db_table": "agent_meeting_cursor",
            },
        ),
    ]
