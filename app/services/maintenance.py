"""Background housekeeping: prune old recording logs and compact the database."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..database import Database
from ..utils import human_size
from .settings import SettingsService

logger = logging.getLogger("streammonitor.maintenance")

MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60
COMPACT_INTERVAL_SECONDS = 24 * 60 * 60


def _recording_id(path: Path) -> int | None:
    try:
        return int(path.stem)
    except ValueError:
        return None


def prune_recording_logs(
    config: AppConfig,
    db: Database,
    settings: SettingsService,
    *,
    active_ids: set[int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Delete recording logs past the retention window, plus orphaned logs.

    A log is kept if its recording is still active. Retention ``0`` disables
    pruning entirely, including orphans.
    """
    result: dict[str, Any] = {"deleted": 0, "bytes": 0, "errors": 0}
    retention_days = int(settings.typed()["recording_log_retention_days"])
    if retention_days <= 0:
        return result

    log_dir = config.recording_log_dir.resolve()
    if not log_dir.is_dir():
        return result

    protected = set(active_ids or ())
    protected.update(int(row["id"]) for row in db.active_recordings())
    known_ids = db.recording_ids()
    cutoff = (now if now is not None else time.time()) - retention_days * 86400

    pruned_ids: list[int] = []
    for path in log_dir.glob("*.log"):
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(log_dir) or not resolved.is_file():
                continue
            recording_id = _recording_id(resolved)
            if recording_id is not None and recording_id in protected:
                continue
            stat = resolved.stat()
            orphan = recording_id is None or recording_id not in known_ids
            if not orphan and stat.st_mtime >= cutoff:
                continue
            resolved.unlink()
        except OSError:
            result["errors"] += 1
            logger.debug("Could not prune %s", path, exc_info=True)
            continue
        result["deleted"] += 1
        result["bytes"] += stat.st_size
        if recording_id is not None and recording_id in known_ids:
            pruned_ids.append(recording_id)

    db.clear_log_paths(pruned_ids)
    if result["deleted"]:
        message = (
            f"Pruned {result['deleted']} recording log(s) "
            f"({human_size(result['bytes'])}): older than {retention_days} day(s) "
            "or left over from deleted recordings"
        )
        db.add_event("info", "system", message)
        logger.info(message)
    return result


class Maintenance:
    """Runs housekeeping at startup and then periodically."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        settings: SettingsService,
        active_ids=None,
        *,
        interval: float = MAINTENANCE_INTERVAL_SECONDS,
    ):
        self._config = config
        self._db = db
        self._settings = settings
        self._active_ids = active_ids or (lambda: set())
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._last_compact: float | None = None

    def run_once(self) -> dict[str, Any]:
        result = prune_recording_logs(
            self._config, self._db, self._settings, active_ids=set(self._active_ids())
        )
        if (
            self._last_compact is None
            or time.monotonic() - self._last_compact >= COMPACT_INTERVAL_SECONDS
        ):
            try:
                result["compact"] = self._db.compact()
            except Exception:  # noqa: BLE001 - compaction is best effort
                logger.warning("Database compaction failed", exc_info=True)
            self._last_compact = time.monotonic()
        return result

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - housekeeping must never crash the app
                logger.exception("Maintenance run failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="streammonitor-maintenance")

    async def shutdown(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
