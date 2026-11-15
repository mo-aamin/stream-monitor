#!/bin/sh
# Adjust the runtime UID/GID to match a bind mount, then drop privileges.
set -e

DATA_DIR="${DATA_DIR:-/data}"
RECORDINGS_DIR="${RECORDINGS_DIR:-/recordings}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ -n "${UMASK:-}" ]; then
    umask "${UMASK}"
fi

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$RECORDINGS_DIR"

    if [ "$PUID" != "1000" ] || [ "$PGID" != "1000" ]; then
        groupmod -o -g "$PGID" appuser 2>/dev/null || true
        usermod -o -u "$PUID" appuser 2>/dev/null || true
    fi

    # Keep the home directory writable for yt-dlp/Deno caches even if the
    # container previously ran with a different PUID.
    chown -R "$PUID:$PGID" /home/appuser 2>/dev/null || true

    # /data is small (SQLite + logs) so a recursive fix is cheap. Recordings may
    # contain terabytes, so only fix the directory itself.
    chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true
    chown "$PUID:$PGID" "$RECORDINGS_DIR" 2>/dev/null || true

    exec gosu "$PUID:$PGID" "$@"
fi

exec "$@"
