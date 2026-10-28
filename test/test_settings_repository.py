"""Settings repository serialization tests."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from app.services import db_initializer, settings_manager
from app.utils.sqlite_utils import connect_sqlite


def test_synced_column_upgrade_preserves_existing_channels(monkeypatch, tmp_path):
    from migrations.channel_settings_sync import upgrade_channel_settings_schema

    database = tmp_path / 'old-settings.db'
    monkeypatch.setattr(db_initializer.Config, 'get_settings_db_path', lambda: database)
    db_initializer._create_database_schema()
    with sqlite3.connect(database) as connection:
        fresh_columns = connection.execute('PRAGMA table_info(channels)').fetchall()
        connection.execute('ALTER TABLE channels DROP COLUMN synced')
        connection.execute("INSERT INTO channels (mac, threshold) VALUES ('AABBCCDDEEFF', '35')")
    db_initializer._create_database_schema()
    with sqlite3.connect(database) as connection:
        assert 'synced' not in {row[1] for row in connection.execute('PRAGMA table_info(channels)')}
    upgrade_channel_settings_schema(database)
    upgrade_channel_settings_schema(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute('PRAGMA table_info(channels)').fetchall() == fresh_columns
        assert connection.execute('SELECT threshold, synced FROM channels').fetchone() == ('35', 0)
        connection.execute('UPDATE channels SET synced=1')
    upgrade_channel_settings_schema(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT synced FROM channels').fetchone() == (1,)
    # The standalone command works without any manage.py integration.
    import runpy
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'migrations' / 'channel_settings_sync.py'),
                  run_name='__main__')
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT synced FROM channels').fetchone() == (1,)


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


def test_typed_connection_converts_boolean_and_json(tmp_path):
    database = tmp_path / "typed.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE values_table (enabled BOOLEAN, payload JSON)")
        connection.execute(
            "INSERT INTO values_table VALUES (?, ?)",
            (True, '{"items":[1,2]}'),
        )

    connection = connect_sqlite(database, row_factory=True, typed=True)
    row = connection.execute("SELECT * FROM values_table").fetchone()
    connection.close()

    assert row["enabled"] is True
    assert row["payload"] == {"items": [1, 2]}


def test_user_repository_round_trips_typed_fields(initialized_settings_manager):
    manager = initialized_settings_manager
    preferences = {
        "display": {"time_format": "24h"},
        "inbox": {"records_per_page": 20},
        "reports": {"density": "comfortable"},
    }
    assert manager.save_user(
        "typed@example.com",
        {
            "name": "Typed User",
            "password": "hash",
            "role": "member",
            "mfa_enabled": True,
            "mfa_secret": "enabled-secret",
            "groups": [],
            "preferences": preferences,
        },
    )

    user = manager.get_user("typed@example.com")
    assert user["mfa_enabled"] is True
    assert user["mfa_secret"] == "enabled-secret"
    assert user["groups"] == []
    assert user["preferences"] == preferences
    assert manager.get_user_by_id(user["id"])["email"] == "typed@example.com"
    assert manager.count_users() == 1
    safe_user = manager.get_user_by_id(user["id"], safe=True)
    assert set(safe_user) == {
        "id", "email", "name", "role", "groups",
        "permissions", "keywords", "preferences",
    }

    replacement = {**preferences, "inbox": {"records_per_page": 0}}
    assert manager.update_user_by_id(user["id"], {"preferences": replacement})
    assert manager.get_user_by_id(user["id"])["preferences"] == replacement


def test_new_user_preferences_are_resolved_from_group_defaults(
    initialized_settings_manager,
):
    manager = initialized_settings_manager
    first = manager.save_group({
        "name": "First",
        "permissions": [],
        "default_preferences": {
            "inbox": {
                "time_format": "12h",
                "records_per_page": 20,
            },
            "reports": {},
        },
    })
    second = manager.save_group({
        "name": "Second",
        "permissions": [],
        "default_preferences": {
            "inbox": {
                "time_format": "24h",
                "show_channel": True,
            },
            "reports": {"density": "compact"},
        },
    })

    assert manager.save_user(
        "defaults@example.com",
        {
            "name": "Defaults",
            "password": "hash",
            "role": "member",
            "groups": [second, first],
        },
    )

    assert manager.get_user("defaults@example.com")["preferences"] == {
        "inbox": {
            "time_format": "24h",
            "records_per_page": 20,
            "show_channel": True,
        },
        "reports": {"density": "compact"},
    }


def test_keywords_can_be_shared_and_deleted_globally(initialized_settings_manager):
    manager = initialized_settings_manager
    first = manager.save_group({
        "name": "First keywords", "permissions": [],
        "keywords": [{"pattern": "engine fire"}],
    })
    keyword_id = manager.get_group_by_id(first)["keywords"][0]
    second = manager.save_group({
        "name": "Second keywords", "permissions": [], "keywords": [keyword_id],
    })

    assert manager.get_group_by_id(second)["keyword_details"][0]["pattern"] == "engine fire"
    assert manager.delete_keyword(keyword_id)
    assert manager.get_group_by_id(first)["keywords"] == []
    assert manager.get_group_by_id(second)["keywords"] == []
