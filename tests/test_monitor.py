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
