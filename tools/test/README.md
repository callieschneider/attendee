# Overnight test harness

Three layers of synthetic-user testing for the meeting agent. Each layer
exercises a different slice of the stack at a different fidelity / cost.

## Layer 1 — text-only utterance injection

```
POST /agent/debug/inject-utterance
Header: X-Debug-Token: <AGENT_DEBUG_TOKEN env value>
Body:   {"bot_id": "bot_xxx", "text": "...", "speaker": "Tester"}
```

The web service publishes the utterance on the `agent:live:inject_utterance`
Redis pub/sub channel. The bridge service's `LiveSessionManager.signal_listener`
picks it up and forwards to Gemini Live as `clientContent` with
`turnComplete:true` so Gemini treats it as a finished user turn and
responds. Tool calls, canvas updates, and TTS all run normally.

CLI helper: `python tools/test/inject.py <bot_id> "your text here"`

## Layer 2 — synthetic mic over the bridge WS

`tools/test/fake_meet_user.py` connects directly to the production audio
bridge as if it were the Attendee bot, generates TTS via macOS `say`, and
streams 16k mono PCM. Tests the full Gemini Live audio path. Doesn't
reproduce Meet's audio-mixer echo (that's Layer 3).

## Layer 3 — real Meet via browser MCP + BlackHole

Driven by me (the agent) in this chat. Uses the Cursor browser MCP to join
Meet, plays TTS through BlackHole virtual audio cable so Meet picks it up
as the user's mic. This is the only layer that reproduces the echo loop
because it's the only one that goes through Meet's audio mixer.

See `SETUP_FOR_CALLIE.md` for the one-time machine setup.

## Running

```bash
# 1. Pick a meeting URL (must be a real Meet that the bot can join).
export TEST_MEET_URL="https://meet.google.com/xxx-yyyy-zzz"

# 2. Optional: cap how many cases run.
export TEST_MAX_CASES=20

# 3. Run the supervisor.
python tools/test/supervisor.py
```

Reports land in `tools/test/runs/<timestamp>/` and a final
`SUMMARY.md` is written when the loop exits.

## Stopping

```bash
touch tools/test/runs/STOP   # supervisor exits at next case boundary
```
