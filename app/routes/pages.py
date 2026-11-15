"""Server-rendered pages."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..context import channel_display_status, render
from ..utils import human_size, tail_text

router = APIRouter()


def _ctx(request: Request):
    return request.app.state.ctx


def _active_sizes(active_items: list[dict]) -> int:
    return sum(int(item.get("size_bytes") or 0) for item in active_items)


def _merge_active_recordings(
    recordings: list[dict], active_by_id: dict[int, dict]
) -> list[dict]:
    """Overlay live elapsed/size/filename data onto in-progress DB rows."""
    merged = []
    for recording in recordings:
        if recording.get("status") == "recording":
            live = active_by_id.get(int(recording["id"]))
            if live:
                recording = {
                    **recording,
                    "filename": live.get("filename") or recording.get("filename"),
                    "filepath": live.get("filepath") or recording.get("filepath"),
                    "size_bytes": live.get("size_bytes"),
                    "duration_seconds": live.get("elapsed_seconds"),
                }
        merged.append(recording)
    return merged


@router.get("/")
async def dashboard(request: Request):
    ctx = _ctx(request)
    active_items = ctx.monitor.active_recordings()
    active = {item["channel_id"]: item for item in active_items}
    active_by_id = {int(item["recording_id"]): item for item in active_items}
    channels = ctx.db.list_channels()
    live_channels = []
    for channel in channels:
        active_item = active.get(channel["id"])
        if active_item or (channel["enabled"] and channel["last_status"] == "live"):
            live_channels.append(
                {
                    "channel": channel,
                    "active": active_item,
                    "status": channel_display_status(channel, active_item),
                }
            )
    live_channels.sort(key=lambda item: (item["active"] is None, item["channel"]["platform"]))
    stats = ctx.db.dashboard_stats()
    stats["recordings_bytes"] += _active_sizes(active_items)
    stats["recordings_bytes_human"] = human_size(stats["recordings_bytes"])
    return render(
        ctx,
        request,
        "dashboard.html",
        page_title="Dashboard",
        stats=stats,
        live_channels=live_channels,
        recent_recordings=_merge_active_recordings(
            ctx.db.list_recordings(limit=8), active_by_id
        ),
        events=ctx.db.list_events(limit=7),
    )


@router.get("/channels")
async def channels(request: Request):
    ctx = _ctx(request)
    active = {item["channel_id"]: item for item in ctx.monitor.active_recordings()}
    rows = []
    for channel in ctx.db.list_channels():
        active_item = active.get(channel["id"])
        rows.append(
            {
                "channel": channel,
                "active": active_item,
                "status": channel_display_status(channel, active_item),
            }
        )
    counts = {key: 0 for key in ("youtube", "twitch", "kick", "rumble")}
    for row in rows:
        counts[row["channel"]["platform"]] = counts.get(row["channel"]["platform"], 0) + 1
    return render(
        ctx,
        request,
        "channels.html",
        page_title="Channels",
        rows=rows,
        counts=counts,
        channel_total=len(rows),
    )


@router.get("/recordings")
async def recordings(request: Request):
    ctx = _ctx(request)
    status = request.query_params.get("status") or None
    search = request.query_params.get("q") or None
    if status not in {None, "recording", "completed", "failed", "stopped", "interrupted"}:
        status = None
    active_items = ctx.monitor.active_recordings()
    active_by_id = {int(item["recording_id"]): item for item in active_items}
    items = _merge_active_recordings(
        ctx.db.list_recordings(limit=300, status=status, search=search),
        active_by_id,
    )
    stats = ctx.db.recording_stats()
    stats["total_bytes"] += _active_sizes(active_items)
    stats["total_bytes_human"] = human_size(stats["total_bytes"])
    return render(
        ctx,
        request,
        "recordings.html",
        page_title="Recordings",
        recordings=items,
        stats=stats,
        status_filter=status or "",
        search=search or "",
    )


@router.get("/settings")
async def settings_page(request: Request):
    ctx = _ctx(request)
    return render(ctx, request, "settings.html", page_title="Settings")


@router.get("/logs")
async def logs_page(request: Request):
    ctx = _ctx(request)
    return render(
        ctx,
        request,
        "logs.html",
        page_title="Activity & Logs",
        events=ctx.db.list_events(limit=200),
        app_log=tail_text(ctx.config.app_log_path, 180_000),
        recording_log_path=str(ctx.config.recording_log_dir),
    )
