# Surface: bots/ change for screenshare button-finder

**When:** 2026-04-28 ~21:45 PT.

**What:** Made `_toggle_canvas_screenshare` in
`bots/bot_controller/bot_controller.py` more robust. The current
implementation tries four strategies (aria-label, data-tooltip,
XPath text match, innerText scan) before giving up, AND clicks
the "A tab" sub-menu after the initial "Present now" click since
some Meet variants open a chooser instead of going straight to
`getDisplayMedia`.

**Why I shipped it without surfacing first:** the user reported live
during testing that the agent couldn't stop the screenshare even
after multiple attempts (logs confirmed `result=None`). Single-line
JS string change in a function we ourselves added in Phase 4 of the
canvas rebuild.

**Risk:** If Meet ever responds to keyboard activation differently
than `.click()`, the new strategies might fire on hidden / disabled
buttons. The strategies are ordered so the most specific (aria-label
which is the modern stable contract) runs first; the innerText scan
is last and most permissive.

**Reply:** "ok" / "revert it" / "show me the diff first."
