"""Small shared helpers used across the app."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp produced by this app (treat naive values as UTC)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _ISO_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def human_size(size: int | float | None) -> str:
    if size is None:
        return "—"
    size = float(size)
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def relative_time(value: str | datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "—"
    moment = parse_iso(value) if isinstance(value, str) else value
    if moment is None:
        return "—"
    now = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = (now - moment).total_seconds()
    if delta < 0:
        future = abs(delta)
        if future < 60:
            return "in a few seconds"
        if future < 3600:
            return f"in {int(future // 60)}m"
        if future < 86400:
            return f"in {int(future // 3600)}h"
        return f"in {int(future // 86400)}d"
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 604800:
        return f"{int(delta // 86400)}d ago"
    return moment.strftime("%b %d, %Y")


def format_local(value: str | datetime | None, tz_name: str) -> str:
    if value is None:
        return "—"
    moment = parse_iso(value) if isinstance(value, str) else value
    if moment is None:
        return "—"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def datetime_local(value: str | datetime | None, tz_name: str) -> str:
    """ISO-8601 string in the display timezone (used in HTML datetime attrs)."""
    if value is None:
        return ""
    moment = parse_iso(value) if isinstance(value, str) else value
    if moment is None:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return moment.astimezone(tz).isoformat()


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """Make a user supplied string safe for use inside a filename."""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", str(name or "")).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "Unknown"
    return cleaned[:max_length].rstrip(" .")


def escape_output_template(value: str) -> str:
    """Escape literal text that is inserted into a yt-dlp output template."""
    return value.replace("%", "%%")


def tail_text(path: Path, max_bytes: int = 200_000) -> str:
    """Read the tail of a text file without loading the whole thing."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", errors="replace")


_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/@\s:]+):(?P<pw>[^/@\s]+)@")


def mask_credentials(text: str) -> str:
    """Hide user:password pairs in URLs (for example a proxy setting)."""
    return _CREDENTIALS_RE.sub(r"\g<scheme>\g<user>:***@", str(text))


def clamp_int(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
