"""SQLite persistence layer.

The app deliberately uses the standard library ``sqlite3`` module: the workload
is tiny, WAL mode handles the occasional concurrent read, and it keeps the
Docker image free of an ORM.  A new connection is opened per operation so the
layer is safe to use from both the event loop and FastAPI's threadpool.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .utils import utcnow_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT    NOT NULL,
    identifier      TEXT    NOT NULL COLLATE NOCASE,
    url             TEXT    NOT NULL,
    name            TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    last_checked_at TEXT,
    last_status     TEXT    NOT NULL DEFAULT 'unknown',
    last_title      TEXT,
    last_error      TEXT,
    last_live_at    TEXT,
    UNIQUE (platform, identifier)
);

CREATE TABLE IF NOT EXISTS recordings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    platform         TEXT    NOT NULL,
    channel_name     TEXT,
    title            TEXT,
    video_id         TEXT,
    url              TEXT,
    filename         TEXT,
    filepath         TEXT,
    status           TEXT    NOT NULL DEFAULT 'recording',
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    duration_seconds INTEGER,
    size_bytes       INTEGER,
    exit_code        INTEGER,
    error            TEXT,
    log_path         TEXT,
    ytdlp_version    TEXT
);

CREATE INDEX IF NOT EXISTS idx_recordings_started
    ON recordings (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_recordings_channel
    ON recordings (channel_id, status);
CREATE INDEX IF NOT EXISTS idx_recordings_status
    ON recordings (status);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    category   TEXT NOT NULL DEFAULT 'system',
    channel_id INTEGER,
    message    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created
    ON events (created_at DESC);
"""


class DuplicateChannelError(Exception):
    """Raised when a platform/channel pair already exists."""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # ------------------------------------------------------------------ setup
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def _update(self, table: str, row_id: int, values: dict[str, Any], allowed: set[str]) -> bool:
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return False
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = list(fields.values()) + [row_id]
        with self.connect() as conn:
            cursor = conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", params)
            return cursor.rowcount > 0

    # ----------------------------------------------------------------- channels
    def list_channels(
        self,
        *,
        platform: str | None = None,
        enabled_only: bool = False,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if enabled_only:
            clauses.append("enabled = 1")
        if search:
            clauses.append("(identifier LIKE ? OR name LIKE ? OR url LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM channels {where} ORDER BY platform ASC, identifier ASC",
                params,
            ).fetchall()
        return self._rows(rows)

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        return dict(row) if row else None

    def add_channel(
        self,
        platform: str,
        identifier: str,
        url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO channels
                        (platform, identifier, url, name, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (platform, identifier, url, name or None, 1 if enabled else 0, now, now),
                )
                channel_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DuplicateChannelError(identifier) from exc
        channel = self.get_channel(channel_id)
        assert channel is not None
        return channel

    def update_channel(self, channel_id: int, **values: Any) -> bool:
        values["updated_at"] = utcnow_iso()
        allowed = {
            "identifier", "url", "name", "enabled", "updated_at",
            "last_checked_at", "last_status", "last_title", "last_error", "last_live_at",
        }
        return self._update("channels", channel_id, values, allowed)

    def set_channel_status(
        self,
        channel_id: int,
        status: str,
        *,
        title: str | None = None,
        error: str | None = None,
        live: bool = False,
    ) -> None:
        now = utcnow_iso()
        with self.connect() as conn:
            if live:
                conn.execute(
                    """
                    UPDATE channels
                       SET last_status = ?, last_checked_at = ?, last_title = ?,
                           last_error = NULL, last_live_at = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (status, now, title, now, now, channel_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE channels
                       SET last_status = ?, last_checked_at = ?, last_error = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (status, now, error, now, channel_id),
                )

    def delete_channel(self, channel_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            return cursor.rowcount > 0

    def count_channels(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(enabled), 0) AS enabled FROM channels"
            ).fetchone()
        return {"total": int(row["total"]), "enabled": int(row["enabled"])}

    # --------------------------------------------------------------- recordings
    def create_recording(
        self,
        *,
        channel_id: int | None,
        platform: str,
        channel_name: str,
        title: str,
        video_id: str | None,
        url: str,
        log_path: str,
        ytdlp_version: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO recordings
                    (channel_id, platform, channel_name, title, video_id, url,
                     status, started_at, log_path, ytdlp_version)
                VALUES (?, ?, ?, ?, ?, ?, 'recording', ?, ?, ?)
                """,
                (channel_id, platform, channel_name, title, video_id, url, now, log_path, ytdlp_version),
            )
            recording_id = int(cursor.lastrowid)
        recording = self.get_recording(recording_id)
        assert recording is not None
        return recording

    def update_recording(self, recording_id: int, **values: Any) -> bool:
        allowed = {
            "channel_id", "filename", "filepath", "status", "finished_at",
            "duration_seconds", "size_bytes", "exit_code", "error", "log_path",
            "title", "video_id", "url",
        }
        return self._update("recordings", recording_id, values, allowed)

    def finish_recording(
        self,
        recording_id: int,
        *,
        status: str,
        exit_code: int | None = None,
        error: str | None = None,
        filename: str | None = None,
        filepath: str | None = None,
        size_bytes: int | None = None,
    ) -> None:
        row = self.get_recording(recording_id)
        if row is None:
            return
        started = row.get("started_at")
        duration: int | None = None
        if started:
            try:
                start_dt = datetime.fromisoformat(started)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                duration = int((datetime.now(timezone.utc) - start_dt).total_seconds())
            except ValueError:
                duration = None
        values: dict[str, Any] = {
            "status": status,
            "finished_at": utcnow_iso(),
            "duration_seconds": max(0, duration) if duration is not None else None,
            "exit_code": exit_code,
            "error": error,
        }
        if filename is not None:
            values["filename"] = filename
        if filepath is not None:
            values["filepath"] = filepath
        if size_bytes is not None:
            values["size_bytes"] = size_bytes
        self.update_recording(recording_id, **values)

    def get_recording(self, recording_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        return dict(row) if row else None

    def list_recordings(
        self,
        *,
        limit: int = 200,
        status: str | None = None,
        search: str | None = None,
        channel_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if search:
            clauses.append("(channel_name LIKE ? OR title LIKE ? OR filename LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM recordings {where} ORDER BY started_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return self._rows(rows)

    def active_recordings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE status = 'recording' ORDER BY started_at ASC"
            ).fetchall()
        return self._rows(rows)

    def active_recording_for_channel(self, channel_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recordings WHERE channel_id = ? AND status = 'recording' "
                "ORDER BY id DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_orphaned_recordings(self) -> int:
        """Recordings left in 'recording' state by a previous process are dead."""
        now = utcnow_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE recordings
                   SET status = 'interrupted', finished_at = ?,
                       error = 'Application restarted while this recording was active'
                 WHERE status = 'recording'
                """,
                (now,),
            )
            return cursor.rowcount

    def delete_recording(self, recording_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
            return cursor.rowcount > 0

    def recording_ids(self) -> set[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM recordings").fetchall()
        return {int(row["id"]) for row in rows}

    def clear_log_paths(self, recording_ids: list[int]) -> int:
        """Forget the log file of recordings whose log was pruned."""
        if not recording_ids:
            return 0
        with self.connect() as conn:
            cursor = conn.executemany(
                "UPDATE recordings SET log_path = NULL WHERE id = ?",
                [(recording_id,) for recording_id in recording_ids],
            )
            return cursor.rowcount

    def compact(self) -> dict[str, int]:
        """Checkpoint the WAL and reclaim free pages if many have built up."""
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            total_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        vacuumed = 0
        # VACUUM rewrites the whole file, so only do it when it pays off.
        if free_pages >= 1000 and free_pages * 4 >= total_pages:
            conn = sqlite3.connect(str(self.path), timeout=30)
            try:
                conn.execute("PRAGMA busy_timeout = 30000")
                conn.execute("VACUUM")
                vacuumed = 1
            finally:
                conn.close()
        return {"free_pages": free_pages, "total_pages": total_pages, "vacuumed": vacuumed}

    def recording_stats(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(size_bytes), 0) AS total_bytes,
                       COALESCE(SUM(CASE WHEN substr(started_at, 1, 10) = ? THEN 1 ELSE 0 END), 0) AS today
                  FROM recordings
                """,
                (today,),
            ).fetchone()
        return {
            "total": int(row["total"]),
            "total_bytes": int(row["total_bytes"]),
            "today": int(row["today"]),
        }

    # ----------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def save_settings(self, values: dict[str, str]) -> None:
        now = utcnow_iso()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                [(key, str(value), now) for key, value in values.items()],
            )

    # ------------------------------------------------------------------- events
    def add_event(
        self,
        level: str,
        category: str,
        message: str,
        channel_id: int | None = None,
    ) -> int:
        now = utcnow_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO events (created_at, level, category, channel_id, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, level, category, channel_id, message),
            )
            event_id = int(cursor.lastrowid)
            row = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()
            if row and int(row["count"]) > 2500:
                conn.execute(
                    "DELETE FROM events WHERE id IN "
                    "(SELECT id FROM events ORDER BY id ASC LIMIT 500)"
                )
        return event_id

    def list_events(
        self,
        *,
        limit: int = 100,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        if level:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM events WHERE level = ? ORDER BY id DESC LIMIT ?",
                    (level, limit),
                ).fetchall()
        else:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return self._rows(rows)

    # -------------------------------------------------------------------- stats
    def dashboard_stats(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.connect() as conn:
            channels = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(enabled), 0) AS enabled,
                       COALESCE(SUM(CASE WHEN enabled = 1 AND last_status = 'live' THEN 1 ELSE 0 END), 0) AS live
                  FROM channels
                """
            ).fetchone()
            recordings = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(size_bytes), 0) AS total_bytes,
                       COALESCE(SUM(CASE WHEN substr(started_at, 1, 10) = ? THEN 1 ELSE 0 END), 0) AS today
                  FROM recordings
                """,
                (now,),
            ).fetchone()
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM recordings WHERE status = 'recording'"
            ).fetchone()
        return {
            "channels_total": int(channels["total"]),
            "channels_enabled": int(channels["enabled"]),
            "channels_live": int(channels["live"]),
            "recordings_total": int(recordings["total"]),
            "recordings_bytes": int(recordings["total_bytes"]),
            "recordings_today": int(recordings["today"]),
            "recordings_active": int(active["count"]),
        }
