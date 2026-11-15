"""Application context, template environment and rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import APP_NAME, __version__
from .config import AppConfig
from .database import Database
from .services.monitor import Monitor
from .services.platforms import PLATFORMS, platform_color, platform_label
from .services.settings import SettingsService
from .services.tools import ToolService
from .utils import (
    format_local,
    human_duration,
    human_size,
    relative_time,
)

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


@dataclass
class AppContext:
    config: AppConfig
    db: Database
    settings: SettingsService
    tools: ToolService
    monitor: Monitor
    templates: Jinja2Templates


def build_context(config: AppConfig) -> AppContext:
    config.ensure_directories()
    db = Database(config.database_path)
    db.init()
    settings = SettingsService(db, config)
    tools = ToolService(settings)
    monitor = Monitor(db, settings, config, tools)
    templates = build_templates(config)
    return AppContext(
        config=config,
        db=db,
        settings=settings,
        tools=tools,
        monitor=monitor,
        templates=templates,
    )


def build_templates(config: AppConfig) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    env = templates.env
    env.filters["human_size"] = human_size
    env.filters["human_duration"] = human_duration
    env.filters["localtime"] = lambda value: format_local(value, config.timezone)
    env.filters["reltime"] = relative_time
    env.filters["basename"] = lambda value: Path(value).name if value else "—"
    env.globals["app_name"] = APP_NAME
    env.globals["app_version"] = __version__
    env.globals["platforms"] = PLATFORMS
    env.globals["platform_label"] = platform_label
    env.globals["platform_color"] = platform_color
    return templates


def render(
    ctx: AppContext,
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    **context,
):
    base = {
        "request": request,
        "app_name": APP_NAME,
        "app_version": __version__,
        "settings": ctx.settings.typed(),
        "monitor_state": ctx.monitor.status(),
        "platforms": PLATFORMS,
        "current_path": request.url.path,
        "timezone": ctx.config.timezone,
        "data_dir": ctx.config.data_dir,
        "database_path": ctx.config.database_path,
        "recordings_path": ctx.config.recordings_dir,
        "recording_log_dir": ctx.config.recording_log_dir,
        "app_log_path": ctx.config.app_log_path,
    }
    base.update(context)
    return ctx.templates.TemplateResponse(request, template_name, base, status_code=status_code)


def channel_display_status(channel: dict, active: dict | None) -> str:
    """Return a UI status string for a channel row."""
    if active:
        return "recording"
    status = channel.get("last_status") or "unknown"
    if status not in {"live", "offline", "error", "unknown"}:
        return "unknown"
    return status
