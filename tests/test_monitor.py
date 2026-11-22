from __future__ import annotations

import asyncio
import os

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


def test_finalize_remuxes_leftover_part_file(ctx, tmp_path, monkeypatch):
    import app.services.monitor as monitor_module

    prefix = "2026-11-20_08-00-00 - YouTube - X - "
    active, output_dir, log_path = _make_active(ctx, tmp_path, prefix)
    part = output_dir / f"{prefix}Live.mp4.part"
    part.write_bytes(b"ts" * 512)
    active.stop_requested = True

    async def fake_ensure_playable(filepath, cfg):
        assert filepath == str(part)
        target = filepath[:-5]
        os.replace(filepath, target)
        return target, "Remuxed mpegts recording to a playable MP4."

    monkeypatch.setattr(monitor_module, "ensure_playable", fake_ensure_playable)
    asyncio.run(ctx.monitor._finalize(active, 255, None))

    recording = ctx.db.get_recording(active.recording_id)
    assert recording["status"] == "stopped"
    assert recording["filename"] == f"{prefix}Live.mp4"
    assert recording["size_bytes"] == 1024
    assert "Remuxed mpegts" in log_path.read_text(encoding="utf-8")


def test_finalize_survives_remux_crash(ctx, tmp_path, monkeypatch):
    import app.services.monitor as monitor_module

    prefix = "2026-11-20_08-00-00 - YouTube - X - "
    active, output_dir, _ = _make_active(ctx, tmp_path, prefix)
    (output_dir / f"{prefix}Live.mp4").write_bytes(b"x" * 1024)

    async def boom(filepath, cfg):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(monitor_module, "ensure_playable", boom)
    asyncio.run(ctx.monitor._finalize(active, 0, None))

    recording = ctx.db.get_recording(active.recording_id)
    assert recording["status"] == "completed"
    assert (output_dir / f"{prefix}Live.mp4").exists()


def test_interrupt_signals_only_ytdlp(monkeypatch):
    import signal

    import app.services.monitor as monitor_module

    sent = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def send_signal(self, sig):
            sent.append(("proc", sig))

    def fake_killpg(pgid, sig):
        sent.append(("group", sig))

    monkeypatch.setattr(monitor_module.os, "killpg", fake_killpg)
    monkeypatch.setattr(monitor_module.os, "getpgid", lambda pid: pid)
    Monitor._interrupt(Monitor, FakeProcess())  # type: ignore[arg-type]
    assert sent == [("proc", signal.SIGINT)]

    sent.clear()
    Monitor._kill(Monitor, FakeProcess())  # type: ignore[arg-type]
    assert sent == [("group", signal.SIGKILL)]


class _FakeYtdlp:
    """Stands in for a yt-dlp process: exits some time after SIGINT or on SIGKILL."""

    def __init__(self, exit_after_sigint: float | None):
        import signal

        self._signal = signal
        self.pid = 4242
        self.returncode = None
        self.signals: list[int] = []
        self._exit_after_sigint = exit_after_sigint
        self._exited = asyncio.Event()

    def _exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self._exited.set()

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == self._signal.SIGINT and self._exit_after_sigint is not None:
            asyncio.get_running_loop().call_later(self._exit_after_sigint, self._exit, 0)
        elif sig == self._signal.SIGKILL:
            self._exit(-9)

    async def wait(self):
        await self._exited.wait()
        return self.returncode


def _setup_stop(ctx, tmp_path, monkeypatch, process, remux_seconds):
    import app.services.monitor as monitor_module

    prefix = "2026-11-20_08-00-00 - Rumble - X - "
    active, output_dir, _ = _make_active(ctx, tmp_path, prefix)
    (output_dir / f"{prefix}Live.mp4").write_bytes(b"x" * 1024)
    remux_done = []

    async def slow_ensure_playable(filepath, cfg):
        await asyncio.sleep(remux_seconds)
        remux_done.append(filepath)
        return filepath, None

    def fake_killpg(pgid, sig):
        process.send_signal(sig)

    monkeypatch.setattr(monitor_module, "ensure_playable", slow_ensure_playable)
    monkeypatch.setattr(monitor_module.os, "killpg", fake_killpg)
    monkeypatch.setattr(monitor_module.os, "getpgid", lambda pid: pid)

    monitor = ctx.monitor
    active.process = process

    async def consume():
        code = await process.wait()
        await monitor._finalize(active, code, None)

    def register():
        active.task = asyncio.create_task(consume(), name="recording-test")
        monitor._active[active.recording_id] = active
        monitor._active_by_channel[active.channel_id] = active.recording_id

    return active, register, remux_done


def test_stop_allows_long_post_processing(ctx, tmp_path, monkeypatch):
    import signal

    process = _FakeYtdlp(exit_after_sigint=0.3)
    active, register, remux_done = _setup_stop(ctx, tmp_path, monkeypatch, process, 0.3)
    ctx.settings.update({"stop_timeout": 120})

    async def run():
        register()
        assert await ctx.monitor.stop_recording(active.recording_id) is True

    asyncio.run(run())
    assert process.signals == [signal.SIGINT]
    assert remux_done
    assert ctx.db.get_recording(active.recording_id)["status"] == "stopped"


def test_stop_timeout_kill_does_not_cancel_running_remux(ctx, tmp_path, monkeypatch):
    import signal

    import app.services.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "KILL_SETTLE_TIMEOUT", 0.05)
    process = _FakeYtdlp(exit_after_sigint=None)  # ignores SIGINT
    active, register, remux_done = _setup_stop(ctx, tmp_path, monkeypatch, process, 0.4)

    async def run():
        register()
        assert await ctx.monitor.stop_recording(active.recording_id, timeout=0.1) is True

    asyncio.run(run())
    assert process.signals == [signal.SIGINT, signal.SIGKILL]
    assert remux_done, "the app remux must finish, not be cancelled"
    recording = ctx.db.get_recording(active.recording_id)
    assert recording["status"] == "stopped"
    assert active.recording_id not in ctx.monitor._active


def test_request_stop_returns_immediately_and_is_idempotent(ctx, tmp_path, monkeypatch):
    import signal

    process = _FakeYtdlp(exit_after_sigint=0.2)
    active, register, remux_done = _setup_stop(ctx, tmp_path, monkeypatch, process, 0.1)

    async def run():
        register()
        assert ctx.monitor.request_stop_channel(active.channel_id) is True
        first = active.stop_task
        assert ctx.monitor.request_stop(active.recording_id) is True
        assert active.stop_task is first
        assert not active.task.done()
        await first

    asyncio.run(run())
    assert process.signals == [signal.SIGINT]
    assert remux_done
    assert ctx.db.get_recording(active.recording_id)["status"] == "stopped"
    assert ctx.monitor.request_stop(active.recording_id) is False


def test_live_check_keeps_cooldown_and_offline_clears_it(ctx):
    import time

    from app.services.monitor import CheckResult

    monitor = ctx.monitor
    channel = ctx.db.add_channel("youtube", "@cd", "https://www.youtube.com/@cd/live", "CD")
    cid = channel["id"]
    monitor._fail_cooldown[cid] = time.monotonic() + 300
    monitor._quick_retries[cid] = 2

    monitor._apply_result(channel, CheckResult(status="live", live=True, title="T"))
    assert cid in monitor._fail_cooldown
    assert monitor._quick_retries[cid] == 2

    monitor._apply_result(ctx.db.get_channel(cid), CheckResult(status="offline"))
    assert cid not in monitor._fail_cooldown
    assert cid not in monitor._quick_retries


def test_startup_failures_get_quick_retries_then_cooldown(ctx, monkeypatch):
    import time

    import app.services.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "QUICK_RETRY_DELAY", 0.05)
    monitor = ctx.monitor
    channel = ctx.db.add_channel("youtube", "@qr", "https://www.youtube.com/@qr/live", "QR")
    cid = channel["id"]
    scheduled = []
    monkeypatch.setattr(monitor, "schedule_channel_check", lambda channel_id: scheduled.append(channel_id))

    async def run():
        for _ in range(2):
            before = time.monotonic()
            monitor._set_fail_cooldown(cid, ran_seconds=3)
            assert monitor._fail_cooldown[cid] - before < 1
        monitor._set_fail_cooldown(cid, ran_seconds=3)
        remaining = monitor._fail_cooldown[cid] - time.monotonic()
        assert 299 < remaining <= 300
        await asyncio.sleep(1.3)

    asyncio.run(run())
    assert monitor._quick_retries[cid] == 2
    assert scheduled == [cid, cid]
    events = [e["message"] for e in ctx.db.list_events(limit=10)]
    assert any("attempt 1 of 2" in m for m in events)
    assert any("attempt 2 of 2" in m for m in events)


def test_late_failure_uses_full_cooldown(ctx, monkeypatch):
    import time

    monitor = ctx.monitor
    channel = ctx.db.add_channel("youtube", "@lf", "https://www.youtube.com/@lf/live", "LF")
    cid = channel["id"]
    monkeypatch.setattr(monitor, "schedule_channel_check", lambda channel_id: None)

    async def run():
        monitor._set_fail_cooldown(cid, ran_seconds=600)

    asyncio.run(run())
    assert cid not in monitor._quick_retries
    assert monitor._fail_cooldown[cid] - time.monotonic() > 299


def test_successful_recording_resets_retry_state(ctx, tmp_path):
    import time

    prefix = "2026-11-20_08-00-00 - YouTube - X - "
    active, output_dir, _ = _make_active(ctx, tmp_path, prefix)
    (output_dir / f"{prefix}Live.mp4").write_bytes(b"x" * 1024)
    ctx.monitor._fail_cooldown[active.channel_id] = time.monotonic() + 300
    ctx.monitor._quick_retries[active.channel_id] = 1

    asyncio.run(ctx.monitor._finalize(active, 0, None))

    assert ctx.db.get_recording(active.recording_id)["status"] == "completed"
    assert active.channel_id not in ctx.monitor._fail_cooldown
    assert active.channel_id not in ctx.monitor._quick_retries
