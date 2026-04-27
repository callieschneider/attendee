"""
Canvas pump daemon — runs the canvas screenshot loop inside the web
container as a background daemon thread instead of a celery task.

Why this exists:
The original implementation queued `push_canvas_images` to celery beat
every 500ms. The worker container has CELERY_WORKER_MAX_TASKS_PER_CHILD=1
(needed for Zoom SDK segfault prevention), which means every task forks
a brand-new Python process — taking 5–10s to import Django + bots +
agent code. Scheduling 2 tasks/sec while each takes 5–10s to even
START produces an unbounded queue backlog (we observed 1791 messages
piled up in the `celery` queue with no progress on the pump).

Solution:
Run the pump as a long-lived daemon thread inside the *web* container.
The web container has the same image (so chromedriver is available)
and runs gthread gunicorn (so spawning a thread is cheap). A Redis
lock with TTL ensures only one of the N gunicorn worker processes
actually drives the pump at any time; if that worker dies the lock
expires and another worker takes over within the TTL window.

The selenium driver is reused across iterations — same long-lived
chromedriver model as the original pump module — but lives in the
gunicorn worker process, so it survives until that worker dies.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("agent.canvas.pump_daemon")


_PUMP_LOCK_KEY = "canvas:pump:lock"
_PUMP_LOCK_TTL_SECONDS = 10
_PUMP_INTERVAL_SECONDS = float(os.getenv("AGENT_CANVAS_PUMP_SECONDS", "0.5"))

_started = False
_started_lock = threading.Lock()


def start_pump_daemon() -> None:
    """Idempotent: starts the pump thread once per process."""
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    t = threading.Thread(
        target=_run_loop,
        name="canvas-pump-daemon",
        daemon=True,
    )
    t.start()
    log.info("canvas pump daemon: started thread (pid=%s, interval=%ss)", os.getpid(), _PUMP_INTERVAL_SECONDS)


def _redis_client():
    try:
        import redis
    except Exception:
        log.exception("canvas pump daemon: redis import failed")
        return None
    url = (
        os.getenv("REDIS_URL")
        or os.getenv("CELERY_BROKER_URL")
        or "redis://localhost:6379/0"
    )
    try:
        return redis.from_url(url, decode_responses=True)
    except Exception:
        log.exception("canvas pump daemon: redis connect failed")
        return None


def _try_acquire_lock(r, lock_value: str) -> bool:
    """Acquire the pump lock or refresh it if we already hold it."""
    try:
        current = r.get(_PUMP_LOCK_KEY)
        if current == lock_value:
            r.expire(_PUMP_LOCK_KEY, _PUMP_LOCK_TTL_SECONDS)
            return True
        return bool(r.set(_PUMP_LOCK_KEY, lock_value, nx=True, ex=_PUMP_LOCK_TTL_SECONDS))
    except Exception:
        log.exception("canvas pump daemon: lock acquire failed")
        return False


def _run_loop() -> None:
    import uuid
    import django

    try:
        django.setup()
    except Exception:
        pass

    lock_value = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    r = _redis_client()
    backoff = _PUMP_INTERVAL_SECONDS

    while True:
        try:
            if r is None:
                time.sleep(5)
                r = _redis_client()
                continue

            if not _try_acquire_lock(r, lock_value):
                # Another worker is pumping. Idle until it dies.
                time.sleep(_PUMP_LOCK_TTL_SECONDS / 2)
                continue

            from agent.canvas.pump import _live_bot_ids, _push_one
            from django.conf import settings

            api_key = getattr(settings, "ATTENDEE_API_KEY", "")
            api_base = getattr(settings, "AGENT_APP_URL", "").rstrip("/")
            if not api_key or not api_base:
                log.warning("canvas pump daemon: ATTENDEE_API_KEY/AGENT_APP_URL not set; idling")
                time.sleep(10)
                continue

            t0 = time.monotonic()
            try:
                bots = _live_bot_ids()
            except Exception:
                log.exception("canvas pump daemon: live_bot_ids failed")
                bots = []

            for bot_id in bots:
                try:
                    _push_one(bot_id, api_base, api_key)
                except Exception:
                    log.exception("canvas pump daemon: push failed bot=%s", bot_id)

            elapsed = time.monotonic() - t0
            sleep_for = max(0.05, _PUMP_INTERVAL_SECONDS - elapsed)
            backoff = _PUMP_INTERVAL_SECONDS
            time.sleep(sleep_for)
        except Exception:
            log.exception("canvas pump daemon: loop iteration crashed")
            time.sleep(min(30, backoff * 2))
            backoff = min(30, backoff * 2)
