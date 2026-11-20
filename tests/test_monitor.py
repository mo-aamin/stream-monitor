from __future__ import annotations

import asyncio

from app.services.monitor import Monitor, find_output_by_prefix


def test_status_when_not_started(ctx):
    monitor: Monitor = ctx.monitor
    status = monitor.status()
    assert status["state"] == "stopped"
    assert status["loop_running"] is False
    assert status["active"] == []


def test_find_output_by_prefix(tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir()
    prefix = "2026-11-13_16-37-07 - YouTube - Lofi Girl - "
    final = directory / f"{prefix}Title [id].mp4"
    part = directory / f"{prefix}Title [id].mp4.part"
    intermediate = directory / f"{prefix}Title [id].f248.mp4.part"
    other = directory / "2026-11-13_16-37-07 - Kick - xQc - Other [id].mp4"
    sidecar = directory / f"{prefix}Title [id].mp4.ytdl"
    for path in (final, part, intermediate, other, sidecar):
        path.write_bytes(b"x")

    assert find_output_by_prefix(str(directory), prefix) == str(final)

    final.unlink()
    assert find_output_by_prefix(str(directory), prefix) == str(part)

    part.unlink()
    # Only intermediate fragments remain, so nothing is reported yet.
    assert find_output_by_prefix(str(directory), prefix) is None


def test_find_output_by_prefix_missing_directory(ctx, tmp_path):
    assert find_output_by_prefix(str(tmp_path / "missing"), "prefix") is None


def test_stop_unknown_recording(ctx):
    monitor: Monitor = ctx.monitor
    assert asyncio.run(monitor.stop_recording(12345)) is False
    assert asyncio.run(monitor.stop_channel_recording(12345)) is False


def test_forget_channel(ctx):
    monitor: Monitor = ctx.monitor
    sentinel = object()
    monitor._fail_cooldown[1] = 100.0
    monitor._channel_locks[1] = sentinel  # type: ignore[assignment]
    monitor.forget_channel(1)
    assert 1 not in monitor._fail_cooldown
    assert 1 not in monitor._channel_locks


def test_rumble_error_classification():
    error = Monitor._rumble_error
    assert "404" in error(404, "")
    assert "not found" in error(410, "")
    assert "HTTP 403" in error(403, "")
    assert error(200, "<html>ok</html>") is None
    assert error(200, "<title>404 Not found</title>") is not None


def _make_active(ctx, tmp_path, prefix):
    import time
    from collections import deque

    from app.services.monitor import ActiveRecording

    channel = ctx.db.add_channel("youtube", "@x", "https://www.youtube.com/@x/live", "X")
    log_path = tmp_path / "rec.log"
    recording = ctx.db.create_recording(
        channel_id=channel["id"],
        platform="youtube",
        channel_name="X",
        title="Live",
        video_id="abc",
        url="https://www.youtube.com/watch?v=abc",
        log_path=str(log_path),
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    active = ActiveRecording(
        recording_id=recording["id"],
        channel_id=channel["id"],
        platform="youtube",
        channel_name="X",
        title="Live",
        url="https://www.youtube.com/watch?v=abc",
        log_path=log_path,
        started_at=recording["started_at"],
        started_epoch=time.time(),
        started_monotonic=time.monotonic(),
        output_dir=str(output_dir),
        file_prefix=prefix,
        log_handle=open(log_path, "w", encoding="utf-8"),
        output_tail=deque(["ERROR: Did not get any data blocks"]),
    )
    return active, output_dir, log_path


def test_finalize_nonzero_exit_with_complete_file_is_completed(ctx, tmp_path):
    prefix = "2026-11-18_08-23-58 - YouTube - X - "
    active, output_dir, log_path = _make_active(ctx, tmp_path, prefix)
    (output_dir / f"{prefix}Live [abc].mp4").write_bytes(b"x" * 1024)

    asyncio.run(ctx.monitor._finalize(active, 1, None))

    recording = ctx.db.get_recording(active.recording_id)
    assert recording["status"] == "completed"
    assert recording["exit_code"] == 1
    assert not recording["error"]
    assert "Did not get any data blocks" in log_path.read_text(encoding="utf-8")


def test_finalize_nonzero_exit_with_only_partials_is_failed(ctx, tmp_path):
    prefix = "2026-11-18_08-23-58 - YouTube - X - "
    active, output_dir, _ = _make_active(ctx, tmp_path, prefix)
    (output_dir / f"{prefix}Live [abc].f248.mp4.part").write_bytes(b"x" * 1024)
    (output_dir / f"{prefix}Live [abc].mp4.part").write_bytes(b"x" * 1024)

    asyncio.run(ctx.monitor._finalize(active, 1, None))

    recording = ctx.db.get_recording(active.recording_id)
    assert recording["status"] == "failed"


def _rumble_channel(ctx, identifier):
    return ctx.db.add_channel("rumble", identifier, f"https://rumble.com/{identifier}")


def test_rumble_check_switches_c_to_user_when_channel_missing(ctx):
    monitor: Monitor = ctx.monitor
    channel = _rumble_channel(ctx, "c/BonginoReport")
    fetched = []

    async def fake_fetch(url, cfg):
        fetched.append(url)
        if "/c/" in url:
            return None, "Rumble channel not found (HTTP 410). Check the channel name.", 410
        return "<html>no live stream</html>", None, 200

    monitor._fetch_rumble_page = fake_fetch
    result = asyncio.run(monitor._perform_check(dict(channel)))

    assert fetched == [
        "https://rumble.com/c/BonginoReport/live",
        "https://rumble.com/user/BonginoReport/live",
    ]
    assert result.status == "offline"
    saved = ctx.db.get_channel(channel["id"])
    assert saved["identifier"] == "user/BonginoReport"
    assert saved["url"] == "https://rumble.com/user/BonginoReport"


def test_rumble_check_keeps_channel_when_both_forms_missing(ctx):
    monitor: Monitor = ctx.monitor
    channel = _rumble_channel(ctx, "c/NoSuchName")

    async def fake_fetch(url, cfg):
        return None, "Rumble channel not found (HTTP 404). Check the channel name.", 404

    monitor._fetch_rumble_page = fake_fetch
    result = asyncio.run(monitor._perform_check(dict(channel)))

    assert result.status == "error"
    assert "not found" in result.message
    assert ctx.db.get_channel(channel["id"])["identifier"] == "c/NoSuchName"


def test_recording_log_size_cap(ctx, tmp_path):
    from app.services.monitor import ActiveRecording

    log_path = tmp_path / "1.log"
    handle = open(log_path, "a", encoding="utf-8")
    active = ActiveRecording(
        recording_id=1,
        channel_id=1,
        platform="youtube",
        channel_name="Test",
        title="Stream",
        url="https://example.com",
        log_path=log_path,
        started_at="2026-01-01T00:00:00",
        started_epoch=0.0,
        started_monotonic=0.0,
        output_dir=str(tmp_path),
        file_prefix="x",
        log_handle=handle,
        log_limit_bytes=1000,
    )
    line = "y" * 99  # 100 bytes with the newline
    for _ in range(50):
        ctx.monitor._handle_output_line(active, line)
    handle.close()

    content = log_path.read_text()
    assert content.count(line) == 10
    assert content.count("# Log truncated") == 1
    assert active.log_truncated is True
    assert len(active.output_tail) == 50
