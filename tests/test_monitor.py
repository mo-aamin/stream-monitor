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
