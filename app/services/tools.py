"""Detect installed helper binaries and report their versions."""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import __version__ as APP_VERSION


class ToolError(Exception):
    pass


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _extract(pattern: str, text: str, fallback: str = "") -> str:
    match = re.search(pattern, text or "", re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else fallback


class ToolService:
    """Runs version checks against the configured binaries.

    Version calls are synchronous (subprocess.run) and intentionally cached;
    callers in async routes should wrap them with ``asyncio.to_thread``.
    """

    CACHE_TTL = 20.0

    def __init__(self, settings_service):
        self._settings = settings_service
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0

    # ------------------------------------------------------------------ helpers
    def clear_cache(self) -> None:
        with self._lock:
            self._cache = None
            self._cached_at = 0.0

    def _run(self, path: str, args: list[str], timeout: float = 20) -> tuple[int, str]:
        if not path:
            raise ToolError("No path configured.")
        expanded = str(Path(path).expanduser())
        if not os.path.exists(expanded):
            raise ToolError(f"Not found: {expanded}")
        if not os.path.isfile(expanded) or not os.access(expanded, os.X_OK):
            raise ToolError(f"Not an executable file: {expanded}")
        try:
            completed = subprocess.run(
                [expanded, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"Timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            detail = _first_line(output) or f"exit code {completed.returncode}"
            raise ToolError(detail[:300])
        return completed.returncode, output

    def _tool_result(self, key: str, label: str, path: str, args: list[str], pattern: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": key,
            "label": label,
            "path": path,
            "ok": False,
            "version": None,
            "error": None,
        }
        try:
            _, output = self._run(path, args)
            result["version"] = _extract(pattern, output, _first_line(output)) or "installed"
            result["ok"] = True
        except ToolError as exc:
            result["error"] = str(exc)
        return result

    # ------------------------------------------------------------------- tools
    def ytdlp(self, path: str) -> dict[str, Any]:
        return self._tool_result(
            "ytdlp", "yt-dlp", path, ["--version"], r"^(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)"
        )

    def deno(self, path: str) -> dict[str, Any]:
        return self._tool_result(
            "deno", "Deno", path, ["--version"], r"^deno\s+([0-9][^\s]*)"
        )

    def ffmpeg(self, path: str) -> dict[str, Any]:
        return self._tool_result(
            "ffmpeg", "FFmpeg", path, ["-version"], r"^ffmpeg version\s+(\S+)"
        )

    def ffprobe(self, path: str) -> dict[str, Any]:
        return self._tool_result(
            "ffprobe", "FFprobe", path, ["-version"], r"^ffprobe version\s+(\S+)"
        )

    @staticmethod
    def _package_version(*names: str) -> dict[str, Any]:
        for name in names:
            try:
                return {"ok": True, "version": importlib.metadata.version(name), "error": None}
            except importlib.metadata.PackageNotFoundError:
                continue
        return {"ok": False, "version": None, "error": "Python package not installed"}

    def curl_cffi(self) -> dict[str, Any]:
        found = self._package_version("curl_cffi", "curl-cffi")
        return {
            "key": "curl_cffi",
            "label": "curl-cffi",
            "path": "Python package",
            "ok": found["ok"],
            "version": found["version"],
            "error": found["error"] if not found["ok"] else None,
        }

    def ytdlp_ejs(self) -> dict[str, Any]:
        found = self._package_version("yt-dlp-ejs", "yt_dlp_ejs")
        return {
            "key": "ytdlp_ejs",
            "label": "yt-dlp EJS",
            "path": "Python package",
            "ok": found["ok"],
            "version": found["version"],
            "error": found["error"] if not found["ok"] else None,
        }

    def python(self) -> dict[str, Any]:
        import platform

        return {
            "key": "python",
            "label": "Python",
            "path": shutil.which("python3") or "python3",
            "ok": True,
            "version": platform.python_version(),
            "error": None,
        }

    # ------------------------------------------------------------------ public
    def all_versions(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._cache is not None
                and now - self._cached_at < self.CACHE_TTL
            ):
                return self._cache
            cfg = self._settings.typed()
            tools = [
                self.ytdlp(cfg["ytdlp_path"]),
                self.deno(cfg["deno_path"]),
                self.ffmpeg(cfg["ffmpeg_path"]),
                self.ffprobe(cfg["ffprobe_path"]),
                self.curl_cffi(),
                self.ytdlp_ejs(),
                self.python(),
            ]
            cache = {
                "ok": True,
                "app_version": APP_VERSION,
                "tools": tools,
                "cached_at": time.time(),
            }
            self._cache = cache
            self._cached_at = now
            return cache

    def test_tool(self, tool: str, path: str | None = None) -> dict[str, Any]:
        cfg = self._settings.typed()
        path = (path if path is not None else "").strip()
        if tool == "ytdlp":
            result = self.ytdlp(path or cfg["ytdlp_path"])
        elif tool == "deno":
            result = self.deno(path or cfg["deno_path"])
        elif tool == "ffmpeg":
            result = self.ffmpeg(path or cfg["ffmpeg_path"])
        elif tool == "ffprobe":
            result = self.ffprobe(path or cfg["ffprobe_path"])
        elif tool == "curl_cffi":
            result = self.curl_cffi()
        elif tool == "ytdlp_ejs":
            result = self.ytdlp_ejs()
        else:
            return {"ok": False, "error": f"Unknown tool: {tool}"}
        return result

    def impersonate_targets(self) -> dict[str, Any]:
        cfg = self._settings.typed()
        path = cfg["ytdlp_path"]
        try:
            _, output = self._run(path, ["--list-impersonate-targets"], timeout=30)
        except ToolError as exc:
            return {"ok": False, "error": str(exc), "output": "", "targets": []}
        targets: list[dict[str, Any]] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith(("client", "available")):
                continue
            if "curl_cffi" in stripped or stripped.lower().startswith("chrome"):
                available = "unavailable" not in stripped.lower()
                name = stripped.split()[0]
                targets.append({"name": name, "available": available, "detail": stripped})
        return {"ok": True, "error": None, "output": output.strip(), "targets": targets}
