"""Shared pytest fixtures for v1-routines tests.

Auth is set up via env var so every test gets a known DASHBOARD_PASSWORD.
GITHUB_PAT is also stubbed so write paths exercise the auth-required
branch (without actually hitting GitHub — respx mocks intercept).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

# Set env BEFORE importing dashboard — the module reads them at import.
os.environ.setdefault("DASHBOARD_PASSWORD", "test-pw")
os.environ.setdefault("GITHUB_PAT", "ghp_test_token_for_respx_mocks")
os.environ.setdefault("GITHUB_REPO_SLUG", "emstacho-su/ai-news-agent-routines")
os.environ.setdefault("GITHUB_DATA_BRANCH", "data")

import config  # noqa: E402
import dashboard  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def app():
    return dashboard.create_app()


@pytest.fixture
def client(app, tmp_path: Path, monkeypatch) -> Iterator[TestClient]:
    """TestClient with local data dir pointing at a tmp path so saved/
    read/profile tests don't pollute the real data/ dir."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_STATE_FILE", tmp_path / "dashboard_state.json")
    monkeypatch.setattr(config, "SAVED_ITEMS_FILE", tmp_path / "saved_items.json")
    monkeypatch.setattr(config, "PROFILE_FILE", tmp_path / "profile.md")
    # Reset the dashboard's in-memory cache between tests so mocked
    # GitHub responses always go through the network mock.
    dashboard._CACHE.clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth():
    """HTTP basic auth tuple matching DASHBOARD_PASSWORD."""
    return ("stack", "test-pw")
