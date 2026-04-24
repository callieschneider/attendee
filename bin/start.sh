#!/usr/bin/env bash
set -euo pipefail

# Dispatch based on SERVICE_ROLE env var.
# Default is "web" if unset.

ROLE="${SERVICE_ROLE:-web}"

case "$ROLE" in
  web)
    exec gunicorn attendee.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 120
    ;;
  worker)
    exec celery -A attendee worker -l info
    ;;
  bridge)
    exec python -m agent.bridge
    ;;
  *)
    echo "Unknown SERVICE_ROLE: $ROLE (expected 'web', 'worker', or 'bridge')" >&2
    exit 1
    ;;
esac
