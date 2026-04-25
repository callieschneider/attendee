#!/usr/bin/env bash
set -euo pipefail

# Dispatch based on SERVICE_ROLE env var.
# Default is "web" if unset.

ROLE="${SERVICE_ROLE:-web}"

case "$ROLE" in
  web)
    # Auto-run migrations on every web deploy
    python manage.py migrate --noinput
    exec gunicorn attendee.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 120
    ;;
  worker)
    # -B runs celery beat inside the same process (no separate service needed).
    # Agent beat schedule: canvas image pump (see production_with_agent.py).
    # Use /tmp for the schedule db since the working dir isn't writable.
    exec celery -A attendee worker -B -l info -s /tmp/celerybeat-schedule
    ;;
  bridge)
    exec python -m agent.bridge
    ;;
  *)
    echo "Unknown SERVICE_ROLE: $ROLE (expected 'web', 'worker', or 'bridge')" >&2
    exit 1
    ;;
esac
