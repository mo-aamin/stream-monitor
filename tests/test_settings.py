from __future__ import annotations

import pytest

from app.services.settings import SettingsService


def test_defaults_are_typed(ctx):
    settings = ctx.settings.typed()
    assert settings["monitor_enabled"] is True
    assert settings["check_interval"] == 60
    assert settings["impersonate_kick"] is True
    assert settings["impersonate_youtube"] is False
    assert isinstance(settings["ytdlp_path"], str)


def test_recording_limit_setting_removed(ctx):
    assert "max_concurrent_recordings" not in ctx.settings.typed()
    assert "max_concurrent_recordings" not in ctx.settings.raw()


def test_recordings_dir_is_not_a_setting(ctx):
    with pytest.raises(ValueError, match="Unknown setting"):
        ctx.settings.update({"recordings_dir": "/tmp/should-not-be-a-setting"})


def test_update_and_reload(ctx):
    settings = ctx.settings
    settings.update({"check_interval": 120, "monitor_enabled": False, "merge_output_format": "mkv"})
    fresh = SettingsService(ctx.db, ctx.config)
    typed = fresh.typed()
    assert typed["check_interval"] == 120
    assert typed["monitor_enabled"] is False
    assert typed["merge_output_format"] == "mkv"


@pytest.mark.parametrize(
    "values,needle",
    [
        ({"check_interval": 5}, "between"),
        ({"check_interval": "abc"}, "whole number"),
        ({"merge_output_format": "avi"}, "must be one of"),
        ({"impersonate_target": "bad target!"}, "invalid characters"),
        ({"proxy": "ftp://host"}, "Proxy"),
        ({"extra_ytdlp_args": "--foo 'unterminated"}, "malformed"),
        ({"filename_template": "no extension"}, "%(ext)s"),
        ({"filename_template": "../../escape/%(ext)s"}, "parent-directory"),
        ({"ytdlp_path": ""}, "cannot be empty"),
        ({"unknown_key": "x"}, "Unknown setting"),
    ],
)
def test_invalid_values_rejected(ctx, values, needle):
    with pytest.raises(ValueError) as error:
        ctx.settings.update(values)
    assert needle.lower() in str(error.value).lower()


def test_cookies_file_must_exist(ctx, tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(ValueError, match="Cookies file not found"):
        ctx.settings.update({"cookies_file": str(missing)})

    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    ctx.settings.update({"cookies_file": str(cookies)})
    assert ctx.settings.typed()["cookies_file"] == str(cookies)


def test_boolean_coercion(ctx):
    ctx.settings.update({"monitor_enabled": "on", "impersonate_twitch": 0})
    typed = ctx.settings.typed()
    assert typed["monitor_enabled"] is True
    assert typed["impersonate_twitch"] is False


def test_filename_template_default_has_no_id_or_live_date(ctx):
    template = ctx.settings.typed()["filename_template"]
    assert template == "%(fulltitle).120B.%(ext)s"
    assert "%(id)s" not in template


def test_legacy_filename_template_is_migrated(ctx):
    ctx.db.save_settings({"filename_template": "%(title).120B [%(id)s].%(ext)s"})
    fresh = SettingsService(ctx.db, ctx.config)
    assert fresh.typed()["filename_template"] == "%(fulltitle).120B.%(ext)s"


def test_custom_filename_template_is_kept(ctx):
    ctx.settings.update({"filename_template": "%(title)s [custom].%(ext)s"})
    fresh = SettingsService(ctx.db, ctx.config)
    assert fresh.typed()["filename_template"] == "%(title)s [custom].%(ext)s"


def test_log_settings_defaults_and_limits(ctx):
    cfg = ctx.settings.typed()
    assert cfg["recording_log_retention_days"] == 30
    assert cfg["recording_log_max_mb"] == 10
    ctx.settings.update({"recording_log_retention_days": 0, "recording_log_max_mb": 500})
    assert ctx.settings.typed()["recording_log_retention_days"] == 0
    with pytest.raises(ValueError, match="between"):
        ctx.settings.update({"recording_log_max_mb": 0})
    with pytest.raises(ValueError, match="between"):
        ctx.settings.update({"recording_log_retention_days": -1})


def test_stop_timeout_setting(ctx):
    assert ctx.settings.typed()["stop_timeout"] == 3600
    ctx.settings.update({"stop_timeout": 7200})
    assert ctx.settings.typed()["stop_timeout"] == 7200
    for bad in (60, 20000):
        with pytest.raises(ValueError, match="between"):
            ctx.settings.update({"stop_timeout": bad})


def test_live_from_start_max_age_setting(ctx):
    assert ctx.settings.typed()["youtube_live_from_start_max_age"] == 120
    ctx.settings.update({"youtube_live_from_start_max_age": 0})
    assert ctx.settings.typed()["youtube_live_from_start_max_age"] == 0
    with pytest.raises(ValueError, match="between"):
        ctx.settings.update({"youtube_live_from_start_max_age": -1})
