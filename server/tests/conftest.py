"""Общие фикстуры тестов серверного распознавателя (`server/`)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Каждый тест работает на своей SQLite-БД в tmp,PostGIS-специфика не требуется."""
    monkeypatch.setenv("PHOENIX_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PHOENIX_DASHBOARD_AUTH", "false")
    monkeypatch.setenv("PHOENIX_DEBUG_KEEP_AUDIO", "false")
    monkeypatch.setenv("PHOENIX_GAP_ALERT_SEC", "180")
    monkeypatch.setenv("PHOENIX_SPOOL_MAX_BYTES", str(8 * 1024 * 1024))

    from server import config as config_module

    config_module.reload_settings()
    yield
    config_module.reload_settings()


@pytest.fixture(autouse=True)
def _reset_analyzer():
    from server import confirmation

    confirmation.reset_chunk_analyzer()
    yield
    confirmation.reset_chunk_analyzer()