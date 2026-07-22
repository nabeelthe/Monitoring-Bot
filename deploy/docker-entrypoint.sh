#!/bin/sh
# Entrypoint that makes the data volume writable, then drops to the non-root
# 'bot' user. Needed because mounted volumes (Fly.io, some Docker setups) come
# up root-owned even though the image runs as 'bot'. Signals are forwarded to
# the bot process so shutdowns stay clean (setpriv/su both exec in place).
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data
    chown -R bot:bot /app/data 2>/dev/null || true
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=bot --regid=bot --init-groups "$@"
    fi
    exec su bot -s /bin/sh -c 'exec "$0" "$@"' "$@"
fi

exec "$@"
