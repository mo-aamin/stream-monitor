"""JSON API used by the web UI."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..database import DuplicateChannelError
from ..services.platforms import (
    ChannelValidationError,
    normalize_channel,
    platform_label,
)
from ..utils import human_size, tail_text

router = APIRouter(prefix="/api")


def _ctx(request: Request):
    return request.app.state.ctx


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body
        raise HTTPException(400, "Request body must be valid JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object.")
    return payload


def _clean_name(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:100] if text else None


# --------------------------------------------------------------------- status
@router.get("/status")
async def status(request: Request):
    ctx = _ctx(request)
    stats = ctx.db.dashboard_stats()
    stats["recordings_bytes_human"] = human_size(stats["recordings_bytes"])
    return {"ok": True, "monitor": ctx.monitor.status(), "stats": stats}


# -------------------------------------------------------------------- channels
@router.get("/channels")
async def list_channels(request: Request):
    ctx = _ctx(request)
    channels = ctx.db.list_channels()
    active = {item["channel_id"]: item for item in ctx.monitor.active_recordings()}
    for channel in channels:
        channel["active"] = active.get(channel["id"])
    return {"ok": True, "channels": channels}


@router.post("/channels", status_code=201)
async def add_channel(request: Request):
    ctx = _ctx(request)
    data = await _json(request)
    try:
        normalized = normalize_channel(
            str(data.get("platform") or ""), str(data.get("identifier") or "")
        )
    except ChannelValidationError as exc:
        raise HTTPException(400, str(exc)) from None
    try:
        channel = ctx.db.add_channel(
            normalized.platform,
            normalized.identifier,
            normalized.url,
            name=_clean_name(data.get("name")),
        )
    except DuplicateChannelError:
        raise HTTPException(409, "That channel is already in the list.") from None
    ctx.db.add_event(
        "info",
        "channel",
        f"Channel added: {platform_label(channel['platform'])} — {channel['identifier']}",
        channel["id"],
    )
    ctx.monitor.schedule_channel_check(channel["id"])
    return {"ok": True, "channel": channel, "message": "Channel added — live check queued."}


@router.post("/channels/bulk", status_code=201)
async def bulk_add_channels(request: Request):
    ctx = _ctx(request)
    data = await _json(request)
    platform = str(data.get("platform") or "")
    lines = str(data.get("lines") or "")
    name = _clean_name(data.get("name"))
    entries = [line.strip() for line in lines.splitlines() if line.strip()]
    if not entries:
        raise HTTPException(400, "Paste at least one channel, one per line.")
    if len(entries) > 200:
        raise HTTPException(400, "Please import at most 200 channels at once.")
    added: list[dict] = []
    skipped: list[str] = []
    errors: list[dict] = []
    for entry in entries:
        if len(added) >= 200:
            break
        try:
            normalized = normalize_channel(platform, entry)
            channel = ctx.db.add_channel(
                normalized.platform, normalized.identifier, normalized.url, name=name
            )
        except ChannelValidationError as exc:
            errors.append({"line": entry, "error": str(exc)})
            continue
        except DuplicateChannelError:
            skipped.append(entry)
            continue
        added.append(channel)
    if added:
        # One bounded check-all is safer than spawning one process per row.
        ctx.monitor.schedule_check_all()
        ctx.db.add_event(
            "info",
            "channel",
            f"Bulk import: added {len(added)} {platform_label(platform)} channel(s)",
        )
    return {
        "ok": True,
        "added": len(added),
        "skipped": len(skipped),
        "errors": errors[:20],
        "message": f"Added {len(added)} channel(s); {len(skipped)} duplicate(s), {len(errors)} invalid.",
    }


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(request: Request, channel_id: int):
    ctx = _ctx(request)
    channel = ctx.db.get_channel(channel_id)
    if channel is None:
        raise HTTPException(404, "Channel not found.")
    enabled = not bool(channel["enabled"])
    ctx.db.update_channel(channel_id, enabled=1 if enabled else 0)
    ctx.db.add_event(
        "info",
        "channel",
        f"Channel {('enabled' if enabled else 'disabled')}: {channel['identifier']}",
        channel_id,
    )
    if enabled:
        ctx.monitor.schedule_channel_check(channel_id)
    return {"ok": True, "enabled": enabled}


@router.delete("/channels/{channel_id}")
async def delete_channel(request: Request, channel_id: int):
    ctx = _ctx(request)
    channel = ctx.db.get_channel(channel_id)
    if channel is None:
        raise HTTPException(404, "Channel not found.")
    if ctx.monitor.is_recording(channel_id):
        await ctx.monitor.stop_channel_recording(channel_id, timeout=20.0)
    ctx.db.delete_channel(channel_id)
    ctx.monitor.forget_channel(channel_id)
    ctx.db.add_event(
        "info",
        "channel",
        f"Channel removed: {platform_label(channel['platform'])} — {channel['identifier']}",
        channel_id,
    )
    return {"ok": True, "message": "Channel removed."}


@router.post("/channels/{channel_id}/check")
async def check_channel(request: Request, channel_id: int):
    ctx = _ctx(request)
    result = await ctx.monitor.check_channel_now(channel_id)
    if result is None:
        raise HTTPException(404, "Channel not found or monitor is shutting down.")
    return {"ok": True, **result}


@router.post("/channels/{channel_id}/stop")
async def stop_channel_recording(request: Request, channel_id: int):
    ctx = _ctx(request)
    stopped = await ctx.monitor.stop_channel_recording(channel_id)
    if not stopped:
        raise HTTPException(404, "This channel is not recording.")
    return {"ok": True, "message": "Recording stopped."}


# --------------------------------------------------------------------- monitor
@router.post("/monitor/pause")
async def pause_monitor(request: Request):
    ctx = _ctx(request)
    await ctx.monitor.pause()
    return {"ok": True, "monitor": ctx.monitor.status()}


@router.post("/monitor/resume")
async def resume_monitor(request: Request):
    ctx = _ctx(request)
    await ctx.monitor.resume()
    return {"ok": True, "monitor": ctx.monitor.status()}


@router.post("/monitor/toggle")
async def toggle_monitor(request: Request):
    ctx = _ctx(request)
    # Base the decision on the actual loop state, not just the stored flag:
    # a stopped loop with monitor_enabled=1 (MONITOR_AUTOSTART=false) should
    # start when the user presses Resume.
    if ctx.monitor.state == "running":
        await ctx.monitor.pause()
    else:
        await ctx.monitor.resume()
    return {"ok": True, "monitor": ctx.monitor.status()}


@router.post("/monitor/check-all")
async def check_all(request: Request):
    ctx = _ctx(request)
    allow_record = bool(ctx.settings.typed()["monitor_enabled"])
    count = ctx.monitor.schedule_check_all(allow_record=allow_record)
    if not ctx.monitor.loop_running:
        message = "Monitor is stopped. Press Resume monitor to start checking."
    elif count == 0:
        message = "No enabled channels to check."
    else:
        message = f"Checking {count} channel(s) now."
    return {"ok": True, "queued": count, "message": message}


# ----------------------------------------------------------------------- tools
@router.get("/tools/versions")
async def tool_versions(request: Request):
    ctx = _ctx(request)
    force = request.query_params.get("refresh") in {"1", "true", "yes"}
    payload = await asyncio.to_thread(ctx.tools.all_versions, force)
    return payload


@router.post("/tools/test")
async def test_tool(request: Request):
    ctx = _ctx(request)
    data = await _json(request)
    tool = str(data.get("tool") or "")
    path = data.get("path")
    result = await asyncio.to_thread(
        ctx.tools.test_tool, tool, None if path is None else str(path)
    )
    return {"ok": bool(result.get("ok")), "result": result}


@router.get("/tools/impersonate-targets")
async def impersonate_targets(request: Request):
    ctx = _ctx(request)
    result = await asyncio.to_thread(ctx.tools.impersonate_targets)
    return result


# -------------------------------------------------------------------- settings
@router.get("/settings")
async def get_settings(request: Request):
    ctx = _ctx(request)
    return {
        "ok": True,
        "settings": ctx.settings.typed(),
        "raw": ctx.settings.raw(),
    }


@router.post("/settings")
async def save_settings(request: Request):
    ctx = _ctx(request)
    data = await _json(request)
    values = data.get("values") if isinstance(data.get("values"), dict) else data
    try:
        saved = ctx.settings.update(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    ctx.tools.clear_cache()
    ctx.monitor.wake()
    ctx.db.add_event(
        "info",
        "settings",
        f"Settings updated ({len(saved)} value(s)): {', '.join(sorted(saved))}",
    )
    return {"ok": True, "saved": sorted(saved), "settings": ctx.settings.typed()}


# ------------------------------------------------------------------ recordings
@router.get("/recordings/{recording_id}/log")
async def recording_log(request: Request, recording_id: int):
    ctx = _ctx(request)
    recording = ctx.db.get_recording(recording_id)
    if recording is None:
        raise HTTPException(404, "Recording not found.")
    log_path = recording.get("log_path")
    if not log_path:
        raise HTTPException(404, "This recording has no log file.")
    path = Path(log_path).resolve()
    log_dir = ctx.config.recording_log_dir.resolve()
    if not path.is_relative_to(log_dir) or not path.is_file():
        raise HTTPException(404, "Recording log file not found.")
    return {
        "ok": True,
        "log": tail_text(path, 250_000),
        "recording": recording,
    }


@router.post("/recordings/{recording_id}/stop")
async def stop_recording(request: Request, recording_id: int):
    ctx = _ctx(request)
    stopped = await ctx.monitor.stop_recording(recording_id)
    if not stopped:
        raise HTTPException(404, "Recording is not active.")
    return {"ok": True, "message": "Recording stopped."}


@router.delete("/recordings/{recording_id}")
async def delete_recording(request: Request, recording_id: int):
    ctx = _ctx(request)
    recording = ctx.db.get_recording(recording_id)
    if recording is None:
        raise HTTPException(404, "Recording not found.")
    if recording["status"] == "recording":
        await ctx.monitor.stop_recording(recording_id)

    delete_file = request.query_params.get("delete_file") in {"1", "true", "yes"}
    file_deleted = False
    file_error: str | None = None
    if delete_file and recording.get("filepath"):
        file_path = Path(recording["filepath"]).resolve()
        recordings_dir = Path(ctx.config.recordings_dir).resolve()
        if not file_path.is_relative_to(recordings_dir):
            raise HTTPException(400, "Refusing to delete a file outside the recordings directory.")
        try:
            if file_path.exists():
                file_path.unlink()
                file_deleted = True
        except OSError as exc:
            file_error = str(exc)

    # Keep the logs directory from growing forever when entries are removed.
    log_path = recording.get("log_path")
    if log_path:
        candidate = Path(log_path).resolve()
        log_dir = ctx.config.recording_log_dir.resolve()
        if candidate.is_relative_to(log_dir):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    ctx.db.delete_recording(recording_id)
    ctx.db.add_event(
        "info",
        "recording",
        f"Recording entry deleted: {recording.get('filename') or recording_id}"
        + (" (file deleted)" if file_deleted else ""),
        recording.get("channel_id"),
    )
    return {
        "ok": True,
        "file_deleted": file_deleted,
        "file_error": file_error,
        "message": "Recording deleted." if not file_error else f"Entry deleted, file error: {file_error}",
    }


# ---------------------------------------------------------------------- events
@router.get("/events")
async def list_events(request: Request):
    ctx = _ctx(request)
    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    level = request.query_params.get("level") or None
    if level not in {None, "info", "warning", "error"}:
        level = None
    return {"ok": True, "events": ctx.db.list_events(limit=limit, level=level)}
