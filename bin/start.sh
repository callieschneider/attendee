#!/usr/bin/env bash
set -euo pipefail

# Dispatch based on SERVICE_ROLE env var.
# Default is "web" if unset.

ROLE="${SERVICE_ROLE:-web}"

case "$ROLE" in
  web)
    # Auto-run migrations on every web deploy
    python manage.py migrate --noinput
    # gthread workers + many threads so long-lived SSE streams from
    # /agent/canvas/v2/<bot>/stream don't pin a sync worker per client and
    # block all other requests. Timeout=0 disables the silent-worker reaper
    # (the SSE loop sends `:hb` heartbeats every 15s, which keeps the
    # connection alive but counts as silent under the default 30s reaper).
    exec gunicorn attendee.wsgi \
      --bind "0.0.0.0:${PORT:-8000}" \
      --worker-class gthread \
      --workers 4 \
      --threads 16 \
      --timeout 0 \
      --graceful-timeout 30 \
      --keep-alive 75
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
