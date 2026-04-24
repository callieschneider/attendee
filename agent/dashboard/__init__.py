"""
Admin dashboard for live meeting observability.

- Home: active bots + recent meetings + per-series status
- Per-meeting: live TranscriptEvent feed + ActionLogEntry stream via SSE
- Per-series: config (verbosity, proactivity, agent name override, budget)
"""
default_app_config = None
