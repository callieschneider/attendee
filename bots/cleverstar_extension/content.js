// Content script (ISOLATED world). Bridge between the MAIN-world
// inject.js (which can hijack navigator.mediaDevices.getDisplayMedia)
// and the extension's service worker (which has chrome.tabCapture).
//
// MAIN-world scripts can't call chrome.* APIs, and ISOLATED-world
// content scripts can't see/modify the page's navigator object. So
// inject.js postMessages here, we forward to the service worker via
// chrome.runtime.sendMessage, and post the response back.

"use strict";

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "cleverstar-inject") return;
  const { requestId, type } = data;
  try {
    chrome.runtime.sendMessage({ type }, (response) => {
      const err = chrome.runtime.lastError;
      window.postMessage(
        {
          source: "cleverstar-content",
          requestId,
          response: err ? { error: err.message } : response,
        },
        "*"
      );
    });
  } catch (e) {
    window.postMessage(
      {
        source: "cleverstar-content",
        requestId,
        response: { error: String(e && e.message || e) },
      },
      "*"
    );
  }
});

// Handshake: let inject.js know the bridge is alive. inject.js can
// listen for this and not assume the extension is missing.
window.postMessage({ source: "cleverstar-content", event: "ready" }, "*");
