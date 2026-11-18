"""Build yt-dlp commands and interpret its output."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils import escape_output_template, sanitize_filename
from .platforms import platform_label

# Errors that simply mean "nothing is live right now" rather than a real fault.
_OFFLINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"not currently live",
        r"is not live",
        r"no live (?:stream|broadcast)",
        r"live (?:event|stream) (?:will|is scheduled to) begin",
        r"will begin in",
        r"premieres in",
        r"upcoming live",
        r"channel is offline",
        r"currently offline",
        r"there is no live",
        r"no videos? (?:found|available)",
        r"does not have any videos",
        r"has not (?:streamed|gone live)",
        r"no formats? found",
        r"unable to find (?:the )?(?:channel|video|stream)",
        r"channel (?:does not exist|not found)",
        r"video (?:does not exist|not found|unavailable)",
        r"\b404\b",
        r"not found",
    )
)

_IMPERSONATE_HINT = re.compile(
    r"impersonat|curl_cffi|curl-cffi", re.IGNORECASE
)
_JS_RUNTIME_HINT = re.compile(r"javascript runtime|\bdeno\b|\bejs\b", re.IGNORECASE)
_SIGNIN_HINT = re.compile(
    r"sign in|login required|confirm you(?:'| a)?re not a bot|cookies", re.IGNORECASE
)


MP4_COMPAT_FORMAT_SORT = "vcodec:h264,acodec:aac"


def _has_format_sort(extra_args: str) -> bool:
    """True when the user's extra yt-dlp args already set a format sort."""
    try:
        tokens = shlex.split(extra_args)
    except ValueError:
        return False
    return any(
        token in {"-S", "--format-sort"} or token.startswith("--format-sort=")
        for token in tokens
    )


def build_common_args(cfg: dict[str, Any], platform: str) -> list[str]:
    args: list[str] = [
        cfg["ytdlp_path"],
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout",
        "20",
    ]
    deno_path = (cfg.get("deno_path") or "").strip()
    if deno_path:
        args.extend(["--js-runtimes", f"deno:{deno_path}"])
    ffmpeg_path = (cfg.get("ffmpeg_path") or "").strip()
    if ffmpeg_path:
        args.extend(["--ffmpeg-location", ffmpeg_path])
    cookies = (cfg.get("cookies_file") or "").strip()
    if cookies:
        args.extend(["--cookies", cookies])
    proxy = (cfg.get("proxy") or "").strip()
    if proxy:
        args.extend(["--proxy", proxy])
    target = (cfg.get("impersonate_target") or "").strip()
    if target and cfg.get(f"impersonate_{platform}"):
        args.extend(["--impersonate", target])
    return args


def build_check_args(cfg: dict[str, Any], platform: str, url: str) -> list[str]:
    args = build_common_args(cfg, platform)
    args.extend(
        [
            "--simulate",
            "--dump-single-json",
            "--no-progress",
            "--retries",
            "2",
            "--extractor-retries",
            "1",
            url,
        ]
    )
    return args


def filename_prefix(platform: str, channel_name: str, started: datetime) -> str:
    """The unique prefix every file from one recording starts with."""
    return (
        f"{started.strftime('%Y-%m-%d_%H-%M-%S')} - {platform_label(platform)} - "
        f"{sanitize_filename(channel_name, 80)} - "
    )


def build_output_template(
    recordings_dir: str,
    filename_template: str,
    platform: str,
    channel_name: str,
    started: datetime,
) -> str:
    """Filename prefix mirrors the old app: date - platform - channel - title."""
    prefix = filename_prefix(platform, channel_name, started)
    escaped_prefix = escape_output_template(str(Path(recordings_dir)) + "/" + prefix)
    return escaped_prefix + filename_template


def build_record_args(
    cfg: dict[str, Any],
    platform: str,
    url: str,
    output_template: str,
) -> list[str]:
    args = build_common_args(cfg, platform)
    args.extend(
        [
            "--newline",
            "--progress",
            "--progress-delta",
            "30",
            "--no-colors",
            "--windows-filenames",
            "--trim-filenames",
            "240",
            "--retries",
            "10",
            "--fragment-retries",
            "20",
            "--retry-sleep",
            "2",
        ]
    )
    merge_format = (cfg.get("merge_output_format") or "").strip()
    if merge_format:
        args.extend(["--merge-output-format", merge_format])
    format_selector = (cfg.get("format_selector") or "").strip()
    extra = (cfg.get("extra_ytdlp_args") or "").strip()
    if format_selector:
        args.extend(["-f", format_selector])
    elif merge_format == "mp4" and not _has_format_sort(extra):
        # yt-dlp prefers VP9/AV1 on YouTube by default; VP9 inside MP4 does not
        # play in QuickTime/Safari and many other players. Prefer H.264 + AAC.
        # ``-S`` only ranks formats, so streams without H.264 still record.
        args.extend(["-S", MP4_COMPAT_FORMAT_SORT])
    if cfg.get("write_info_json"):
        args.append("--write-info-json")
    if platform == "youtube" and cfg.get("youtube_live_from_start"):
        args.append("--live-from-start")
    if platform == "rumble" and cfg.get("rumble_live_from_start"):
        args.append("--live-from-start")

    if extra:
        try:
            args.extend(shlex.split(extra))
        except ValueError:
            # Validation should have caught this; ignore rather than crash a recording.
            pass

    args.extend(["--print", "after_move:%(filepath)s"])
    args.extend(["-o", output_template, url])
    return args


def parse_metadata(payload: str) -> dict[str, Any]:
    """Parse ``-J`` output into a normalised live-status dictionary."""
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Unexpected yt-dlp output")
    if data.get("_type") == "playlist":
        entries = [entry for entry in (data.get("entries") or []) if isinstance(entry, dict)]
        if not entries:
            raise ValueError("No playable entries returned")
        data = entries[0]

    live_status = str(data.get("live_status") or "").lower()
    is_live = bool(data.get("is_live")) or live_status == "is_live"
    channel_name = (
        data.get("channel")
        or data.get("uploader")
        or data.get("creator")
        or data.get("uploader_id")
        or "Unknown"
    )
    return {
        "is_live": is_live,
        "live_status": live_status or None,
        "channel_name": str(channel_name),
        "title": str(data.get("fulltitle") or data.get("title") or "Untitled stream"),
        "video_id": str(data.get("id") or ""),
        "webpage_url": str(data.get("webpage_url") or data.get("original_url") or ""),
        "extractor": str(data.get("extractor_key") or data.get("extractor") or ""),
    }


def classify_failure(stderr: str) -> str:
    """Return 'offline' | 'impersonate' | 'js_runtime' | 'signin' | 'error'."""
    text = (stderr or "").strip()
    if not text:
        return "error"
    if _IMPERSONATE_HINT.search(text):
        return "impersonate"
    if _SIGNIN_HINT.search(text):
        return "signin"
    if _JS_RUNTIME_HINT.search(text) and re.search(
        r"not (?:found|available|installed)|requires|missing", text, re.IGNORECASE
    ):
        return "js_runtime"
    for pattern in _OFFLINE_PATTERNS:
        if pattern.search(text):
            return "offline"
    return "error"


def short_error(stderr: str, limit: int = 600) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    # Prefer the last ERROR line; yt-dlp puts the useful message there.
    for line in reversed(lines):
        if "ERROR" in line.upper():
            text = line
            break
    else:
        text = lines[-1]
    text = re.sub(r"^ERROR:\s*", "", text, flags=re.IGNORECASE)
    return text[:limit]


_DESTINATION_RE = re.compile(r"^\[download\] Destination:\s+(?P<path>.+?)\s*$")
_MERGE_RE = re.compile(r'^\[Merger\] Merging formats into "(?P<path>.+)"\s*$')
_EXISTING_RE = re.compile(r"^\[download\]\s+(?P<path>.+?) has already been downloaded")
_MOVE_RE = re.compile(r'^\[MoveFiles\] Moving file "(?P<src>.+)" to "(?P<dst>.+)"\s*$')


def extract_output_path(line: str) -> str | None:
    """Recognise the final media path in yt-dlp's progress output."""
    text = (line or "").strip()
    for pattern, group in (
        (_MERGE_RE, "path"),
        (_MOVE_RE, "dst"),
        (_EXISTING_RE, "path"),
        (_DESTINATION_RE, "path"),
    ):
        match = pattern.match(text)
        if match:
            path = match.group(group).strip()
            if path.endswith(".part"):
                path = path[:-5]
            if path:
                return path
    # ``--print after_move:%(filepath)s`` emits a bare absolute path.
    if text.startswith("/") and not text.startswith("//"):
        lowered = text.lower()
        if lowered.endswith(
            (".mp4", ".mkv", ".webm", ".flv", ".ts", ".m4a", ".mp3", ".opus", ".ogg", ".wav")
        ):
            return text
    return None
