from __future__ import annotations

import pytest

from app.database import DuplicateChannelError


def test_channel_crud_and_status(ctx):
    db = ctx.db
    channel = db.add_channel(
        "youtube", "@Example", "https://www.youtube.com/@Example/live", name="Example"
    )
    assert channel["enabled"] == 1
    assert db.get_channel(channel["id"])["name"] == "Example"
    assert db.count_channels() == {"total": 1, "enabled": 1}

    with pytest.raises(DuplicateChannelError):
        db.add_channel("youtube", "@example", "https://www.youtube.com/@Example/live")

    db.set_channel_status(channel["id"], "live", title="A stream", live=True)
    updated = db.get_channel(channel["id"])
    assert updated["last_status"] == "live"
    assert updated["last_title"] == "A stream"
    assert updated["last_live_at"]

    assert db.update_channel(channel["id"], enabled=0) is True
    assert db.get_channel(channel["id"])["enabled"] == 0
    assert db.count_channels() == {"total": 1, "enabled": 0}

    assert db.delete_channel(channel["id"]) is True
    assert db.get_channel(channel["id"]) is None


def test_recording_lifecycle_and_stats(ctx):
    db = ctx.db
    channel = db.add_channel("twitch", "xqc", "https://www.twitch.tv/xqc")
    recording = db.create_recording(
        channel_id=channel["id"],
        platform="twitch",
        channel_name="xQc",
        title="Live",
        video_id="vid",
        url="https://www.twitch.tv/xqc",
        log_path="/tmp/log.txt",
        ytdlp_version="2026.01.01",
    )
    assert recording["status"] == "recording"
    assert len(db.active_recordings()) == 1
    assert db.active_recording_for_channel(channel["id"])["id"] == recording["id"]

    db.update_recording(recording["id"], filepath="/tmp/video.mp4", filename="video.mp4")
    db.finish_recording(
        recording["id"], status="completed", exit_code=0, filepath="/tmp/video.mp4",
        filename="video.mp4", size_bytes=2048,
    )
    finished = db.get_recording(recording["id"])
    assert finished["status"] == "completed"
    assert finished["size_bytes"] == 2048
    assert finished["finished_at"] is not None
    assert finished["duration_seconds"] is not None
    assert db.active_recordings() == []

    stats = db.recording_stats()
    assert stats["total"] == 1
    assert stats["total_bytes"] == 2048

    assert db.delete_recording(recording["id"]) is True
    assert db.get_recording(recording["id"]) is None


def test_mark_orphaned_recordings(ctx):
    db = ctx.db
    channel = db.add_channel("kick", "nicklee", "https://kick.com/nicklee")
    recording = db.create_recording(
        channel_id=channel["id"],
        platform="kick",
        channel_name="nicklee",
        title="Live",
        video_id=None,
        url="https://kick.com/nicklee",
        log_path="",
    )
    assert db.mark_orphaned_recordings() == 1
    row = db.get_recording(recording["id"])
    assert row["status"] == "interrupted"
    assert "restarted" in row["error"]


def test_settings_events_and_dashboard(ctx):
    db = ctx.db
    db.save_settings({"check_interval": "45"})
    assert db.get_setting("check_interval") == "45"
    assert db.get_setting("missing") is None

    db.add_event("info", "system", "hello")
    db.add_event("error", "recording", "bad", channel_id=None)
    events = db.list_events(limit=10)
    assert len(events) == 2
    assert events[0]["level"] == "error"
    assert len(db.list_events(level="info")) == 1

    stats = db.dashboard_stats()
    assert stats["channels_total"] == 0
    assert stats["recordings_total"] == 0
