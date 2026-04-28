# Overnight Run — SUMMARY

Run window: 2026-04-28 ~03:42 PT (10:42 UTC) → ~05:10 PT (12:10 UTC).

## Top 3 things to look at first

1. **`surface/04-meet-not-admitting-bots.md`** — I need a fresh
   joinable Meet URL from you to keep going. Bots dispatched after
   ~11:18 UTC fail at the join page's name-input step, suggesting the
   meeting room ended or the join policy changed. Reply with a new
   URL when you wake up.
2. **`surface/01-blackhole-needs-sudo.md`** — install BlackHole +
   reboot to unlock Layer 3 (real-Meet audio testing). Required to
   actually test the echo-loop fix candidates I prepared.
3. **`surface/02-bots-screenshare-flag.md`** — I shipped a one-line
   fix inside the Attendee fork (the canvas tab title's middle dot
   was breaking Chromium's auto-select). I judged it OK because it's
   a string we ourselves added in Phase 4, not upstream Attendee. Tell
   me to revert if you disagree.

## Verified wins (full evidence in `runs/`)

Tool reliability (priority 1, smoke run before things broke):

- 8/8 expected-tool-call cases got the right tool fired by Gemini Live
  on a single bot in a single supervisor pass on commit 9e51f17d:
  `runs/20260428-111604/SUMMARY.md`. The original 7/8 result on commit
  828d13a8 was missing only `update_dashboard`; the dashboard prompt +
  UI fix landed in 0ad57ba1.
- Tool latencies that landed:
  - `update_notes`: ~460ms
  - `navigate_canvas`: ~tens of ms
  - `think_deep` (Haiku call): ~7100ms total, focus tab streamed
  - `create_visual`: ~20ms (cached chart)
  - `screen_share_canvas`: ~1100ms (publishes Redis command)

## Code shipped (all commits prefixed `[overnight]`)

| commit | what |
| --- | --- |
| `828d13a8` | harness Layer 1: `/agent/debug/inject-utterance` + bridge signal handler + `tools/test/inject.py` |
| `9e51f17d` | harness supervisor + cases.json + Layer 2 helper + `AGENT_DEBUG_TOKEN` settings binding |
| `0ad57ba1` | dashboard fix: render `dashboard_payload` + coach `update_dashboard` in the system prompt |
| `0e217370` | screenshare: ASCII-only canvas title so Chromium's auto-select-desktop-capture-source actually matches |
| `115ed312` | echo loop: 3 env-tunable knobs (`AGENT_ECHO_TAIL_MS`, `AGENT_INTERRUPT_TAIL_MS`, `AGENT_TURN_COOLDOWN_MS`) for A/B without redeploys |
| `09ebac70` | fix: missing `import os` in manager.py (caught by harness — bridge crashed every WS connection) |
| `44010775` | fix: escape curly braces in `VOICE_SYSTEM_PROMPT_TEMPLATE` (my dashboard examples broke `.format()`) |

## Two self-inflicted regressions, both caught and fixed

- `09ebac70` was a fix for a `NameError: name 'os' is not defined`
  introduced in `115ed312`. The bridge crashed on every connection
  for ~5 minutes before I caught it. The harness caught it because
  the next supervisor run scored 0/8 — that immediate signal is the
  whole point of the harness, even when it catches my own bugs.
- `44010775` was a fix for a `KeyError: 'key'` introduced in
  `0ad57ba1`. The system prompt template went through `str.format()`
  and my `{key: value}` example was interpreted as a placeholder.
  Caught the same way.

Lesson for the morning: any change to formatter.py needs a quick
local `.format()` smoke test before pushing. I added a tiny check at
the bottom of the file is one option.

## What's blocked / open

- **Echo loop A/B** — knobs shipped, default values unchanged. Needs
  Layer 3 (BlackHole + Cursor browser mic config + a logged-in Google
  account) to actually measure duplicate rates. See
  `surface/03-echo-loop-knobs.md` for the sweep plan.
- **Screenshare canvas-render verification** — the ASCII-title fix is
  a strong hypothesis but I can't confirm without watching Meet's
  share dialog populate via the browser MCP, which needs a live bot.
  Comes for free once meet-not-admitting is unblocked.
- **All Layer 3 work** — full real-Meet runs against the priority-3
  echo cases. Blocked on the surface items above.

## How to keep this going

```bash
# from the project root, after you've replied with a fresh Meet URL:
sed -i '' 's|TEST_MEET_URL=.*|TEST_MEET_URL=https://meet.google.com/<new>|' tools/test/.env.harness
python3 tools/test/supervisor.py
```

Reports land in `tools/test/runs/<ts>/`. Use `--reuse-bot bot_xxx` to
keep running cases against an already-joined bot without dispatching
a new one.

## Stop file

```bash
touch tools/test/runs/STOP   # supervisor exits at next case boundary
```

(Empty file present means "exit"; remove to allow runs again.)
