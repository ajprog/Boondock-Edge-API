"""Settings repository serialization tests."""

import sqlite3
from datetime import datetime, timezone
from app.services import db_initializer, settings_manager


def test_new_settings_database_can_store_datetime(monkeypatch, tmp_path):
    database = tmp_path / "settings.db"
    monkeypatch.setattr(settings_manager, "SETTINGS_DB_PATH", database)
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: database)
    db_initializer._create_database_schema()
    monkeypatch.setattr(settings_manager.SettingsManager, "_instance", None)
    manager = settings_manager.SettingsManager()
    timestamp = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)

    assert manager.set_setting("last_run", timestamp)
    assert manager.get_setting("last_run") == timestamp

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value, type FROM settings WHERE key = 'last_run'"
        ).fetchone() == (timestamp.isoformat(), "datetime")
