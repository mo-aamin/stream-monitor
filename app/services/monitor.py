"""Background live-stream monitor and recording supervisor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

import httpx

from ..config import AppConfig
from ..database import Database
from ..utils import human_size, mask_credentials, utcnow, utcnow_iso
from .platforms import (
    extract_rumble_embed_url,
    NormalizedChannel,
    is_direct_rumble_video,
    platform_label,
    rumble_alternate_channel,
    rumble_live_page_url,
)
from .settings import SettingsService
from .tools import ToolService
from .ytdlp import (
    build_check_args,
    build_output_template,
    build_record_args,
    classify_failure,
    extract_output_path,
    filename_prefix,
    parse_metadata,
    short_error,
)

if TYPE_CHECKING:
    from asyncio.subprocess import Process

logger = logging.getLogger("streammonitor.monitor")

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_MEDIA_SUFFIXES = (
    ".mp4", ".mkv", ".webm", ".flv", ".ts", ".m4a", ".mp3", ".opus", ".ogg", ".wav", ".part"
)
# Rumble answers 404 or 410 for a /c/ or /user/ name that does not exist.
_RUMBLE_NOT_FOUND = frozenset({404, 410})
# Fragment / per-format files created while a download is in progress.
_INTERMEDIATE_RE = re.compile(r"\.f\d+\.|-Frag\d+\.")


def _is_complete_output(filepath: str | None, size: int | None) -> bool:
    """A finished media file: exists, non-empty, not a partial or per-format file."""
    if not filepath or not size or size <= 0:
        return False
    name = os.path.basename(filepath)
    if name.endswith((".part", ".ytdl")) or _INTERMEDIATE_RE.search(name):
        return False
    return os.path.isfile(filepath)


def find_output_by_prefix(directory: str, prefix: str) -> str | None:
    """Find the newest finalized (or partial) file for one recording.

    Every recording gets a unique prefix (timestamp + platform + channel), so
    this is safe even when several channels record at once.  yt-dlp's stdout is
    block-buffered when piped, which is why we cannot rely on parsing
    ``[download] Destination:`` in real time.
    """
    base = Path(directory)
    if not base.is_dir():
        return None
    finals: list[Path] = []
    partials: list[Path] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        name = entry.name
        if not name.startswith(prefix):
            continue
        if name.endswith(".ytdl") or _INTERMEDIATE_RE.search(name):
            continue
        if not name.lower().endswith(_MEDIA_SUFFIXES):
            continue
        if name.endswith(".part"):
            partials.append(entry)
        else:
            finals.append(entry)
    pool = finals or partials
    if not pool:
        return None
    try:
        return str(max(pool, key=lambda path: path.stat().st_mtime))
    except OSError:
        return str(pool[0])


@dataclass
class CheckResult:
    status: str  # live | offline | error
    live: bool = False
    title: str | None = None
    channel_name: str | None = None
    video_id: str | None = None
    url: str | None = None
    message: str | None = None
    live_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "live": self.live,
            "title": self.title,
            "channel_name": self.channel_name,
            "video_id": self.video_id,
            "url": self.url,
            "message": self.message,
        }


@dataclass
class ActiveRecording:
    recording_id: int
    channel_id: int
    platform: str
    channel_name: str
    title: str
    url: str
    log_path: Path
    started_at: str
    started_epoch: float
    started_monotonic: float
    output_dir: str
    file_prefix: str
    process: "Process | None" = None
    task: asyncio.Task | None = None
    log_handle: Any = None
    filepath: str | None = None
    filename: str | None = None
    stop_requested: bool = False
    finalized: bool = False
    output_tail: deque[str] = field(default_factory=lambda: deque(maxlen=200))


class Monitor:
    """Polls channels and supervises one yt-dlp process per recording."""

    def __init__(
        self,
        db: Database,
        settings: SettingsService,
        config: AppConfig,
        tools: ToolService,
    ):
        self._db = db
        self._settings = settings
        self._config = config
        self._tools = tools

        self._task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        # Created in start() so constructing a Monitor never binds to an event
        # loop (important for tests and for Python 3.9/3.10 compatibility).
        self._stop_event: asyncio.Event | None = None
        self._wake_event: asyncio.Event | None = None
        self._check_gate: asyncio.Semaphore | None = None
        self._shutting_down = False

        self._active: dict[int, ActiveRecording] = {}
        self._active_by_channel: dict[int, int] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._fail_cooldown: dict[int, float] = {}

        self._last_run_at: str | None = None
        self._next_run_at: str | None = None
        self._last_result: dict[str, Any] | None = None

    # ------------------------------------------------------------------ state
    @property
    def loop_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def state(self) -> str:
        if self._shutting_down:
            return "stopped"
        if not self.loop_running:
            return "stopped"
        if not self._settings.typed()["monitor_enabled"]:
            return "paused"
        return "running"

    def is_recording(self, channel_id: int) -> bool:
        return channel_id in self._active_by_channel

    def active_for_channel(self, channel_id: int) -> dict[str, Any] | None:
        recording_id = self._active_by_channel.get(channel_id)
        if recording_id is None:
            return None
        active = self._active.get(recording_id)
        return self._serialize_active(active) if active else None

    def active_recordings(self) -> list[dict[str, Any]]:
        return [
            self._serialize_active(active)
            for active in sorted(self._active.values(), key=lambda item: item.started_epoch)
        ]

    def _serialize_active(self, active: ActiveRecording) -> dict[str, Any]:
        resolved = find_output_by_prefix(active.output_dir, active.file_prefix)
        if resolved is None and active.filepath and os.path.exists(active.filepath):
            resolved = active.filepath
        if resolved and resolved != active.filepath:
            active.filepath = resolved
            active.filename = os.path.basename(resolved)
            try:
                self._db.update_recording(
                    active.recording_id, filepath=resolved, filename=active.filename
                )
            except Exception:  # noqa: BLE001 - status display must not fail
                logger.debug("Could not persist active recording path", exc_info=True)
        size = None
        if resolved:
            try:
                size = os.path.getsize(resolved)
            except OSError:
                size = None
        display_name = active.filename
        if display_name and display_name.endswith(".part"):
            display_name = display_name[:-5]
        return {
            "recording_id": active.recording_id,
            "channel_id": active.channel_id,
            "platform": active.platform,
            "channel_name": active.channel_name,
            "title": active.title,
            "url": active.url,
            "filename": display_name,
            "filepath": resolved,
            "started_at": active.started_at,
            "elapsed_seconds": int(time.monotonic() - active.started_monotonic),
            "size_bytes": size,
            "size_human": human_size(size),
            "stop_requested": active.stop_requested,
        }

    def status(self) -> dict[str, Any]:
        cfg = self._settings.typed()
        return {
            "state": self.state,
            "enabled": bool(cfg["monitor_enabled"]),
            "loop_running": self.loop_running,
            "active_count": self.active_count,
            "active": self.active_recordings(),
            "last_run_at": self._last_run_at,
            "next_check_at": self._next_run_at,
            "check_interval": cfg["check_interval"],
            "last_result": self._last_result,
        }

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.loop_running:
            return
        self._shutting_down = False
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._check_gate = asyncio.Semaphore(4)
        self._task = asyncio.create_task(self._loop(), name="streammonitor-loop")
        logger.info("Monitor loop started")

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._wake_event is not None:
            self._wake_event.set()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        for task in list(self._bg_tasks):
            if not task.done() and not task.get_name().startswith("recording-"):
                task.cancel()
        await self._stop_all_recordings(reason="Application shutting down")
        logger.info("Monitor stopped")

    def wake(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()

    async def pause(self) -> None:
        self._settings.update({"monitor_enabled": False})
        self._db.add_event("info", "monitor", "Monitoring paused")
        self._next_run_at = None
        self.wake()

    async def resume(self) -> None:
        if not self.loop_running:
            self.start()
        self._settings.update({"monitor_enabled": True})
        self._db.add_event("info", "monitor", "Monitoring resumed")
        self.wake()

    # ------------------------------------------------------------------- loop
    async def _loop(self) -> None:
        stop_event = self._stop_event
        assert stop_event is not None
        while not stop_event.is_set():
            try:
                cfg = self._settings.typed()
                if not cfg["monitor_enabled"]:
                    self._next_run_at = None
                    await self._wait(2.0)
                    continue

                self._last_run_at = utcnow_iso()
                channels = self._db.list_channels(enabled_only=True)
                to_check = [
                    channel
                    for channel in channels
                    if channel["id"] not in self._active_by_channel
                ]
                if to_check:
                    await asyncio.gather(
                        *(self._check_with_gate(channel) for channel in to_check),
                        return_exceptions=True,
                    )
                self._last_result = {
                    "checked_at": self._last_run_at,
                    "channels": len(to_check),
                }
                interval = max(15, int(cfg["check_interval"]))
                self._next_run_at = (utcnow() + timedelta(seconds=interval)).isoformat(
                    timespec="seconds"
                )
                await self._wait(interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - loop must survive unexpected errors
                logger.exception("Unexpected monitor loop error")
                await self._wait(15.0)
        self._next_run_at = None

    async def _wait(self, timeout: float) -> None:
        wake_event = self._wake_event
        if wake_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            wake_event.clear()

    # ------------------------------------------------------------------ checks
    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    async def _check_with_gate(
        self, channel: dict[str, Any], *, allow_record: bool = True
    ) -> CheckResult | None:
        """Run a check while respecting the shared, process-wide concurrency cap."""
        gate = self._check_gate
        if gate is None:
            return await self.check_channel(channel, allow_record=allow_record)
        async with gate:
            return await self.check_channel(channel, allow_record=allow_record)

    async def check_channel(
        self, channel: dict[str, Any], *, allow_record: bool = True
    ) -> CheckResult | None:
        """Check one channel and persist the result. Serialized per channel."""
        if self._shutting_down:
            return None
        async with self._lock_for(int(channel["id"])):
            result = await self._perform_check(channel)
            self._apply_result(channel, result)
            if result.live and allow_record and not self._shutting_down:
                await self._maybe_record(channel, result)
            return result

    async def check_channel_now(self, channel_id: int) -> dict[str, Any] | None:
        channel = self._db.get_channel(channel_id)
        if channel is None:
            return None
        if channel_id in self._active_by_channel:
            active = self.active_for_channel(channel_id)
            return {
                "status": "recording",
                "live": True,
                "message": "This channel is already being recorded.",
                "active": active,
            }
        result = await self._check_with_gate(channel, allow_record=True)
        return result.as_dict() if result else None

    def schedule_check_all(self, *, allow_record: bool = True) -> int:
        if not self.loop_running:
            return 0
        channels = self._db.list_channels(enabled_only=True)
        self._background(self._run_check_all(channels, allow_record=allow_record))
        return len(channels)

    def schedule_channel_check(self, channel_id: int) -> None:
        """Queue a check for a newly added or re-enabled channel."""
        if not self.loop_running:
            return

        async def runner() -> None:
            channel = self._db.get_channel(channel_id)
            if channel is not None and channel.get("enabled"):
                await self._check_with_gate(channel, allow_record=True)

        self._background(runner())

    async def _run_check_all(
        self, channels: list[dict[str, Any]], *, allow_record: bool
    ) -> None:
        await asyncio.gather(
            *(
                self._check_with_gate(channel, allow_record=allow_record)
                for channel in channels
            ),
            return_exceptions=True,
        )
        self.wake()

    def _background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _perform_check(self, channel: dict[str, Any]) -> CheckResult:
        cfg = self._settings.typed()
        platform = channel["platform"]
        check_url = channel["url"]

        if platform == "rumble" and not is_direct_rumble_video(check_url):
            page_url = rumble_live_page_url(check_url)
            if not page_url:
                return CheckResult(status="offline", message="No usable Rumble URL.")
            html, error, status = await self._fetch_rumble_page(page_url, cfg)
            if error and status in _RUMBLE_NOT_FOUND:
                alternate = rumble_alternate_channel(channel["identifier"])
                alt_page = rumble_live_page_url(alternate.url) if alternate else None
                if alternate and alt_page:
                    alt_html, alt_error, _ = await self._fetch_rumble_page(alt_page, cfg)
                    if not alt_error:
                        self._switch_rumble_channel(channel, alternate)
                        html, error = alt_html, None
            if error:
                return CheckResult(status="error", message=error)
            embed_url = extract_rumble_embed_url(html or "")
            if not embed_url:
                return CheckResult(
                    status="offline",
                    message="No live stream found on the Rumble channel page.",
                )
            check_url = embed_url

        command = build_check_args(cfg, platform, check_url)
        logger.debug("Checking %s: %s", channel["identifier"], mask_credentials(" ".join(command)))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return CheckResult(
                status="error",
                message=f"yt-dlp was not found at {cfg['ytdlp_path']}. Check Settings.",
            )
        except OSError as exc:
            return CheckResult(status="error", message=f"Could not start yt-dlp: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=int(cfg["check_timeout"])
            )
        except asyncio.TimeoutError:
            # The check process is NOT started in its own session, so never use
            # a process-group signal here; kill only the child itself.
            if process.returncode is None:
                process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            return CheckResult(
                status="error",
                message=f"Live check timed out after {cfg['check_timeout']} seconds.",
            )

        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode == 0:
            try:
                metadata = parse_metadata(stdout.decode("utf-8", errors="replace"))
            except (ValueError, json.JSONDecodeError) as exc:
                return CheckResult(status="error", message=f"Could not parse yt-dlp output: {exc}")
            if not metadata["is_live"]:
                return CheckResult(
                    status="offline",
                    live=False,
                    message=f"Not live ({metadata.get('live_status') or 'offline'}).",
                )
            return CheckResult(
                status="live",
                live=True,
                title=metadata["title"],
                channel_name=metadata["channel_name"],
                video_id=metadata["video_id"],
                url=metadata["webpage_url"] or check_url,
                live_status=metadata.get("live_status"),
            )

        kind = classify_failure(stderr_text)
        detail = short_error(stderr_text)
        if kind == "offline":
            return CheckResult(status="offline", message=detail or "Not currently live.")
        if kind == "impersonate":
            return CheckResult(
                status="error",
                message=(
                    "Impersonation failed. Confirm curl-cffi is installed and the target "
                    f"'{cfg['impersonate_target']}' is available. {detail}"
                ).strip(),
            )
        if kind == "js_runtime":
            return CheckResult(
                status="error",
                message=(
                    "yt-dlp could not use a JavaScript runtime. Check the Deno path in "
                    f"Settings. {detail}"
                ).strip(),
            )
        if kind == "signin":
            return CheckResult(
                status="error",
                message=(
                    "The platform requested a sign-in or bot check. Add a cookies file in "
                    f"Settings. {detail}"
                ).strip(),
            )
        return CheckResult(
            status="error",
            message=detail or f"yt-dlp exited with code {process.returncode}.",
        )

    def _switch_rumble_channel(
        self, channel: dict[str, Any], alternate: NormalizedChannel
    ) -> None:
        """Persist the working /c/ or /user/ form of a Rumble channel."""
        old_identifier = channel["identifier"]
        try:
            self._db.update_channel(
                int(channel["id"]), identifier=alternate.identifier, url=alternate.url
            )
        except Exception:  # noqa: BLE001 - e.g. the other form is already a separate row
            logger.warning(
                "Rumble %s not found; %s works but could not be saved",
                old_identifier,
                alternate.identifier,
                exc_info=True,
            )
            return
        channel["identifier"] = alternate.identifier
        channel["url"] = alternate.url
        self._db.add_event(
            "info",
            "channel",
            f"Rumble: {old_identifier} does not exist; switched to {alternate.identifier}",
            int(channel["id"]),
        )
        logger.info("Rumble channel %s switched to %s", old_identifier, alternate.identifier)

    @staticmethod
    def _rumble_error(status: int, html: str) -> str | None:
        if status in _RUMBLE_NOT_FOUND:
            return f"Rumble channel not found (HTTP {status}). Check the channel name."
        if status >= 400:
            return f"Rumble returned HTTP {status}."
        if not html or "<title>404" in html.lower():
            return "Rumble channel page not found."
        return None

    @staticmethod
    def _fetch_rumble_with_curl(url: str, cfg: dict[str, Any]) -> tuple[int, str]:
        """Rumble rejects plain TLS fingerprints; use curl-cffi impersonation."""
        from curl_cffi import requests as curl_requests

        target = (cfg.get("impersonate_target") or "chrome").split(":", 1)[0] or "chrome"
        kwargs: dict[str, Any] = {
            "impersonate": target,
            "timeout": 30,
            "allow_redirects": True,
        }
        proxy = (cfg.get("proxy") or "").strip()
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        response = curl_requests.get(url, **kwargs)
        return response.status_code, response.text

    async def _fetch_rumble_page(
        self, url: str, cfg: dict[str, Any]
    ) -> tuple[str | None, str | None, int | None]:
        """Return ``(html, error, http_status)`` for a Rumble page."""
        browser_error: str | None = None
        try:
            status, html = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_rumble_with_curl, url, cfg),
                timeout=45,
            )
            error = self._rumble_error(status, html)
            if error is None:
                return html, None, status
            if status in _RUMBLE_NOT_FOUND:
                # A definite "does not exist"; the plain-HTTP fallback would only
                # be blocked (403) and hide the real reason.
                return None, error, status
            browser_error = error
        except Exception as exc:  # noqa: BLE001 - fall back to httpx below
            browser_error = f"browser fetch failed: {exc}"
            logger.debug("Rumble curl_cffi fetch failed: %s", exc)

        headers = {
            "User-Agent": _CHROME_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        kwargs: dict[str, Any] = {}
        if cfg.get("proxy"):
            kwargs["proxy"] = cfg["proxy"]
        try:
            async with httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                timeout=httpx.Timeout(30.0, connect=15.0),
                **kwargs,
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            return None, f"{browser_error}; fallback fetch failed: {exc}", None
        error = self._rumble_error(response.status_code, response.text)
        if error:
            message = f"{error} ({browser_error})" if browser_error else error
            return None, message, response.status_code
        return response.text, None, response.status_code

    def _apply_result(self, channel: dict[str, Any], result: CheckResult) -> None:
        channel_id = int(channel["id"])
        previous = channel.get("last_status") or "unknown"
        channel_name = result.channel_name or channel.get("name") or channel["identifier"]
        label = platform_label(channel["platform"])

        if result.live:
            self._db.set_channel_status(
                channel_id, "live", title=result.title, live=True
            )
            self._fail_cooldown.pop(channel_id, None)
            if previous != "live":
                self._db.add_event(
                    "info",
                    "channel",
                    f"{label}: {channel_name} is live — {result.title}",
                    channel_id,
                )
        elif result.status == "offline":
            self._db.set_channel_status(
                channel_id, "offline", error=result.message
            )
            if previous == "live":
                self._db.add_event(
                    "info", "channel", f"{label}: {channel_name} went offline", channel_id
                )
        else:
            self._db.set_channel_status(channel_id, "error", error=result.message)
            if previous != "error":
                self._db.add_event(
                    "warning",
                    "channel",
                    f"{label}: {channel_name} check failed — {result.message}",
                    channel_id,
                )

    # --------------------------------------------------------------- recording
    async def _maybe_record(self, channel: dict[str, Any], result: CheckResult) -> None:
        cfg = self._settings.typed()
        if not cfg["monitor_enabled"] or not self.loop_running or self._shutting_down:
            return
        channel_id = int(channel["id"])
        if channel_id in self._active_by_channel:
            return
        cooldown_until = self._fail_cooldown.get(channel_id, 0)
        if time.monotonic() < cooldown_until:
            return
        if self._db.active_recording_for_channel(channel_id):
            return
        await self._start_recording(channel, result)

    async def _start_recording(self, channel: dict[str, Any], result: CheckResult) -> None:
        cfg = self._settings.typed()
        platform = channel["platform"]
        channel_id = int(channel["id"])
        channel_name = (
            result.channel_name or channel.get("name") or channel.get("identifier") or "Unknown"
        )
        title = result.title or "Untitled stream"
        started_local = datetime.now()
        started_at = utcnow_iso()

        if platform == "youtube" and result.video_id:
            record_url = f"https://www.youtube.com/watch?v={result.video_id}"
        else:
            record_url = result.url or channel["url"]

        output_dir = str(self._config.recordings_dir)
        file_prefix = filename_prefix(platform, channel_name, started_local)
        output_template = build_output_template(
            output_dir,
            cfg["filename_template"],
            platform,
            channel_name,
            started_local,
        )
        command = build_record_args(cfg, platform, record_url, output_template)

        try:
            versions = await asyncio.to_thread(self._tools.all_versions)
            ytdlp_version = next(
                (
                    tool.get("version")
                    for tool in versions.get("tools", [])
                    if tool.get("key") == "ytdlp"
                ),
                None,
            )
        except Exception:  # noqa: BLE001 - version is informational only
            ytdlp_version = None

        recording = self._db.create_recording(
            channel_id=channel_id,
            platform=platform,
            channel_name=channel_name,
            title=title,
            video_id=result.video_id,
            url=record_url,
            log_path="",
            ytdlp_version=ytdlp_version,
        )
        recording_id = int(recording["id"])
        log_path = self._config.recording_log_dir / f"{recording_id}.log"
        self._db.update_recording(recording_id, log_path=str(log_path))

        log_handle = None
        try:
            log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
            log_handle.write(
                f"# Recording {recording_id} — {channel_name} — {title}\n"
                f"# Started: {started_at}\n"
                f"# Command: {mask_credentials(' '.join(command))}\n\n"
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            message = f"yt-dlp was not found at {cfg['ytdlp_path']}. Check Settings."
            self._fail(recording_id, channel_id, message, log_handle)
            return
        except OSError as exc:
            self._fail(recording_id, channel_id, f"Could not start yt-dlp: {exc}", log_handle)
            return

        active = ActiveRecording(
            recording_id=recording_id,
            channel_id=channel_id,
            platform=platform,
            channel_name=channel_name,
            title=title,
            url=record_url,
            log_path=log_path,
            started_at=started_at,
            started_epoch=time.time(),
            started_monotonic=time.monotonic(),
            output_dir=output_dir,
            file_prefix=file_prefix,
            process=process,
            log_handle=log_handle,
        )
        self._active[recording_id] = active
        self._active_by_channel[channel_id] = recording_id
        active.task = asyncio.create_task(self._consume(active), name=f"recording-{recording_id}")
        self._active_tasks_add(active.task)
        self._db.add_event(
            "info",
            "recording",
            f"{platform_label(platform)}: recording started for {channel_name} — {title}",
            channel_id,
        )
        logger.info("[%s] Recording started: %s — %s", recording_id, channel_name, title)

    def _fail(
        self,
        recording_id: int,
        channel_id: int,
        message: str,
        log_handle,
    ) -> None:
        if log_handle:
            try:
                log_handle.write(f"ERROR: {message}\n")
                log_handle.close()
            except OSError:
                pass
        self._db.finish_recording(
            recording_id, status="failed", exit_code=None, error=message
        )
        self._db.set_channel_status(channel_id, "error", error=message)
        self._db.add_event("error", "recording", message, channel_id)
        logger.error("Recording %s failed to start: %s", recording_id, message)
        self._fail_cooldown[channel_id] = time.monotonic() + int(
            self._settings.typed()["retry_cooldown"]
        )

    def _active_tasks_add(self, task: asyncio.Task) -> None:
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _consume(self, active: ActiveRecording) -> None:
        exit_code: int | None = None
        error: str | None = None
        try:
            exit_code = await self._read_output(active)
        except asyncio.CancelledError:
            active.stop_requested = True
            raise
        except Exception as exc:  # noqa: BLE001 - always finalize
            logger.exception("Recording reader crashed")
            error = str(exc)
        finally:
            if not active.finalized:
                await self._finalize(active, exit_code, error)

    async def _read_output(self, active: ActiveRecording) -> int:
        process = active.process
        assert process is not None and process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self._handle_output_line(active, line)
        return await process.wait()

    def _handle_output_line(self, active: ActiveRecording, line: str) -> None:
        active.output_tail.append(line)
        if active.log_handle:
            try:
                active.log_handle.write(line + "\n")
            except OSError:
                pass
        candidate = extract_output_path(line)
        if candidate and candidate != active.filepath:
            active.filepath = candidate
            active.filename = os.path.basename(candidate)
            try:
                self._db.update_recording(
                    active.recording_id,
                    filepath=candidate,
                    filename=active.filename,
                )
            except Exception:  # noqa: BLE001 - DB hiccups should not kill a recording
                logger.exception("Could not update recording path")

    async def _finalize(
        self, active: ActiveRecording, exit_code: int | None, read_error: str | None
    ) -> None:
        if active.finalized:
            return
        active.finalized = True
        cfg = self._settings.typed()

        filepath = find_output_by_prefix(active.output_dir, active.file_prefix)
        if filepath is None and active.filepath and os.path.exists(active.filepath):
            filepath = active.filepath
        if filepath and filepath.endswith(".part"):
            final_path = filepath[:-5]
            if os.path.exists(final_path):
                filepath = final_path
        filename = os.path.basename(filepath) if filepath else active.filename
        size = None
        if filepath:
            try:
                size = os.path.getsize(filepath)
            except OSError:
                size = None

        if self._shutting_down:
            status = "interrupted"
        elif active.stop_requested:
            status = "stopped"
        elif exit_code == 0 and not read_error:
            status = "completed"
        elif not read_error and _is_complete_output(filepath, size):
            # yt-dlp often exits non-zero at the end of a live stream (for
            # example "Did not get any data blocks") after it has already
            # merged a complete file. Treat that as a finished recording.
            status = "completed"
            warning = short_error("\n".join(active.output_tail))
            if active.log_handle:
                try:
                    active.log_handle.write(
                        f"\n# Note: yt-dlp exited with code {exit_code} but produced a "
                        f"complete file{': ' + warning if warning else ''}\n"
                    )
                except OSError:
                    pass
        else:
            status = "failed"

        error = read_error
        if status == "interrupted":
            error = "Application stopped while this recording was active."
        elif status == "failed" and not error:
            error = short_error("\n".join(active.output_tail)) or (
                f"yt-dlp exited with code {exit_code}"
            )
        if status == "completed":
            error = None

        self._db.finish_recording(
            active.recording_id,
            status=status,
            exit_code=exit_code,
            error=error,
            filename=filename,
            filepath=filepath,
            size_bytes=size,
        )

        if status in {"completed", "stopped"}:
            self._db.set_channel_status(active.channel_id, "offline")
            level = "info"
            message = (
                f"{platform_label(active.platform)}: recording stopped for "
                f"{active.channel_name} ({human_size(size)})"
                if status == "stopped"
                else f"{platform_label(active.platform)}: recording finished for "
                f"{active.channel_name} ({human_size(size)})"
            )
        else:
            self._db.set_channel_status(active.channel_id, "error", error=error)
            level = "error" if status == "failed" else "warning"
            message = (
                f"{platform_label(active.platform)}: recording {status} for "
                f"{active.channel_name} — {error}"
            )
            if status == "failed":
                self._fail_cooldown[active.channel_id] = time.monotonic() + int(
                    cfg["retry_cooldown"]
                )
        self._db.add_event(level, "recording", message, active.channel_id)
        logger.info(
            "Recording %s %s (exit=%s, size=%s)",
            active.recording_id,
            status,
            exit_code,
            human_size(size),
        )

        if active.log_handle:
            try:
                active.log_handle.write(f"\n# Finished: {status} (exit={exit_code})\n")
                active.log_handle.close()
            except OSError:
                pass
        self._active.pop(active.recording_id, None)
        if self._active_by_channel.get(active.channel_id) == active.recording_id:
            self._active_by_channel.pop(active.channel_id, None)

    # -------------------------------------------------------------- stop paths
    @staticmethod
    def _signal(process: "Process", sig: int) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _kill(self, process: "Process") -> None:
        self._signal(process, signal.SIGKILL)

    async def stop_recording(self, recording_id: int, timeout: float = 45.0) -> bool:
        active = self._active.get(recording_id)
        if active is None:
            return False
        active.stop_requested = True
        if active.process is not None:
            self._signal(active.process, signal.SIGINT)
        task = active.task
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                if active.process is not None:
                    self._kill(active.process)
                try:
                    await asyncio.wait_for(active.task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    if active.task:
                        active.task.cancel()
                    await self._finalize(active, None, "Recording did not stop cleanly")
        return True

    async def stop_channel_recording(self, channel_id: int, timeout: float = 45.0) -> bool:
        recording_id = self._active_by_channel.get(channel_id)
        if recording_id is None:
            return False
        return await self.stop_recording(recording_id, timeout=timeout)

    async def _stop_all_recordings(self, reason: str) -> None:
        active_list = list(self._active.values())
        if not active_list:
            return
        logger.info("Stopping %s active recording(s): %s", len(active_list), reason)
        for active in active_list:
            active.stop_requested = True
            if active.process is not None:
                self._signal(active.process, signal.SIGINT)
        tasks = [active.task for active in active_list if active.task is not None]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=60
                )
            except asyncio.TimeoutError:
                logger.warning("Recordings did not stop in time; killing them")
                for active in active_list:
                    if active.process is not None:
                        self._kill(active.process)
                for active in active_list:
                    if not active.finalized:
                        await self._finalize(
                            active,
                            active.process.returncode if active.process else None,
                            "Recording did not stop cleanly",
                        )
                for active in active_list:
                    if active.task and not active.task.done():
                        active.task.cancel()

    def forget_channel(self, channel_id: int) -> None:
        self._channel_locks.pop(channel_id, None)
        self._fail_cooldown.pop(channel_id, None)
