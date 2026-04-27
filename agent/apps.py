import os
import sys

from django.apps import AppConfig


class AgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agent"
    label = "agent"
    verbose_name = "Meeting Agent"

    def ready(self):
        # Start the canvas pump daemon, but only inside the web (gunicorn)
        # process. We don't want it firing during `manage.py migrate`,
        # `manage.py shell`, in the celery worker, or in the bridge service.
        if os.getenv("CANVAS_PUMP_DAEMON") == "1" or _is_gunicorn_worker():
            try:
                from agent.canvas.pump_daemon import start_pump_daemon

                start_pump_daemon()
            except Exception:
                pass


def _is_gunicorn_worker() -> bool:
    # Gunicorn imports the wsgi module after forking each worker, so by
    # the time AppConfig.ready() runs in a worker, sys.argv[0] points at
    # the gunicorn entrypoint. Belt-and-braces: also check that this is
    # not a manage.py command.
    argv0 = (sys.argv[0] or "").lower()
    if "gunicorn" in argv0:
        return True
    return False
