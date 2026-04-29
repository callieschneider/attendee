# Surface: bots/ change for canvas-link-on-join

**When:** 2026-04-28 ~22:55 PT.

**What:** Added `_post_canvas_link_to_chat_safely` to
`bots/bot_controller/bot_controller.py`. Right after the bot is
admitted to the meeting (`BOT_RECORDING_PERMISSION_GRANTED`) and
the canvas tab is opened in its Chrome, schedule a
`threading.Timer(6.0, ...)` that POSTs the canvas URL into the
meeting chat via Attendee's existing
`/api/v1/bots/<id>/send_chat_message` endpoint.

**Why I shipped it without surfacing first:** explicit user request
in real-time test ("should also send the direct canvas link in the
chat when it joins a session"). Single new method, doesn't touch
existing flow.

**What it adds to the chat:**
```
Clever Star canvas: https://meeting-agent-web-production.up.railway.app/agent/canvas/v2/bot_xxx/
```

**Risks:** the bot may not be ready to send chat at +6s if Meet's
admit sequence is slow. Failure is logged at warning level and
non-fatal. If we see the message often missing, bump the timer or
poll `is_ready_to_send_chat_messages()`.

**Reply:** "ok" / "revert" / "different message text".
