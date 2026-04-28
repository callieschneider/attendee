# Surface: BlackHole install needs sudo password

**When:** 2026-04-28 ~03:55 PT, during overnight run.

**What:** I tried `brew install --cask blackhole-2ch` but it requires a
sudo password I can't enter from the headless shell. The installer
itself also requires a reboot to load the audio driver.

**Impact:** Layer 3 of the test harness (real Meet audio with TTS via
virtual mic) is blocked until Callie does this manually. Layer 1 (text
injection) and Layer 2 (bridge audio) still work, so I'm pressing on
with priority-1 tool reliability + priority-2 screenshare diagnosis
during the overnight run.

**Action you need to take (whenever convenient):**

1. `brew install --cask blackhole-2ch`
2. Enter sudo password when prompted
3. Reboot
4. Follow `tools/test/SETUP_FOR_CALLIE.md` for the Multi-Output
   Device + Meet mic configuration
5. Reply "audio loop is live"

**What I'll do when you do:** Run the priority-3 echo-loop cases via
Layer 3 with real Meet audio, A/B the candidate fixes
(`_bot_speaking_until` tail, Gemini Live `silenceDurationMs`,
audio-pump gating predicate), commit the winner.
