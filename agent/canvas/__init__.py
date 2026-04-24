"""
Canvas — the web page Attendee renders as the bot's "video feed" (voice_agent).

The bot doesn't show a person; it shows a live view of what Clever Star is
doing: debug panel on the left (transcript + actions + gate state), and a
visualization panel on the right (the current active canvas surface).

The canvas URL is public-but-scoped by bot_id. Treat bot_id as the capability
token — only someone who knows the ID can view. Good enough for single-user.
"""
