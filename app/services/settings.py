"""Typed, validated application settings stored in SQLite."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..database import Database
from ..utils import clamp_int


@dataclass(frozen=True)
class SettingSpec:
    kind: str
    default: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] | None = None


# ``fulltitle`` is the stream title without the date yt-dlp appends to live
# ``title`` values; the date/platform/channel prefix is added separately.
DEFAULT_FILENAME_TEMPLATE = "%(fulltitle).120B.%(ext)s"
# Earlier default (added the video ID and, for live streams, a second date).
_LEGACY_FILENAME_TEMPLATES = frozenset({"%(title).120B [%(id)s].%(ext)s"})


def build_defaults(config: AppConfig) -> dict[str, SettingSpec]:
    tools = config.default_tools
    return {
        # Monitoring
        "monitor_enabled": SettingSpec("bool", "1"),
        "check_interval": SettingSpec("int", "60", 15, 3600),
        "check_timeout": SettingSpec("int", "120", 20, 600),
        "retry_cooldown": SettingSpec("int", "300", 0, 86400),
        # Tools
        "ytdlp_path": SettingSpec("str", tools["ytdlp_path"]),
        "deno_path": SettingSpec("str", tools["deno_path"]),
        "ffmpeg_path": SettingSpec("str", tools["ffmpeg_path"]),
        "ffprobe_path": SettingSpec("str", tools["ffprobe_path"]),
        # Authentication / network
        "cookies_file": SettingSpec("str", ""),
        "proxy": SettingSpec("str", ""),
        "impersonate_target": SettingSpec("str", "chrome"),
        "impersonate_youtube": SettingSpec("bool", "0"),
        "impersonate_twitch": SettingSpec("bool", "1"),
        "impersonate_kick": SettingSpec("bool", "1"),
        "impersonate_rumble": SettingSpec("bool", "1"),
        # Recording
        "filename_template": SettingSpec("str", DEFAULT_FILENAME_TEMPLATE),
        "merge_output_format": SettingSpec("str", "mp4", choices=("", "mp4", "mkv", "webm")),
        "format_selector": SettingSpec("str", ""),
        "youtube_live_from_start": SettingSpec("bool", "0"),
        "rumble_live_from_start": SettingSpec("bool", "0"),
        "write_info_json": SettingSpec("bool", "0"),
        "extra_ytdlp_args": SettingSpec("str", ""),
        # Logs
        "recording_log_retention_days": SettingSpec("int", "30", 0, 3650),
        "recording_log_max_mb": SettingSpec("int", "10", 1, 500),
    }


_IMPERSONATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_PROXY_RE = re.compile(r"^(https?|socks5h?)://[^\s]+$", re.IGNORECASE)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class SettingsService:
    """Reads/writes settings and exposes a typed snapshot."""

    def __init__(self, db: Database, config: AppConfig):
        self._db = db
        self._config = config
        self.specs = build_defaults(config)
        self._cache: dict[str, str] | None = None
        self.ensure_defaults()

    # ------------------------------------------------------------------ basics
    def ensure_defaults(self) -> None:
        existing = self._db.get_settings()
        missing = {
            key: spec.default for key, spec in self.specs.items() if key not in existing
        }
        if existing.get("filename_template") in _LEGACY_FILENAME_TEMPLATES:
            missing["filename_template"] = DEFAULT_FILENAME_TEMPLATE
        if missing:
            self._db.save_settings(missing)
        self._cache = None

    def _raw(self) -> dict[str, str]:
        if self._cache is None:
            stored = self._db.get_settings()
            cache: dict[str, str] = {}
            for key, spec in self.specs.items():
                cache[key] = stored.get(key, spec.default)
            self._cache = cache
        return self._cache

    def raw(self) -> dict[str, str]:
        return dict(self._raw())

    def invalidate(self) -> None:
        self._cache = None

    def get(self, key: str, default: Any = None) -> str:
        spec = self.specs.get(key)
        if spec is None:
            return self._db.get_setting(key) if default is None else default
        return self._raw().get(key, spec.default)

    # ------------------------------------------------------------------ typed
    def typed(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        raw = self._raw()
        for key, spec in self.specs.items():
            value = raw.get(key, spec.default)
            if spec.kind == "bool":
                result[key] = _to_bool(value)
            elif spec.kind == "int":
                result[key] = clamp_int(
                    value,
                    spec.minimum if spec.minimum is not None else -2**31,
                    spec.maximum if spec.maximum is not None else 2**31 - 1,
                    int(spec.default or 0),
                )
            else:
                result[key] = value
        return result

    # ----------------------------------------------------------------- update
    def update(self, values: dict[str, Any]) -> dict[str, str]:
        """Validate and persist values. Returns the normalized strings saved.

        Raises :class:`ValueError` with a user-facing message on invalid input.
        """
        unknown = set(values) - set(self.specs)
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")

        normalized: dict[str, str] = {}
        for key, value in values.items():
            spec = self.specs[key]
            if spec.kind == "bool":
                normalized[key] = "1" if _to_bool(value) else "0"
                continue
            text = str(value).strip() if value is not None else ""
            if spec.kind == "int":
                try:
                    parsed = int(text)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be a whole number.") from None
                minimum = spec.minimum if spec.minimum is not None else -(2**31)
                maximum = spec.maximum if spec.maximum is not None else 2**31 - 1
                if not minimum <= parsed <= maximum:
                    raise ValueError(f"{key} must be between {minimum} and {maximum}.")
                normalized[key] = str(parsed)
                continue
            if spec.choices is not None and text not in spec.choices:
                raise ValueError(f"{key} must be one of: {', '.join(spec.choices) or '(empty)'}.")
            normalized[key] = text

        # -- special validation -------------------------------------------------
        if "ytdlp_path" in normalized and not normalized["ytdlp_path"]:
            raise ValueError("The yt-dlp path cannot be empty.")

        if "cookies_file" in normalized and normalized["cookies_file"]:
            cookie_path = Path(normalized["cookies_file"]).expanduser()
            if not cookie_path.is_file():
                raise ValueError(f"Cookies file not found: {cookie_path}")
            normalized["cookies_file"] = str(cookie_path)

        if "proxy" in normalized and normalized["proxy"]:
            if not _PROXY_RE.match(normalized["proxy"]):
                raise ValueError("Proxy must be a URL such as http://host:port or socks5://host:port.")

        if "impersonate_target" in normalized and normalized["impersonate_target"]:
            if not _IMPERSONATE_RE.match(normalized["impersonate_target"]):
                raise ValueError("Impersonate target contains invalid characters.")

        if "filename_template" in normalized:
            template = normalized["filename_template"]
            if not template:
                raise ValueError("The filename template cannot be empty.")
            if "%(ext)s" not in template:
                raise ValueError("The filename template must contain %(ext)s.")
            if len(template) > 300:
                raise ValueError("The filename template is too long.")
            if ".." in re.split(r"[\\/]", template) or "\x00" in template:
                raise ValueError(
                    "The filename template must not contain parent-directory segments."
                )

        if "format_selector" in normalized and len(normalized["format_selector"]) > 200:
            raise ValueError("The format selector is too long.")

        if "extra_ytdlp_args" in normalized:
            if len(normalized["extra_ytdlp_args"]) > 2000:
                raise ValueError("Extra yt-dlp arguments are too long.")
            try:
                shlex.split(normalized["extra_ytdlp_args"])
            except ValueError as exc:
                raise ValueError(f"Extra yt-dlp arguments are malformed: {exc}") from None

        self._db.save_settings(normalized)
        self._cache = None
        return normalized

    def set_boolean(self, key: str, value: bool) -> None:
        self.update({key: value})

    # ------------------------------------------------------------------- view
    def describe(self) -> dict[str, dict[str, Any]]:
        """Metadata used by templates to render inputs generically."""
        return {
            key: {
                "kind": spec.kind,
                "default": spec.default,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "choices": spec.choices,
            }
            for key, spec in self.specs.items()
        }
