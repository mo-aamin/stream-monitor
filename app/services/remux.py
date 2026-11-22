"""Make sure a finished recording is a real, playable container.

Live HLS recordings are written by ffmpeg as MPEG-TS inside a ``.mp4`` file.
For a single combined format (Twitch, Kick, Rumble) yt-dlp remuxes it to MP4
afterwards when the download ends cleanly.  YouTube without
``--live-from-start`` downloads separate video+audio HLS formats that ffmpeg
merges live, and yt-dlp never remuxes that result (verified with yt-dlp
2026.10.26), leaving TS data that QuickTime/Safari refuse to open.  Killed or
interrupted recordings of any platform can end up the same way.

YouTube with ``--live-from-start`` downloads video and audio as separate
formats, each from the start of the stream at its own speed. When such a
recording is stopped early, one track can be far longer than the other; both
start at the same point, so the file is cut to the length of the shorter one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger("streammonitor.remux")

PROBE_TIMEOUT = 60.0
REMUX_TIMEOUT = 1800.0
# Video and audio lengths may differ by this much (seconds) before trimming.
TRIM_TOLERANCE = 2.0


def _resolve(configured: str | None, name: str) -> str | None:
    path = (configured or "").strip()
    if path:
        expanded = Path(path).expanduser()
        if expanded.is_dir():
            expanded = expanded / name
        return str(expanded) if expanded.exists() else None
    return shutil.which(name)


async def _run(args: list[str], timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return -1, "timed out"
    return process.returncode or 0, out.decode("utf-8", errors="replace")


async def probe_format(ffprobe: str, path: str) -> str | None:
    """Return ffprobe's ``format_name`` (e.g. ``mpegts`` or ``mov,mp4,...``)."""
    code, out = await _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=format_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        PROBE_TIMEOUT,
    )
    if code != 0:
        return None
    text = out.strip().splitlines()
    return text[0].strip() if text else None


async def probe_stream_durations(
    ffprobe: str, path: str
) -> tuple[float | None, float | None]:
    """Return ``(video_seconds, audio_seconds)`` of the first video/audio stream."""
    code, out = await _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "stream=codec_type,duration",
            "-of", "csv=p=0",
            path,
        ],
        PROBE_TIMEOUT,
    )
    video: float | None = None
    audio: float | None = None
    if code != 0:
        return None, None
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        kind, raw = parts[0], parts[1]
        try:
            seconds = float(raw)
        except ValueError:
            continue
        if seconds <= 0:
            continue
        if kind == "video" and video is None:
            video = seconds
        elif kind == "audio" and audio is None:
            audio = seconds
    return video, audio


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


async def ensure_playable(filepath: str, cfg: dict[str, Any]) -> tuple[str, str | None]:
    """Remux TS-in-MP4 / leftover ``.part`` files into a proper MP4, and cut
    files whose video and audio tracks differ in length to the shorter one.

    Returns ``(path, note)``: the (possibly new) path of the recording and a
    human-readable note for the recording log, or ``None`` when nothing was done.
    The original file is only removed after a successful remux.
    """
    if (cfg.get("merge_output_format") or "").strip() != "mp4":
        return filepath, None
    is_part = filepath.endswith(".part")
    target = filepath[:-5] if is_part else filepath
    if not target.lower().endswith(".mp4") or not os.path.isfile(filepath):
        return filepath, None

    ffprobe = _resolve(cfg.get("ffprobe_path"), "ffprobe")
    ffmpeg = _resolve(cfg.get("ffmpeg_path"), "ffmpeg")
    if not ffprobe or not ffmpeg:
        return filepath, None

    fmt = await probe_format(ffprobe, filepath)
    if fmt is None:
        return filepath, "Could not read the recording with ffprobe; left as is."
    is_mp4 = "mp4" in fmt.split(",") or fmt.startswith("mov")

    video_s, audio_s = await probe_stream_durations(ffprobe, filepath)
    trim_to: float | None = None
    if video_s and audio_s and abs(video_s - audio_s) > TRIM_TOLERANCE:
        trim_to = min(video_s, audio_s)

    if trim_to is None:
        if is_mp4 and not is_part:
            return filepath, None
        if is_mp4 and is_part and not os.path.exists(target):
            os.replace(filepath, target)
            return target, "Renamed finished .part file."

    tmp = target[:-4] + ".remux.tmp.mp4"
    code, out = await _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", filepath,
            "-map", "0:v?", "-map", "0:a?",
            "-c", "copy",
            *(["-t", f"{trim_to:.3f}"] if trim_to is not None else []),
            "-movflags", "+faststart",
            "-f", "mp4", tmp,
        ],
        REMUX_TIMEOUT,
    )
    if code != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        detail = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
        logger.warning("Remux of %s failed: %s", filepath, detail)
        action = (
            "Trimming to match video and audio"
            if is_mp4 and trim_to is not None
            else "Remux to MP4"
        )
        return filepath, f"{action} failed ({detail}); original kept."

    final = target
    if os.path.exists(final) and final != filepath:
        # Never clobber a different finished file.
        final = target[:-4] + ".remuxed.mp4"
    os.replace(tmp, final)
    if final != filepath:
        try:
            os.remove(filepath)
        except OSError:
            pass
    notes = []
    if not is_mp4:
        notes.append(f"Remuxed {fmt} recording to a playable MP4.")
    if trim_to is not None:
        notes.append(
            f"Trimmed to {_clock(trim_to)} so video and audio match "
            f"(video was {_clock(video_s)}, audio {_clock(audio_s)})."
        )
    if not notes:
        notes.append("Saved finished .part file as MP4.")
    logger.info("Finalized %s: %s", os.path.basename(final), " ".join(notes))
    return final, " ".join(notes)
