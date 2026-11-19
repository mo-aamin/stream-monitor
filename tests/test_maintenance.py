from __future__ import annotations

import logging
import os
import time

import pytest

from app.main import HealthCheckAccessFilter
from app.services.maintenance import Maintenance, prune_recording_logs

DAY = 86400


def _recording(ctx, *, finished: bool = True) -> int:
    row = ctx.db.create_recording(
        channel_id=None,
        platform="youtube",
        channel_name="Test",
        title="Stream",
        video_id=None,
        url="https://example.com",
        log_path="",
    )
    recording_id = int(row["id"])
    log_path = ctx.config.recording_log_dir / f"{recording_id}.log"
    ctx.db.update_recording(recording_id, log_path=str(log_path))
    if finished:
        ctx.db.finish_recording(recording_id, status="completed", exit_code=0)
    return recording_id


def _log(ctx, name: str, age_days: float, content: str = "x" * 100):
    path = ctx.config.recording_log_dir / name
    path.write_text(content)
    mtime = time.time() - age_days * DAY
    os.utime(path, (mtime, mtime))
    return path


def test_prunes_old_and_orphan_logs(ctx):
    old_id = _recording(ctx)
    new_id = _recording(ctx)
    active_id = _recording(ctx, finished=False)
    old = _log(ctx, f"{old_id}.log", 45)
    new = _log(ctx, f"{new_id}.log", 2)
    active = _log(ctx, f"{active_id}.log", 90)
    orphan = _log(ctx, "99999.log", 1)
    other = _log(ctx, "notes.txt", 400)

    result = prune_recording_logs(ctx.config, ctx.db, ctx.settings)

    assert result["deleted"] == 2
    assert result["bytes"] == 200
    assert not old.exists()
    assert not orphan.exists()
    assert new.exists()
    assert active.exists()
    assert other.exists()
    assert ctx.db.get_recording(old_id)["log_path"] is None
    assert ctx.db.get_recording(new_id)["log_path"]
    events = ctx.db.list_events(limit=5)
    assert any("Pruned 2 recording log" in event["message"] for event in events)


def test_in_memory_active_recordings_are_protected(ctx):
    recording_id = _recording(ctx)
    path = _log(ctx, f"{recording_id}.log", 60)
    prune_recording_logs(ctx.config, ctx.db, ctx.settings, active_ids={recording_id})
    assert path.exists()


def test_retention_zero_keeps_everything(ctx):
    recording_id = _recording(ctx)
    old = _log(ctx, f"{recording_id}.log", 1000)
    orphan = _log(ctx, "12345.log", 1000)
    ctx.settings.update({"recording_log_retention_days": 0})

    result = prune_recording_logs(ctx.config, ctx.db, ctx.settings)

    assert result["deleted"] == 0
    assert old.exists() and orphan.exists()


def test_does_not_follow_symlinks_outside_log_dir(ctx, tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text("keep me")
    link = ctx.config.recording_log_dir / "55555.log"
    link.symlink_to(outside)
    prune_recording_logs(ctx.config, ctx.db, ctx.settings)
    assert outside.exists()


def test_no_event_when_nothing_pruned(ctx):
    before = len(ctx.db.list_events(limit=100))
    prune_recording_logs(ctx.config, ctx.db, ctx.settings)
    assert len(ctx.db.list_events(limit=100)) == before


def test_maintenance_run_once_compacts_database(ctx):
    maintenance = Maintenance(ctx.config, ctx.db, ctx.settings, lambda: set())
    result = maintenance.run_once()
    assert "compact" in result
    # Compaction is skipped until a day has passed.
    assert "compact" not in maintenance.run_once()


@pytest.mark.parametrize(
    ("path", "status", "kept"),
    [
        ("/healthz", 200, False),
        ("/healthz?x=1", 200, False),
        ("/healthz", 503, True),
        ("/api/status", 200, True),
        ("/", 200, True),
    ],
)
def test_health_check_access_filter(path, status, kept):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", path, "1.1", status),
        None,
    )
    assert HealthCheckAccessFilter().filter(record) is kept
