"""Platform URL normalization and Rumble live page helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

PLATFORMS: dict[str, dict[str, str]] = {
    "youtube": {
        "label": "YouTube",
        "short": "YT",
        "color": "#ff4d4d",
        "placeholder": "@channelhandle or https://www.youtube.com/@handle",
        "hint": "Use an @handle, a channel ID (UC…), or a channel URL. A /live suffix is added automatically.",
    },
    "twitch": {
        "label": "Twitch",
        "short": "TW",
        "color": "#a970ff",
        "placeholder": "channelname or https://www.twitch.tv/channelname",
        "hint": "Channel name or URL. Impersonation is strongly recommended for Twitch.",
    },
    "kick": {
        "label": "Kick",
        "short": "KK",
        "color": "#53fc18",
        "placeholder": "channelname or https://kick.com/channelname",
        "hint": "Channel name or URL. Kick normally requires browser impersonation.",
    },
    "rumble": {
        "label": "Rumble",
        "short": "RM",
        "color": "#f5a623",
        "placeholder": "channelname, c/channelname, or https://rumble.com/c/channelname",
        "hint": "Accepts /c/ or /user/ channel URLs. A currently live video URL also works.",
    },
}

_ALLOWED_HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"},
    "twitch": {"twitch.tv", "www.twitch.tv", "m.twitch.tv", "go.twitch.tv"},
    "kick": {"kick.com", "www.kick.com"},
    "rumble": {"rumble.com", "www.rumble.com"},
}

_VIDEO_PATH_RE = re.compile(r"^/(?:v|embed/)[^/]+", re.IGNORECASE)
_RUMBLE_EMBED_RE = re.compile(
    r'"embedUrl"\s*:\s*"(?P<url>https?://(?:www\.)?rumble\.com/embed/[^"]+)"'
)
_RUMBLE_EMBED_FALLBACK_RE = re.compile(
    r"(?P<url>https?://(?:www\.)?rumble\.com/embed/[0-9a-z]+/?)", re.IGNORECASE
)


class ChannelValidationError(ValueError):
    """Invalid platform or channel identifier supplied by a user."""


@dataclass(frozen=True)
class NormalizedChannel:
    platform: str
    identifier: str
    url: str


def platform_label(platform: str) -> str:
    return PLATFORMS.get(platform, {}).get("label", platform.title())


def platform_color(platform: str) -> str:
    return PLATFORMS.get(platform, {}).get("color", "#8b95a5")


def _check_host(platform: str, parsed) -> None:
    if parsed.scheme not in {"http", "https"}:
        raise ChannelValidationError("Only http(s) URLs are supported.")
    if parsed.username or parsed.password:
        raise ChannelValidationError("URLs containing credentials are not allowed.")
    if parsed.port not in (None, 80, 443):
        raise ChannelValidationError("Only standard HTTP/HTTPS ports are allowed.")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS.get(platform, set()):
        raise ChannelValidationError(
            f"That URL does not look like a {platform_label(platform)} URL."
        )


def _clean_url(value: str) -> str:
    parsed = urlparse(value.strip())
    # Drop query/fragment noise from channel URLs; keep path only.
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def normalize_channel(platform: str, raw: str) -> NormalizedChannel:
    """Turn user input into a canonical identifier + URL used for live checks."""
    platform = (platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ChannelValidationError("Choose a supported platform.")
    value = (raw or "").strip()
    if not value:
        raise ChannelValidationError("Enter a channel name or URL.")
    if len(value) > 500:
        raise ChannelValidationError("Channel input is too long.")
    if any(char.isspace() for char in value):
        raise ChannelValidationError("Channel names cannot contain spaces.")

    if not value.lower().startswith(("http://", "https://")):
        if platform == "youtube":
            if value.startswith("@"):
                identifier = value
                url = f"https://www.youtube.com/{value}/live"
            elif value.startswith("UC") and len(value) >= 20:
                identifier = value
                url = f"https://www.youtube.com/channel/{value}/live"
            else:
                identifier = value
                url = f"https://www.youtube.com/{value}/live"
            return NormalizedChannel(platform, identifier, url)
        if platform == "twitch":
            identifier = value.lower()
            return NormalizedChannel(platform, identifier, f"https://www.twitch.tv/{value}")
        if platform == "kick":
            identifier = value.lower()
            return NormalizedChannel(platform, identifier, f"https://kick.com/{value}")
        if platform == "rumble":
            cleaned = value.strip("/")
            if cleaned.startswith("c/") or cleaned.startswith("user/"):
                identifier = cleaned
                return NormalizedChannel(platform, identifier, f"https://rumble.com/{cleaned}")
            identifier = f"c/{cleaned}"
            return NormalizedChannel(platform, identifier, f"https://rumble.com/c/{cleaned}")

    parsed = urlparse(value)
    _check_host(platform, parsed)
    path = parsed.path or "/"
    clean = _clean_url(value)

    if platform == "youtube":
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            direct = value.strip()
            return NormalizedChannel(platform, direct, direct)
        if path.startswith("/watch") or path.startswith(("/shorts/", "/embed/", "/live/")):
            direct = value.strip()
            return NormalizedChannel(platform, direct, direct)
        if path.startswith("/channel/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 2:
                identifier = parts[1]
                return NormalizedChannel(
                    platform, identifier, f"https://www.youtube.com/channel/{identifier}/live"
                )
        if path.startswith("/@"):
            handle = path.split("/")[1]
            return NormalizedChannel(
                platform, handle, f"https://www.youtube.com/{handle}/live"
            )
        if path.startswith(("/c/", "/user/")):
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 2:
                identifier = f"{parts[0]}/{parts[1]}"
                return NormalizedChannel(
                    platform, identifier, f"https://www.youtube.com/{parts[0]}/{parts[1]}/live"
                )
        if path in {"", "/"}:
            raise ChannelValidationError("That YouTube URL does not include a channel.")
        # Some other channel URL (for example a custom legacy path).
        return NormalizedChannel(platform, clean, f"{clean}/live")

    if platform == "twitch":
        parts = [part for part in path.split("/") if part]
        if not parts:
            raise ChannelValidationError("That Twitch URL does not include a channel.")
        identifier = parts[0].lower()
        if len(parts) > 1:
            identifier = "/".join(parts).lower()
        return NormalizedChannel(platform, identifier, clean)

    if platform == "kick":
        parts = [part for part in path.split("/") if part]
        if not parts:
            raise ChannelValidationError("That Kick URL does not include a channel.")
        identifier = parts[0].lower()
        if parts[0] in {"video", "videos"} and len(parts) > 1:
            identifier = "/".join(parts).lower()
        return NormalizedChannel(platform, identifier, clean)

    # Rumble
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise ChannelValidationError("That Rumble URL does not include a channel.")
    if parts[0].lower() in {"c", "user"} and len(parts) >= 2:
        identifier = f"{parts[0].lower()}/{parts[1]}"
        return NormalizedChannel(platform, identifier, f"https://rumble.com/{identifier}")
    if _VIDEO_PATH_RE.match(path) or path.startswith("/embed/"):
        return NormalizedChannel(platform, clean, clean)
    # Root-level profile/channel path.
    return NormalizedChannel(platform, f"c/{parts[0]}", f"https://rumble.com/c/{parts[0]}")


def is_direct_rumble_video(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path or ""
    return bool(path.startswith("/embed/") or _VIDEO_PATH_RE.match(path))


def rumble_live_page_url(url: str) -> str | None:
    """The Rumble page that resolves to the channel's current live stream."""
    if is_direct_rumble_video(url):
        return None
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return None
    if path.endswith("/live"):
        live_path = path
    else:
        live_path = f"{path}/live"
    return urlunparse(("https", parsed.netloc, live_path, "", "", ""))


def extract_rumble_embed_url(html: str) -> str | None:
    """Extract the video/embed URL from a Rumble channel-live page."""
    if not html:
        return None
    match = _RUMBLE_EMBED_RE.search(html) or _RUMBLE_EMBED_FALLBACK_RE.search(html)
    if not match:
        return None
    url = match.group("url")
    parsed = urlparse(url)
    if (parsed.hostname or "").lower().replace("www.", "") != "rumble.com":
        return None
    if not parsed.path.startswith("/embed/"):
        return None
    return urlunparse(("https", "rumble.com", parsed.path.rstrip("/") + "/", "", "", ""))



