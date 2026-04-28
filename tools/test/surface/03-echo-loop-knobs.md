# Surface: echo loop fix candidates ready, need Layer 3 to test

**When:** 2026-04-28 ~04:35 PT, during overnight run.

**Status:** I made the echo-suppression knobs env-tweakable so we can
A/B without redeploys. I did NOT pick winning values — that requires
real-Meet audio testing (Layer 3) which is blocked on BlackHole.

## What changed in code

`agent/live_session/manager.py` now reads three env vars:

| env | default | meaning |
| --- | --- | --- |
| `AGENT_ECHO_TAIL_MS` | 600 | suppression window after each bot audio frame |
| `AGENT_INTERRUPT_TAIL_MS` | 600 | extra suppression after Gemini emits an `interrupted` event |
| `AGENT_TURN_COOLDOWN_MS` | 0 | extra silence after `turnComplete` before mic re-opens |

All three apply to `_bot_speaking_until` — the timestamp the audio pump
checks before forwarding mic frames to Gemini.

## Sweep recommendation once Layer 3 is up

Run priority-3 echo cases under each setting, count duplicates:

| run | ECHO_TAIL_MS | INTERRUPT_TAIL_MS | TURN_COOLDOWN_MS |
| --- | --- | --- | --- |
| baseline | 600 | 600 | 0 |
| A | 900 | 900 | 0 |
| B | 600 | 600 | 300 |
| C | 900 | 900 | 300 |

Set vars via `railway variables --service meeting-agent-bridge --set
AGENT_ECHO_TAIL_MS=900 ...`. Bridge auto-redeploys on var change.

Other suspect knobs in `agent/gemini_live.py` (would need code edit,
already on the surface list because it's `generationConfig`):

- `realtimeInputConfig.automaticActivityDetection.silenceDurationMs`:
  raise from 250 to 400ms — Gemini waits longer before deciding the
  user has finished speaking. Less false-positive turn ends.
- `prefixPaddingMs`: raise from 60 to 120ms — Gemini waits a tiny bit
  more before deciding speech started. Helps swallow brief audio
  spikes from Meet's mix.

## Why I didn't pick a winner

Tuning these without measuring duplicate rate at a few values is
guessing. We've already paid the cost of one wrong guess (the user
reporting "my mistake" loops). Better to leave the knob and let the
A/B sweep decide.

## What you can do tonight that helps

Install BlackHole + reboot (see SETUP_FOR_CALLIE.md). When you wake,
reply "audio loop is live" and I'll run the sweep.
