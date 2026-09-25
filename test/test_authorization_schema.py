"""Clean-install authorization schema and ownership tests."""

import sqlite3


def test_new_install_creates_complete_authorization_schema(monkeypatch, tmp_path):
    from app.services import db_initializer
    from app.services import settings_manager as settings_module

    database = tmp_path / "settings.db"
    monkeypatch.setattr(settings_module, "SETTINGS_DB_PATH", database)
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: database)
    db_initializer._create_database_schema()
    settings_module.SettingsManager._instance = None
    settings_module.SettingsManager()

    connection = sqlite3.connect(database)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    api_key_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(api_keys)")
    }
    group_id_column = connection.execute("PRAGMA table_info(groups)").fetchone()
    migration_count = connection.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    connection.close()
    settings_module.SettingsManager._instance = None

    assert {"credentials", "groups", "channel_owners", "api_keys"}.issubset(tables)
    assert "profiles" not in tables
    assert "tokens" not in tables
    assert "groups" in user_columns
    assert "profile" not in user_columns
    assert api_key_columns == {
        "id",
        "name",
        "permissions",
        "owner",
        "created_at",
        "created_by",
    }
    assert group_id_column[1:3] == ("id", "INTEGER")
    assert migration_count == 1


def test_new_channel_receives_default_group_owner(monkeypatch, tmp_path):
    from app.services import db_initializer
    from app.services import settings_manager as settings_module

    database = tmp_path / "settings.db"
    monkeypatch.setattr(settings_module, "SETTINGS_DB_PATH", database)
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: database)
    db_initializer._create_database_schema()
    settings_module.SettingsManager._instance = None
    manager = settings_module.SettingsManager()
    default_group_id = manager.save_group(
        {
            "name": "Default",
            "description": "Default user group",
            "is_default": True,
            "permissions": [],
        }
    )

    channel_id = manager.save_channel({"name": "New radio", "mac": "112233445566"})

    connection = sqlite3.connect(database)
    owner = connection.execute(
        """SELECT owner_type, owner_id FROM channel_owners
           WHERE channel_id=?""",
        (channel_id,),
    ).fetchone()
    connection.close()
    settings_module.SettingsManager._instance = None
    assert owner == ("group", str(default_group_id))
