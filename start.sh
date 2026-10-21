#!/usr/bin/env sh
set -eu

ROLE="${APP_ROLE:-web}"
echo "Comment Radar: role=${ROLE}"

if [ "$ROLE" = "worker" ]; then
  exec python -m app.worker
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --proxy-headers \
  --forwarded-allow-ips="*"
