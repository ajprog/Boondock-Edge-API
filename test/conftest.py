"""Shared pytest fixtures used across behavior-focused test modules."""

import pytest

from app.services import db_initializer
from app.services import settings_manager as settings_module


@pytest.fixture
def settings_manager_factory(monkeypatch):
    """Construct the production SettingsManager against a test database."""
    managers = []

    def create(database):
        settings_module.SettingsManager._instance = None
        monkeypatch.setattr(settings_module, "SETTINGS_DB_PATH", database)
        manager = settings_module.SettingsManager()
        managers.append(manager)
        return manager

    yield create
    settings_module.SettingsManager._instance = None


@pytest.fixture
def initialized_settings_manager(tmp_path, monkeypatch, settings_manager_factory):
    """Create the production settings schema and return its repository."""
    database = tmp_path / "settings.db"
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: database)
    db_initializer._create_database_schema()
    return settings_manager_factory(database)


@pytest.fixture
def admin_auth(monkeypatch, initialized_settings_manager):
    """Issue a real administrator credential through the production repository."""
    from datetime import datetime, timedelta, timezone

    from app.utils import auth

    manager = initialized_settings_manager
    manager.save_user(
        "admin@example.com",
        {
            "name": "Admin",
            "password": "unused",
            "role": "admin",
            "status": "Active",
            "groups": [],
        },
    )
    token, _ = manager.issue_credential(
        "user",
        "admin@example.com",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        token="test-admin-token",
    )
    monkeypatch.setattr(auth, "_settings_manager", manager)
    return {"Authorization": f"Bearer {token}"}
