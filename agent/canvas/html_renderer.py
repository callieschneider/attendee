"""
HTML → PNG renderer using Selenium (already a dep for bot control).

Haiku generates full self-contained HTML. We spin up a headless Chrome,
render it at 640x360 (right pane of canvas), screenshot, return PNG bytes.

Falls back to the Pillow renderer if Selenium isn't available or fails.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time

log = logging.getLogger("agent.canvas.html_renderer")

CANVAS_W = 620
CANVAS_H = 680


def render_html_to_png(html: str) -> bytes | None:
    """
    Render an HTML string to a PNG at CANVAS_W × CANVAS_H.
    Returns PNG bytes on success, None on failure.
    """
    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument(f"--window-size={CANVAS_W},{CANVAS_H}")
        options.add_argument("--hide-scrollbars")
        options.add_argument("--force-device-scale-factor=1")

        # Try to find chromedriver — it's installed by Attendee's Dockerfile
        for driver_path in [
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
            "chromedriver",
        ]:
            if os.path.exists(driver_path) or driver_path == "chromedriver":
                try:
                    service = Service(executable_path=driver_path)
                    driver = webdriver.Chrome(service=service, options=options)
                    break
                except Exception:
                    continue

        if driver is None:
            log.warning("html_renderer: no chromedriver found")
            return None

        driver.set_window_size(CANVAS_W, CANVAS_H)

        # Write HTML to temp file and load
        with tempfile.NamedTemporaryFile(
            suffix=".html", mode="w", encoding="utf-8", delete=False
        ) as f:
            f.write(html)
            tmp_path = f.name

        try:
            driver.get(f"file://{tmp_path}")
            time.sleep(0.3)  # let JS/CSS settle
            png = driver.get_screenshot_as_png()
            return png
        finally:
            os.unlink(tmp_path)

    except Exception:
        log.exception("html_renderer: failed")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
