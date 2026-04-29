"""
BrowserSession — per-bot headless Chrome the agent can drive.

Phase 2 of the browser-automation plan. Each LiveSessionManager owns
(at most) one BrowserSession, instantiated lazily on the first
`page_*` tool call. The session:

- Spawns a headless Chrome via Selenium (chromedriver is in the
  Docker image, same path the canvas pump uses).
- Runs a screencast pump task that publishes PNG screenshots to
  `canvas:browser:{bot_id}` Redis pubsub at ~2 Hz, only when a
  fresh frame's bytes differ from the previous one (digest skip,
  same trick as the canvas pump).
- Serializes every driver call through an asyncio.Lock so concurrent
  agent tool calls don't trample each other in the Selenium WebDriver
  HTTP transport.

Selenium itself is sync. Every driver call runs through
`loop.run_in_executor` so the bridge's asyncio loop doesn't block.

Lifecycle:
- Created lazily by LiveSessionManager.get_browser_session().
- Survives Gemini WS reopens (it's bound to the manager, which lives
  for the whole Attendee WS connection).
- Killed in LiveSessionManager._shutdown via close().
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Optional

log = logging.getLogger("agent.browser_session")


VIEWPORT_W = 1280
VIEWPORT_H = 800
SCREENCAST_HZ = 2.0
SCREENSHOT_QUALITY = 60  # PNGs don't honor quality but kept for future jpeg switch
TEXT_RETURN_BUDGET = 8000


_CHROMEDRIVER_PATHS = (
    "/usr/local/bin/chromedriver",
    "/usr/bin/chromedriver",
    "chromedriver",
)


class BrowserSessionError(RuntimeError):
    """Raised on driver-level failures the caller should surface as a tool error."""


class BrowserSession:
    """One headless Chrome per bot. Drive via async tool methods."""

    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self._driver = None
        self._lock = asyncio.Lock()
        self._screencast_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._closed = False
        self._last_digest = ""
        self._redis = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def ensure(self) -> None:
        if self._driver is not None or self._closed:
            return
        loop = asyncio.get_event_loop()
        self._driver = await loop.run_in_executor(None, self._build_driver)
        if self._driver is None:
            raise BrowserSessionError("could not start chromedriver")
        self._stop_event = asyncio.Event()
        self._screencast_task = asyncio.create_task(
            self._screencast_loop(), name=f"browser-screencast-{self.bot_id}"
        )
        log.info("browser_session: started bot=%s", self.bot_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._screencast_task:
            try:
                self._screencast_task.cancel()
            except Exception:
                pass
            self._screencast_task = None
        if self._driver is not None:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._driver.quit)
            except Exception:
                log.exception("browser_session: driver.quit failed bot=%s", self.bot_id)
            self._driver = None
        # Tell the canvas to drop any cached screencast frame.
        self._publish_frame_event({"event": "screencast_stop"})
        log.info("browser_session: closed bot=%s", self.bot_id)

    def _build_driver(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except Exception:
            log.exception("browser_session: selenium import failed")
            return None

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument(f"--window-size={VIEWPORT_W},{VIEWPORT_H}")
        options.add_argument("--hide-scrollbars")
        options.add_argument("--force-device-scale-factor=1")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        )

        for path in _CHROMEDRIVER_PATHS:
            if not (os.path.exists(path) or path == "chromedriver"):
                continue
            try:
                drv = webdriver.Chrome(service=Service(executable_path=path), options=options)
                drv.set_window_size(VIEWPORT_W, VIEWPORT_H)
                drv.set_page_load_timeout(30)
                drv.implicitly_wait(0)  # we manage waits explicitly
                return drv
            except Exception:
                continue
        log.warning("browser_session: no chromedriver found on disk")
        return None

    # ── Tool primitives (all sync work in executor under self._lock) ────────

    async def _run(self, fn, *args, **kwargs):
        """Serialize sync driver calls behind the asyncio lock."""
        await self.ensure()
        loop = asyncio.get_event_loop()
        async with self._lock:
            try:
                return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
            except Exception as exc:
                log.exception("browser_session: %s failed bot=%s", fn.__name__, self.bot_id)
                raise BrowserSessionError(f"{type(exc).__name__}: {exc}") from exc

    async def navigate(self, url: str) -> dict:
        def _go():
            self._driver.get(url)
            return {
                "url": self._driver.current_url,
                "title": self._driver.title or "",
            }
        out = await self._run(_go)
        return {"ok": True, **out}

    async def click_text(self, text: str, *, case_sensitive: bool = False) -> dict:
        from selenium.webdriver.common.by import By

        def _click():
            t = text.strip()
            if not t:
                raise BrowserSessionError("text required")
            # Prefer accessible name match (button/link). Fall back to any element.
            xpaths = (
                f"//*[self::a or self::button or @role='button' or @role='link']"
                f"[normalize-space(string(.))='{_xp_lit(t)}']",
                # Case-insensitive contains as a softer fallback
                f"//*[self::a or self::button or @role='button' or @role='link']"
                f"[contains(translate(normalize-space(string(.)),"
                f" 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                f" '{_xp_lit(t.lower())}')]",
                # Last resort: any element with that visible text
                f"//*[normalize-space(string(.))='{_xp_lit(t)}']",
                f"//*[contains(translate(normalize-space(string(.)),"
                f" 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                f" '{_xp_lit(t.lower())}')]",
            )
            if case_sensitive:
                xpaths = (xpaths[0], xpaths[2])
            for xp in xpaths:
                try:
                    elements = self._driver.find_elements(By.XPATH, xp)
                except Exception:
                    elements = []
                for el in elements:
                    try:
                        if not el.is_displayed():
                            continue
                        self._driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center', inline:'center'});", el,
                        )
                        el.click()
                        return {
                            "matched": "text",
                            "element_text": (el.text or "")[:200],
                            "url": self._driver.current_url,
                        }
                    except Exception:
                        continue
            raise BrowserSessionError(f"no clickable element with text {text!r}")

        out = await self._run(_click)
        return {"ok": True, **out}

    async def click_selector(self, selector: str) -> dict:
        from selenium.webdriver.common.by import By

        def _click():
            sel = selector.strip()
            if not sel:
                raise BrowserSessionError("selector required")
            els = self._driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    self._driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});", el,
                    )
                    el.click()
                    return {
                        "matched": "selector",
                        "element_text": (el.text or "")[:200],
                        "url": self._driver.current_url,
                    }
                except Exception:
                    continue
            raise BrowserSessionError(f"no clickable element matching {sel!r}")

        out = await self._run(_click)
        return {"ok": True, **out}

    async def type_in(self, selector: str, value: str, *, submit: bool = False) -> dict:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        def _type():
            sel = selector.strip()
            if not sel:
                raise BrowserSessionError("selector required")
            el = None
            for candidate in self._driver.find_elements(By.CSS_SELECTOR, sel):
                if candidate.is_displayed():
                    el = candidate
                    break
            if el is None:
                raise BrowserSessionError(f"no visible element matching {sel!r}")
            try:
                el.clear()
            except Exception:
                pass
            el.send_keys(value)
            if submit:
                el.send_keys(Keys.RETURN)
            return {
                "value_after": (el.get_attribute("value") or el.text or "")[:400],
                "submitted": bool(submit),
                "url": self._driver.current_url,
            }

        out = await self._run(_type)
        return {"ok": True, **out}

    async def scroll(self, direction: str, pixels: int = 800) -> dict:
        direction = (direction or "down").lower()
        if direction not in ("up", "down", "top", "bottom"):
            return {"error": "direction must be one of up|down|top|bottom"}

        def _scroll():
            if direction == "top":
                self._driver.execute_script("window.scrollTo(0, 0);")
            elif direction == "bottom":
                self._driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
            else:
                dy = pixels if direction == "down" else -pixels
                self._driver.execute_script(f"window.scrollBy(0, {int(dy)});")
            return {"scroll_y": int(self._driver.execute_script("return window.scrollY;"))}

        out = await self._run(_scroll)
        return {"ok": True, **out}

    async def press(self, key: str) -> dict:
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains

        key_map = {
            "enter": Keys.RETURN,
            "return": Keys.RETURN,
            "tab": Keys.TAB,
            "escape": Keys.ESCAPE,
            "esc": Keys.ESCAPE,
            "space": Keys.SPACE,
            "backspace": Keys.BACKSPACE,
            "arrowdown": Keys.ARROW_DOWN,
            "arrowup": Keys.ARROW_UP,
            "arrowleft": Keys.ARROW_LEFT,
            "arrowright": Keys.ARROW_RIGHT,
        }
        k = key_map.get((key or "").strip().lower())
        if k is None:
            return {"error": f"unsupported key {key!r}"}

        def _press():
            ActionChains(self._driver).send_keys(k).perform()
            return {"key": key}

        out = await self._run(_press)
        return {"ok": True, **out}

    async def get_text(self, selector: Optional[str] = None) -> dict:
        from selenium.webdriver.common.by import By

        def _text():
            if selector:
                els = self._driver.find_elements(By.CSS_SELECTOR, selector)
                if not els:
                    raise BrowserSessionError(f"no element matching {selector!r}")
                txt = els[0].text or ""
            else:
                txt = self._driver.execute_script(
                    "return document.body && document.body.innerText || '';"
                ) or ""
            return {"text": txt[:TEXT_RETURN_BUDGET], "truncated": len(txt) > TEXT_RETURN_BUDGET}

        out = await self._run(_text)
        return {"ok": True, **out}

    async def back(self) -> dict:
        def _back():
            self._driver.back()
            return {"url": self._driver.current_url}
        out = await self._run(_back)
        return {"ok": True, **out}

    async def reload(self) -> dict:
        def _reload():
            self._driver.refresh()
            return {"url": self._driver.current_url}
        out = await self._run(_reload)
        return {"ok": True, **out}

    async def status(self) -> dict:
        def _status():
            return {
                "url": self._driver.current_url,
                "title": self._driver.title or "",
                "viewport": [VIEWPORT_W, VIEWPORT_H],
            }
        out = await self._run(_status)
        return {"ok": True, **out}

    # ── Screencast ──────────────────────────────────────────────────────────

    async def _screencast_loop(self) -> None:
        interval = 1.0 / SCREENCAST_HZ
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    png = await self._capture_png()
                    if png is not None:
                        digest = hashlib.sha256(png).hexdigest()
                        if digest != self._last_digest:
                            self._last_digest = digest
                            self._publish_frame_event({
                                "event": "screencast_frame",
                                "png_b64": base64.b64encode(png).decode(),
                                "ts": int(time.time() * 1000),
                            })
                except Exception:
                    log.exception(
                        "browser_session: screencast tick failed bot=%s",
                        self.bot_id,
                    )
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.05, interval - elapsed))
        except asyncio.CancelledError:
            return

    async def _capture_png(self) -> Optional[bytes]:
        if self._driver is None:
            return None
        loop = asyncio.get_event_loop()
        async with self._lock:
            try:
                # Selenium >=4 supports get_screenshot_as_png(); falls back to b64
                return await loop.run_in_executor(None, self._driver.get_screenshot_as_png)
            except Exception:
                log.exception("browser_session: screenshot failed bot=%s", self.bot_id)
                return None

    def _publish_frame_event(self, payload: dict) -> None:
        r = self._get_redis()
        if r is None:
            return
        try:
            r.publish(f"canvas:browser:{self.bot_id}", json.dumps(payload))
        except Exception:
            log.exception(
                "browser_session: publish failed bot=%s", self.bot_id,
            )

    def _get_redis(self):
        if self._redis is not None:
            return self._redis
        try:
            import redis  # noqa: F401
        except Exception:
            return None
        url = (
            os.getenv("REDIS_URL")
            or os.getenv("CELERY_BROKER_URL")
            or "redis://localhost:6379/0"
        )
        try:
            import redis as _r
            self._redis = _r.from_url(url, decode_responses=True)
            return self._redis
        except Exception:
            log.exception("browser_session: redis connect failed")
            return None


def _xp_lit(value: str) -> str:
    """Escape a string for safe inclusion in an XPath literal."""
    return value.replace("'", "&apos;").replace('"', "&quot;")


# ── Per-bot registry ────────────────────────────────────────────────────────
#
# The bridge's LiveSessionManager registers/unregisters the BrowserSession
# here so the page_* tools (which run in the same process but don't
# directly hold a manager reference) can look it up by bot_id.

_SESSIONS: dict[str, BrowserSession] = {}


def get_or_create(bot_id: str) -> BrowserSession:
    bs = _SESSIONS.get(bot_id)
    if bs is None or bs._closed:
        bs = BrowserSession(bot_id)
        _SESSIONS[bot_id] = bs
    return bs


def get(bot_id: str) -> Optional[BrowserSession]:
    bs = _SESSIONS.get(bot_id)
    if bs is None or bs._closed:
        return None
    return bs


async def close_for_bot(bot_id: str) -> None:
    bs = _SESSIONS.pop(bot_id, None)
    if bs is None:
        return
    try:
        await bs.close()
    except Exception:
        log.exception("browser_session: close_for_bot failed bot=%s", bot_id)


# ── Bridge-loop bridge ──────────────────────────────────────────────────────
#
# Tool handlers run via Django's sync_to_async, which puts them on a thread
# pool. To call BrowserSession's async methods we have to schedule the
# coroutine back onto the bridge's main asyncio loop. The bridge calls
# `set_bridge_loop` on startup; tool handlers then use `run_coro_sync`.

_BRIDGE_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_bridge_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _BRIDGE_LOOP
    _BRIDGE_LOOP = loop


def run_coro_sync(coro, *, timeout: float = 35.0):
    """
    Submit a coroutine to the bridge's loop and wait for it. Intended for
    use from sync tool handlers that need to call into BrowserSession.
    Raises BrowserSessionError on timeout or if the loop isn't set yet.
    """
    if _BRIDGE_LOOP is None:
        raise BrowserSessionError(
            "bridge loop not registered (page tools only work in the bridge process)"
        )
    fut = asyncio.run_coroutine_threadsafe(coro, _BRIDGE_LOOP)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeoutError:
        try:
            fut.cancel()
        except Exception:
            pass
        raise BrowserSessionError(f"page tool timed out after {timeout:.1f}s")


