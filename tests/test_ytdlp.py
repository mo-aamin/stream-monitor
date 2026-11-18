from __future__ import annotations

from datetime import datetime

import pytest

from app.services.ytdlp import (
    build_check_args,
    build_output_template,
    build_record_args,
    classify_failure,
    extract_output_path,
    parse_metadata,
    short_error,
)


@pytest.fixture
def cfg():
    return {
        "ytdlp_path": "/usr/local/bin/yt-dlp",
        "deno_path": "/usr/local/bin/deno",
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "cookies_file": "",
        "proxy": "",
        "impersonate_target": "chrome",
        "impersonate_youtube": False,
        "impersonate_twitch": True,
        "impersonate_kick": True,
        "impersonate_rumble": True,
        "filename_template": "%(fulltitle).120B.%(ext)s",
        "merge_output_format": "mp4",
        "format_selector": "",
        "youtube_live_from_start": True,
        "rumble_live_from_start": False,
        "write_info_json": False,
        "extra_ytdlp_args": "",
    }


def test_build_check_args_uses_impersonation_for_kick(cfg):
    args = build_check_args(cfg, "kick", "https://kick.com/nicklee")
    assert args[0] == cfg["ytdlp_path"]
    assert "--impersonate" in args
    assert args[args.index("--impersonate") + 1] == "chrome"
    assert "--js-runtimes" in args
    assert f"deno:{cfg['deno_path']}" in args
    assert args[-1] == "https://kick.com/nicklee"


def test_build_check_args_skips_impersonation_for_youtube(cfg):
    args = build_check_args(cfg, "youtube", "https://www.youtube.com/@x/live")
    assert "--impersonate" not in args


def test_build_record_args(cfg):
    args = build_record_args(cfg, "youtube", "https://www.youtube.com/watch?v=abc", "/out/file.mp4")
    assert "--live-from-start" in args
    assert "--merge-output-format" in args
    assert "mp4" in args
    assert "--print" in args
    assert "after_move:%(filepath)s" in args
    assert args[-1] == "https://www.youtube.com/watch?v=abc"
    # MP4 output prefers H.264/AAC so YouTube does not hand us VP9-in-MP4.
    assert args[args.index("-S") + 1] == "vcodec:h264,acodec:aac"
    assert "-f" not in args

    rumble =build_record_args(cfg, "rumble", "https://rumble.com/embed/abc/", "/out/file.mp4")
    assert "--live-from-start" not in rumble


def test_build_record_args_extra_and_format(cfg):
    cfg["extra_ytdlp_args"] = "--hls-use-mpegts --retry-sleep 3"
    cfg["format_selector"] = "bv*+ba/b"
    args = build_record_args(cfg, "twitch", "https://www.twitch.tv/xqc", "/out/file.mp4")
    assert "--hls-use-mpegts" in args
    assert args[args.index("-f") + 1] == "bv*+ba/b"
    # A user format selector takes precedence over the MP4 compatibility sort.
    assert "-S" not in args


@pytest.mark.parametrize(
    "overrides",
    [
        {"merge_output_format": "mkv"},
        {"merge_output_format": ""},
        {"extra_ytdlp_args": "-S res,fps"},
        {"extra_ytdlp_args": "--format-sort=res"},
    ],
)
def test_build_record_args_skips_mp4_format_sort(cfg, overrides):
    cfg.update(overrides)
    args = build_record_args(cfg, "youtube", "https://www.youtube.com/watch?v=abc", "/out/x")
    assert "vcodec:h264,acodec:aac" not in args


def test_output_template_has_prefix_and_escapes_percent(cfg, tmp_path):
    template = build_output_template(
        str(tmp_path / "recordings"),
        cfg["filename_template"],
        "kick",
        "100% Legends",
        datetime(2026, 1, 2, 3, 4, 5),
    )
    assert "2026-01-02_03-04-05" in template
    assert "Kick" in template
    assert "100%% Legends" in template
    assert template.endswith("%(fulltitle).120B.%(ext)s")


def test_parse_metadata_live_and_offline():
    live = parse_metadata(
        '{"id":"abc","title":"Hi","channel":"Chan","is_live":true,"live_status":"is_live",'
        '"webpage_url":"https://example.test/abc"}'
    )
    assert live["is_live"] is True
    assert live["channel_name"] == "Chan"

    offline = parse_metadata(
        '{"id":"abc","title":"VOD","live_status":"not_live","channel":"Chan"}'
    )
    assert offline["is_live"] is False


def test_parse_metadata_prefers_fulltitle_without_live_date():
    live = parse_metadata(
        '{"id":"abc","title":"Market Open 2026-11-18 08:25","fulltitle":"Market Open",'
        '"channel":"Chan","is_live":true}'
    )
    assert live["title"] == "Market Open"


def test_parse_metadata_playlist():
    payload = '{"_type":"playlist","entries":[{"id":"1","title":"T","is_live":true}]}'
    assert parse_metadata(payload)["title"] == "T"


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("ERROR: This channel is not currently live", "offline"),
        ("ERROR: The channel is offline", "offline"),
        ("ERROR: Unable to impersonate: curl_cffi not installed", "impersonate"),
        ("ERROR: Sign in to confirm you're not a bot", "signin"),
        ("ERROR: Some completely different failure", "error"),
    ],
)
def test_classify_failure(stderr, expected):
    assert classify_failure(stderr) == expected


def test_short_error_prefers_error_line():
    stderr = "WARNING: x\nERROR: boom happened\nmore"
    assert short_error(stderr) == "boom happened"


@pytest.mark.parametrize(
    "line,path",
    [
        ("[download] Destination: /recordings/a.mp4", "/recordings/a.mp4"),
        ('[Merger] Merging formats into "/recordings/a.mkv"', "/recordings/a.mkv"),
        ("[download] /recordings/a.webm has already been downloaded", "/recordings/a.webm"),
        ('[MoveFiles] Moving file "/tmp/a.mp4" to "/recordings/a.mp4"', "/recordings/a.mp4"),
        ("/recordings/a.mp4", "/recordings/a.mp4"),
        ("[download] Destination: /recordings/a.mp4.part", "/recordings/a.mp4"),
        ("[download] 42.0% of 10MiB", None),
    ],
)
def test_extract_output_path(line, path):
    assert extract_output_path(line) == path
