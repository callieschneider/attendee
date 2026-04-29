// MAIN-world inject script. Overrides
// navigator.mediaDevices.getDisplayMedia so that when Meet asks for a
// screen-share source we return a real tab-capture MediaStream of the
// Clever Star canvas tab instead of whatever Chrome's fake-device
// flag would give us.
//
// Flow:
//   1. Meet calls navigator.mediaDevices.getDisplayMedia(...)
//   2. We postMessage to content.js requesting a tabCapture streamId
//   3. content.js forwards to background.js, which calls
//      chrome.tabCapture.getMediaStreamId({targetTabId: canvasTabId})
//   4. We materialize via getUserMedia({video:{mandatory:
//      {chromeMediaSource:'tab', chromeMediaSourceId}}})
//   5. Return that real MediaStream to Meet.
//
// chrome.tabCapture-derived streams are accepted by Meet's WebRTC
// stack the same way a normal screen share would be — the rejection
// we previously hit was because canvas.captureStream() output isn't
// recognized as a valid screen source.

"use strict";

(function () {
  if (window.__cleverstarInjected) return;
  window.__cleverstarInjected = true;

  const CALL_LOG = [];
  window.__cleverstar_override_log = CALL_LOG;
  function log(msg, extra) {
    const entry = { t: Date.now(), msg };
    if (extra !== undefined) entry.extra = extra;
    CALL_LOG.push(entry);
    if (CALL_LOG.length > 200) CALL_LOG.shift();
    try { console.log("[cleverstar]", msg, extra || ""); } catch (e) {}
  }

  let bridgeReady = false;
  const pending = new Map();
  let nextId = 1;

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== "cleverstar-content") return;
    if (data.event === "ready") {
      bridgeReady = true;
      log("bridge ready");
      return;
    }
    const { requestId, response } = data;
    const resolve = pending.get(requestId);
    if (resolve) {
      pending.delete(requestId);
      resolve(response);
    }
  });

  function callExtension(type, timeoutMs) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, resolve);
      window.postMessage({ source: "cleverstar-inject", requestId: id, type }, "*");
      setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id);
          reject(new Error("extension bridge timed out after " + timeoutMs + "ms"));
        }
      }, timeoutMs || 5000);
    });
  }

  const md = navigator.mediaDevices;
  if (!md || typeof md.getDisplayMedia !== "function") {
    log("getDisplayMedia not present at inject time");
    return;
  }

  const original = md.getDisplayMedia.bind(md);

  md.getDisplayMedia = async function (constraints) {
    log("getDisplayMedia called", { constraints });
    try {
      const resp = await callExtension("capture-canvas-tab", 5000);
      log("bridge response", resp);
      if (!resp || resp.error || !resp.streamId) {
        const err = new Error("cleverstar tab-capture failed: " + (resp && resp.error || "no streamId"));
        log("falling back to original getDisplayMedia", err.message);
        return await original(constraints);
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          mandatory: {
            chromeMediaSource: "tab",
            chromeMediaSourceId: resp.streamId,
            maxWidth: 1920,
            maxHeight: 1080,
            maxFrameRate: 30,
          },
        },
      });
      log("got tab-capture stream", {
        videoTracks: stream.getVideoTracks().length,
        label: (stream.getVideoTracks()[0] || {}).label,
      });

      // Some Meet versions inspect the track for a "displaySurface" hint.
      // Tag it as 'browser' (a tab) so Meet treats it like a normal
      // tab share.
      try {
        stream.getVideoTracks().forEach((t) => {
          if (typeof t.applyConstraints === "function") {
            t.applyConstraints({ width: 1920, height: 1080 }).catch(() => {});
          }
        });
      } catch (e) {}

      return stream;
    } catch (e) {
      log("override threw, falling back", String(e && e.message || e));
      return await original(constraints);
    }
  };

  log("getDisplayMedia override installed");
})();
