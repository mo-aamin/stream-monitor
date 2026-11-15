from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.context import build_context
from app.main import create_app


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    data_dir = tmp_path / "data"
    return AppConfig(
        data_dir=data_dir,
        recordings_dir=tmp_path / "recordings",
        database_path=data_dir / "test.db",
        host="127.0.0.1",
        port=8080,
        timezone="UTC",
        log_level="INFO",
        monitor_autostart=False,
    )


@pytest.fixture
def ctx(config: AppConfig):
    return build_context(config)


@pytest.fixture
def client(config: AppConfig):
    with TestClient(create_app(config, start_monitor=False)) as test_client:
        yield test_client
