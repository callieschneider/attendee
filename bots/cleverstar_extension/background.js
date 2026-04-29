// Service worker. Receives a "capture-canvas-tab" message from the
// content script, locates the canvas tab by title, and returns a
// chrome.tabCapture streamId that the MAIN-world inject script can
// hand to getUserMedia({video: {mandatory: {chromeMediaSource:'tab',
// chromeMediaSourceId: streamId}}}). The resulting stream is a real
// tab capture — Meet accepts it as a screen-share source.
//
// Why a service worker rather than putting tabCapture in the content
// script? chrome.tabCapture.getMediaStreamId requires extension-level
// access to chrome.tabs and chrome.tabCapture, neither of which is
// available in MAIN-world content scripts.

"use strict";

function findCanvasTab() {
  return new Promise((resolve) => {
    chrome.tabs.query({}, (tabs) => {
      // Prefer exact title match; fall back to substring.
      const exact = tabs.find(t => t.title === "Clever Star Canvas");
      if (exact) return resolve(exact);
      const substr = tabs.find(t => (t.title || "").includes("Clever Star Canvas"));
      resolve(substr || null);
    });
  });
}

chrome.runtime.onMessage.addListener((req, sender, sendResponse) => {
  if (!req || req.type !== "capture-canvas-tab") return false;
  (async () => {
    try {
      const canvasTab = await findCanvasTab();
      if (!canvasTab) {
        sendResponse({ error: "no canvas tab found (looking for title 'Clever Star Canvas')" });
        return;
      }
      const consumerTabId = sender && sender.tab ? sender.tab.id : undefined;
      chrome.tabCapture.getMediaStreamId(
        {
          targetTabId: canvasTab.id,
          consumerTabId: consumerTabId,
        },
        (streamId) => {
          if (chrome.runtime.lastError) {
            sendResponse({ error: chrome.runtime.lastError.message });
            return;
          }
          if (!streamId) {
            sendResponse({ error: "getMediaStreamId returned empty" });
            return;
          }
          sendResponse({
            streamId,
            targetTabId: canvasTab.id,
            consumerTabId,
            title: canvasTab.title,
          });
        }
      );
    } catch (e) {
      sendResponse({ error: String(e && e.message || e) });
    }
  })();
  return true; // async response
});
