"""Shared pytest fixtures using the application's real configuration and databases."""

from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from app.services import db_initializer, recordings_db_initializer
from app.services.settings_manager import get_settings_manager


@pytest.fixture
def initialized_settings_manager():
    """Create a clean settings DB at the path selected by real Config."""
    database = Config.get_settings_db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()

    db_initializer._create_database_schema()
    manager = get_settings_manager()

    yield manager

    if database.exists():
        database.unlink()


@pytest.fixture
def recording_store():
    """Create a clean recordings DB and recordings directory using real Config."""
    database = Config.get_recordings_db_path()
    recordings_dir = Config.get_db_dir().parent / "recordings"

    database.parent.mkdir(parents=True, exist_ok=True)
    recordings_dir.mkdir(parents=True, exist_ok=True)

    if database.exists():
        database.unlink()

    recordings_db_initializer.initialize_db()

    yield database, recordings_dir

    if database.exists():
        database.unlink()


@pytest.fixture
def admin_auth(initialized_settings_manager):
    """Issue a real administrator credential through the production repository."""
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
    return {"Authorization": f"Bearer {token}"}
