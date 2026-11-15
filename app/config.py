"""Environment driven application configuration.

Everything that must be known before the database exists (paths, host binding)
lives here. User-editable runtime options live in the database and are
managed by :mod:`app.services.settings`.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_tool(name: str, fallback: str) -> str:
    """Pick an executable path that exists on this machine, else a sensible default."""
    override = os.environ.get(f"{name.upper()}_PATH")
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    return fallback


def _parse_port(value: str, default: int = 8080) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


@dataclass(frozen=True)
class AppConfig:
    """Immutable process level configuration."""

    data_dir: Path
    recordings_dir: Path
    database_path: Path
    host: str
    port: int
    timezone: str
    log_level: str
    monitor_autostart: bool

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def app_log_path(self) -> Path:
        return self.log_dir / "app.log"

    @property
    def recording_log_dir(self) -> Path:
        return self.log_dir / "recordings"

    @property
    def default_tools(self) -> dict[str, str]:
        """Default tool paths seeded into settings on first run."""
        return {
            "ytdlp_path": _default_tool("yt-dlp", "/usr/local/bin/yt-dlp"),
            "deno_path": _default_tool("deno", "/usr/local/bin/deno"),
            "ffmpeg_path": _default_tool("ffmpeg", "/usr/bin/ffmpeg"),
            "ffprobe_path": _default_tool("ffprobe", "/usr/bin/ffprobe"),
        }

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.recording_log_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    """Build configuration from environment variables."""
    data_dir = Path(_env("DATA_DIR", "./data")).expanduser().resolve()
    recordings_dir = Path(_env("RECORDINGS_DIR", "./recordings")).expanduser().resolve()
    database_path = Path(
        _env("DATABASE_PATH", str(data_dir / "streammonitor.db"))
    ).expanduser().resolve()

    timezone = _env("TZ", "UTC")
    # Fail soft on an invalid TZ so the app still boots.
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - any invalid zone falls back to UTC
        timezone = "UTC"

    return AppConfig(
        data_dir=data_dir,
        recordings_dir=recordings_dir,
        database_path=database_path,
        host=_env("APP_HOST", "0.0.0.0"),
        port=_parse_port(_env("APP_PORT", "8080")),
        timezone=timezone,
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        monitor_autostart=_env_bool("MONITOR_AUTOSTART", True),
    )
