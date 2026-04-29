# Surface: video input to Gemini Live (other participants + own screenshare)

**When:** 2026-04-28 ~22:35 PT.

**Your ask:** "Ideally [the agent] can see everything we can see…
including the ability to see screen shares from other users. And the
ability to see its own screen share."

**What I shipped tonight (text-side):**
- `get_canvas_content` — agent reads every canvas tab's current text.
- `get_browser_screenshot` — base64 PNG of its own headless Chrome
  page. Gemini Live can ingest images, so this works today.

**What's surfaced (not built yet):** **streaming Meet video into
Gemini Live so the agent can see other participants' webcams and
their screenshares.** This is the "see what we see" piece.

## Architecture

```
Meet -> Attendee bot's Chrome -> capture frames at N fps
     -> bridge re-encodes to JPEG ~80% quality
     -> bridge sends to Gemini Live as realtimeInput.video
     -> Gemini incorporates into its multi-modal context
```

abstraKt does this exact pattern in
`/tmp/abstrakt-ref/src/lib/utils/gemini-live.ts:565` — `sendRealtimeJpeg`.
The model side already accepts it. The work is the capture pipeline.

## Cost analysis

Gemini Live charges by image-input tokens. Per Google's docs each
image is ~258 tokens for the Live API (resolved image, default
resolution).

| FPS | Tokens/min | Tokens/hr | $/hr (Flash Live preview, $0.15/1M input) |
|-----|-----------|-----------|----|
| 0.5 | 7,740 | 464,400 | $0.07 |
| 1   | 15,480 | 928,800 | $0.14 |
| 2   | 30,960 | 1,857,600 | $0.28 |
| 4   | 61,920 | 3,715,200 | $0.56 |

Plus output (audio + tool calls) — same as today.

For a 30-min meeting at 1 fps: ~7c extra. At 4 fps: ~28c extra.
Manageable for a single user.

## Two-source decision

There's actually **three** distinct video streams worth caring about,
not two:

1. **Other participants' webcams + their screenshares** — this is what
   the bot's Meet tab is rendering in its viewport. Capturing the bot's
   Chrome viewport gives us all of this in one image.
2. **The bot's own canvas content** — the agent already drives this
   (`get_canvas_content`, `get_browser_screenshot`). No new pipeline
   needed.
3. **The bot's own screenshare** — same content as the canvas tab,
   plus any browser-tab screencast. Also already accessible via
   existing tools.

So #1 is the only NEW source we need to wire. Capture the bot's
**Meet tab viewport**, send to Gemini.

## Implementation sketch

Three pieces, all in the bridge:

**a. `MeetVideoCapture` class.** Selenium's `driver.get_screenshot_as_png()`
on the bot's Meet tab handle, in a loop at configurable FPS. Same
pattern as the existing canvas pump.

**b. Throttle / change detection.** Skip frames whose hash matches
the previous frame (same trick as canvas pump — Meet's tile is
mostly static when nobody's talking, no point sending duplicates).

**c. `realtimeInput.video` send.** Add to LiveSessionManager: every
captured frame, base64-encode and send `{realtimeInput:
{video: {mimeType: "image/jpeg", data: b64}}}` over the Gemini WS.

Configurable via env:
- `AGENT_MEET_VIDEO_ENABLED` (default off — cost guardrail)
- `AGENT_MEET_VIDEO_FPS` (default 1)
- `AGENT_MEET_VIDEO_QUALITY` (default 70)

## Risks / open questions

1. **Bandwidth.** ~80KB/frame × 1 fps × bridge → Google = 80KB/s
   outbound from Railway. Trivial in absolute terms; just want it
   logged so we'd notice if a runaway loop spams.
2. **Privacy / consent.** Sending other participants' video to a
   third party (Google) without their explicit consent is a real
   thing in some jurisdictions. For your single-user, single-meeting
   case it's a personal choice; if/when this expands you'll want a
   visible "this bot sees your video" indicator on the canvas.
3. **Capture target.** Where exactly to capture from. Two options:
   - Selenium screenshot of the bot's Meet tab (full page including
     headers, sidebars). Simple. ~150ms per capture.
   - CDP `Page.captureScreenshot` with viewport clipping. Crisper,
     ~50ms. More code.
4. **Coordination with screen-share-out.** The bot can already share
   its canvas (Phase 4). Capturing the Meet viewport while presenting
   could trigger a feedback loop where the agent sees its own
   share. Mitigate by clipping out the share-thumbnail region or by
   pausing video-in while the bot is presenting.

## Recommendation

Build it as an opt-in feature gated by `AGENT_MEET_VIDEO_ENABLED=1`.
Default 1 fps. Add a brief "Cleverstar can see the meeting" badge to
the canvas header so the user knows. Estimated 2-3 hours of focused
work.

**Reply with one of:**
- "yes build it" — I'll spec it as a proper plan and execute next.
- "yes build it now" — I'll just go (smaller than Phase 2).
- "wait, let me think about cost first."
- "skip — text-context is enough."
