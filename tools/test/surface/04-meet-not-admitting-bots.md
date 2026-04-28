# Surface: bots can't join Meet (name_input UI element not found)

**When:** 2026-04-28 ~05:05 PT.

**Symptom:** Every fresh bot dispatched after ~11:18 UTC (4:18 AM PT)
fails to join the meeting at the "name_input" step — Attendee's
Selenium driver can't locate the join-page name field, times out, and
the bot exits.

Two affected bots:
- `bot_jz4dJLYCuEIV3Ylv` (12:00 UTC)
- `bot_kmUYoSZy5ENE8mbu` (12:04 UTC)

**Where the failure lives:** inside the Attendee fork at
`bots/web_bot_adapter/web_bot_adapter.py` (or one of the join-flow
helpers it calls). I deliberately did not touch this file beyond the
already-surfaced ASCII tab-title fix because the plan rules say the
Attendee fork is sacred and changes there get surfaced.

**What I think happened:** Most likely Google ended the meeting room
(it's a personal-account Meet so it auto-closes after a while of
inactivity) OR Callie left and locked the room. The bot lands on the
sign-in page instead of the join page, and the join-flow's element
search blows up.

A less likely but possible cause: Google rolled out a UI change to
the Meet join page that the Attendee join-flow hasn't been updated
for. If you see this happening to ALL meet URLs starting tomorrow,
that's likely it.

**What I need from you (the human) to unblock:**

Reply with a fresh, joinable Google Meet URL when you wake up:
- Open Meet
- Start an instant meeting OR start one with a fresh code
- Set room policy to "anyone with link can join" if your account
  defaults to "knock to join"
- Send me the URL

I'll plug it into `tools/test/.env.harness` (TEST_MEET_URL) and
restart the supervisor.

**What I shipped before this blocker hit:**

Tool reliability run on `bot_EUqkHa8rddR1ywK4` (the bot that succeeded
joining at 11:14 UTC) — see `tools/test/runs/20260428-111604/`:
- 7/8 cases PASS on first run
- 1 fail: `p1.dashboard.update` — agent didn't call update_dashboard
- Fix shipped in commit 0ad57ba1: prompt now coaches the tool, plus
  the Dashboard tab UI now actually renders dashboard_payload entries
  (it ignored them before).

Also shipped in commits afterwards (NOT yet verified end-to-end
against a live bot because the meet stopped admitting):
- ASCII-only canvas tab title for screenshare auto-select match
  (commit 0e217370)
- Env-tunable echo suppression knobs (commit 115ed312)
- Two follow-up fixes for crashes I introduced (commits 09ebac70,
  44010775) — see `surface/01-blackhole-needs-sudo.md` for the audio
  routing setup if you want to go after the echo loop next.
