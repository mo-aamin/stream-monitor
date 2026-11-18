from __future__ import annotations

import pytest

from app.services.platforms import (
    ChannelValidationError,
    extract_rumble_embed_url,
    is_direct_rumble_video,
    normalize_channel,
    rumble_alternate_channel,
    rumble_live_page_url,
)


@pytest.mark.parametrize(
    "raw,identifier,url",
    [
        ("@HasanAbi", "@HasanAbi", "https://www.youtube.com/@HasanAbi/live"),
        (
            "https://www.youtube.com/@HasanAbi",
            "@HasanAbi",
            "https://www.youtube.com/@HasanAbi/live",
        ),
        (
            "https://youtube.com/channel/UC12345678901234567890",
            "UC12345678901234567890",
            "https://www.youtube.com/channel/UC12345678901234567890/live",
        ),
    ],
)
def test_normalize_youtube(raw, identifier, url):
    result = normalize_channel("youtube", raw)
    assert result.platform == "youtube"
    assert result.identifier == identifier
    assert result.url == url


def test_youtube_direct_video_urls_are_preserved():
    short = normalize_channel("youtube", "https://youtu.be/abc123")
    assert short.url == "https://youtu.be/abc123"
    watch = normalize_channel("youtube", "https://www.youtube.com/watch?v=abc123")
    assert watch.url == "https://www.youtube.com/watch?v=abc123"


def test_normalize_twitch_and_kick():
    twitch = normalize_channel("twitch", "xQc")
    assert twitch.identifier == "xqc"
    assert twitch.url == "https://www.twitch.tv/xQc"

    kick = normalize_channel("kick", "https://kick.com/nicklee")
    assert kick.identifier == "nicklee"
    assert kick.url == "https://kick.com/nicklee"


def test_normalize_rumble_forms():
    assert normalize_channel("rumble", "BannonsWarRoom").identifier == "c/BannonsWarRoom"
    channel = normalize_channel("rumble", "https://rumble.com/c/BannonsWarRoom")
    assert channel.identifier == "c/BannonsWarRoom"
    assert channel.url == "https://rumble.com/c/BannonsWarRoom"
    user = normalize_channel("rumble", "https://rumble.com/user/SomeUser")
    assert user.identifier == "user/SomeUser"
    video = normalize_channel("rumble", "https://rumble.com/v6m1ds9-warroom-live.html")
    assert is_direct_rumble_video(video.url)
    assert rumble_live_page_url(video.url) is None


@pytest.mark.parametrize(
    "platform,raw",
    [
        ("youtube", "https://www.twitch.tv/xqc"),
        ("twitch", "https://www.youtube.com/@xqc"),
        ("rumble", "https://example.com/c/foo"),
        ("youtube", "http://127.0.0.1/@foo"),
        ("kick", "https://kick.com.evil.test/foo"),
    ],
)
def test_rejects_foreign_hosts(platform, raw):
    with pytest.raises(ChannelValidationError):
        normalize_channel(platform, raw)


def test_rejects_credentials_in_url():
    with pytest.raises(ChannelValidationError):
        normalize_channel("twitch", "https://user:pass@www.twitch.tv/xqc")


def test_rejects_whitespace_and_unknown_platform():
    with pytest.raises(ChannelValidationError):
        normalize_channel("youtube", "@bad name")
    with pytest.raises(ChannelValidationError):
        normalize_channel("vimeo", "abc")


def test_rumble_live_page_url():
    assert (
        rumble_live_page_url("https://rumble.com/c/BannonsWarRoom")
        == "https://rumble.com/c/BannonsWarRoom/live"
    )
    assert (
        rumble_live_page_url("https://rumble.com/user/SomeUser")
        == "https://rumble.com/user/SomeUser/live"
    )
    assert (
        rumble_live_page_url("https://rumble.com/c/Foo/live")
        == "https://rumble.com/c/Foo/live"
    )


def test_extract_rumble_embed_url():
    html = 'foo "embedUrl":"https://rumble.com/embed/v7g4n0q/" bar'
    assert extract_rumble_embed_url(html) == "https://rumble.com/embed/v7g4n0q/"
    assert extract_rumble_embed_url("<html>nothing</html>") is None
    assert extract_rumble_embed_url('"embedUrl":"https://evil.test/embed/abc/"') is None


def test_rumble_alternate_channel():
    user = rumble_alternate_channel("c/BonginoReport")
    assert user.identifier == "user/BonginoReport"
    assert user.url == "https://rumble.com/user/BonginoReport"
    assert rumble_alternate_channel("user/Bongino").identifier == "c/Bongino"
    assert rumble_alternate_channel("https://rumble.com/v6m1ds9-x.html") is None
    assert rumble_alternate_channel("c/") is None
