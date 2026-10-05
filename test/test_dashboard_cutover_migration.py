import json
import sqlite3

from migrations.dashboard_cutover import upgrade_dashboard_schema


def test_upgrade_dashboard_schema_migrates_preferences_identity_and_recordings(tmp_path):
    settings_path = tmp_path / "settings.db"
    recordings_path = tmp_path / "event.db"
    db = sqlite3.connect(settings_path)
    db.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, type TEXT NOT NULL);
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, description TEXT,
            is_default INTEGER, permissions TEXT);
        INSERT INTO groups VALUES (7, 'Race Control', '', 1, '["recording.read"]');
        CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT NOT NULL, password TEXT NOT NULL,
            role TEXT, status TEXT, access_level TEXT, mfa_enabled INTEGER, created_at TEXT,
            login_history TEXT, devices TEXT, groups TEXT);
        INSERT INTO users VALUES ('admin@example.com','Admin','hash','admin','Active',NULL,0,
            NULL,'[]','[]','[7]');
        CREATE TABLE pagination_preferences (email TEXT PRIMARY KEY, records_per_page INTEGER,
            current_page INTEGER, reverse_sort INTEGER, show_full_timestamps INTEGER);
        INSERT INTO pagination_preferences VALUES ('admin@example.com', 50, 1, 1, 1);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, mac TEXT);
        CREATE TABLE hallucinations (id INTEGER PRIMARY KEY, data TEXT);
        INSERT INTO hallucinations VALUES (3, '{"pattern":"*engine fire*"}');
    """)
    settings = {
        "global_inbox_view_mode": "pagination", "global_inbox_records_per_page": 20,
        "global_hallucination": True, "global_show_duplicate_files": True,
        "global_model": "base.en", "global_transcription_queue_enabled": False,
        "keywords": ["urgent"],
    }
    db.executemany("INSERT INTO settings VALUES (?,?,?)", [
        (key, json.dumps(value), "json") for key, value in settings.items()
    ])
    db.commit()
    db.close()

    recordings = sqlite3.connect(recordings_path)
    recordings.execute("""CREATE TABLE recordings (
        id INTEGER PRIMARY KEY, timestamp TEXT, transcription TEXT)""")
    recordings.execute("INSERT INTO recordings VALUES (1,'2026-09-22T10:00:00Z','An engine fire was reported')")
    recordings.commit()
    recordings.close()

    assert upgrade_dashboard_schema(settings_path, recordings_path)
    assert upgrade_dashboard_schema(settings_path, recordings_path)

    db = sqlite3.connect(settings_path)
    db.row_factory = sqlite3.Row
    user = db.execute("SELECT * FROM users").fetchone()
    preferences = json.loads(user["preferences"])
    assert user["id"] == 1
    assert preferences["inbox"]["records_per_page"] == 50
    assert preferences["inbox"]["sort_direction"] == "oldest_first"
    assert preferences["inbox"]["show_hallucinations"] is False
    keyword_ids = json.loads(db.execute("SELECT keywords FROM groups WHERE id=7").fetchone()[0])
    assert len(keyword_ids) == 1
    keyword = db.execute("SELECT * FROM keywords WHERE id=?", (keyword_ids[0],)).fetchone()
    assert (keyword["pattern"], keyword["match_type"], keyword["case_sensitive"]) == (
        "urgent", "literal", 0
    )
    assert {r[1] for r in db.execute("PRAGMA table_info(channels)")} >= {"device_hostname", "device_ip"}
    assert db.execute("SELECT value FROM settings WHERE key='model'").fetchone() is not None
    assert db.execute("SELECT 1 FROM settings WHERE key='global_model'").fetchone() is None
    assert json.loads(db.execute(
        "SELECT value FROM settings WHERE key='transcription_queue_enabled'"
    ).fetchone()[0]) is False
    assert db.execute(
        "SELECT 1 FROM settings WHERE key='global_transcription_queue_enabled'"
    ).fetchone() is None
    pattern = db.execute("SELECT pattern,match_type FROM hallucinations WHERE id=3").fetchone()
    assert pattern[1] == "regex"
    db.close()

    recordings = sqlite3.connect(recordings_path)
    row = recordings.execute("SELECT timestamp,is_hallucination,updated_at FROM recordings").fetchone()
    recordings.close()
    assert row[0] == 1790071200000
    assert row[1] == 1
    assert row[2] == 1790071200000


def test_clean_install_schema_contains_only_current_cutover_columns(monkeypatch, tmp_path):
    from app.services import db_initializer, recordings_db_initializer

    settings_path = tmp_path / "settings.db"
    recordings_path = tmp_path / "recordings.db"
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: settings_path)
    monkeypatch.setattr(recordings_db_initializer, "DB_PATH", recordings_path)
    db_initializer._create_database_schema()
    recordings_db_initializer.initialize_db()

    settings = sqlite3.connect(settings_path)
    users = {r[1] for r in settings.execute("PRAGMA table_info(users)")}
    groups = {r[1] for r in settings.execute("PRAGMA table_info(groups)")}
    hallucinations = {r[1] for r in settings.execute("PRAGMA table_info(hallucinations)")}
    tables = {r[0] for r in settings.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    settings.close()
    assert users == {
        "id", "email", "name", "password", "role",
        "mfa_enabled", "mfa_enforced", "mfa_secret",
        "created_at", "groups", "preferences",
    }
    assert {"default_preferences", "keywords"} <= groups
    assert hallucinations == {"id", "pattern", "match_type", "case_sensitive"}
    assert "pagination_preferences" not in tables

    recordings = sqlite3.connect(recordings_path)
    recording_info = list(recordings.execute("PRAGMA table_info(recordings)"))
    columns = {r[1] for r in recording_info}
    timestamp_type = next(r[2] for r in recording_info if r[1] == "timestamp")
    recordings.close()
    assert {"is_hallucination", "updated_at"} <= columns
    assert timestamp_type == "INTEGER"
