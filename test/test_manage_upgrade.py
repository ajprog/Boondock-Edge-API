import json
import sqlite3

import pytest

import manage
from test.test_release_metadata import build_info


@pytest.fixture
def installation(monkeypatch, tmp_path):
    data = tmp_path / "data"
    code = tmp_path / "api"
    data.mkdir()
    code.mkdir()
    info = build_info(version="2.0.0-beta.10")
    (code / "build-info.json").write_text(json.dumps(info))
    monkeypatch.setattr(manage, "DATA_ROOT", data)
    monkeypatch.setattr(manage, "CODE_ROOT", code)
    return data, code, info


@pytest.mark.parametrize("manifest", [None, {}, {"api": {}}, {"dashboard": {"version": "1.0.0"}}])
def test_missing_version_runs_all_migrations(monkeypatch, installation, manifest):
    data, code, info = installation
    if manifest is not None:
        (data / "manifest.json").write_text(json.dumps(manifest))
    called = []
    monkeypatch.setattr("migrations.authorization.upgrade_authorization_schema",
                        lambda path: called.append("authorization"))
    monkeypatch.setattr("migrations.dashboard_cutover.upgrade_dashboard_schema",
                        lambda settings, recordings: called.append("dashboard"))
    manage.upgrade()
    assert called == ["authorization", "dashboard"]
    assert json.loads((data / "manifest.json").read_text()) == {**(manifest or {}), "api": info}


@pytest.mark.parametrize("current", [
    "2.0.0-beta.1", "2.0.0-beta.2", "2.0.0-beta.9", "2.0.0-beta.10",
])
def test_installed_version_skips_pre_manifest_migrations(monkeypatch, installation, current):
    data, _, info = installation
    original = {"api": {"version": current}, "dashboard": {"version": "3.0.0"}, "future": [1]}
    (data / "manifest.json").write_text(json.dumps(original))
    def unexpected(*args):
        pytest.fail("Pre-manifest migrations must not run for a versioned installation")
    monkeypatch.setattr("migrations.authorization.upgrade_authorization_schema", unexpected)
    monkeypatch.setattr("migrations.dashboard_cutover.upgrade_dashboard_schema", unexpected)
    manage.upgrade()
    assert json.loads((data / "manifest.json").read_text()) == {**original, "api": info}


def test_failure_preserves_manifest_bytes(monkeypatch, installation):
    data, _, _ = installation
    path = data / "manifest.json"
    original = '{ "dashboard": {"custom": 42} }\n'
    path.write_text(original)
    def fail(*args):
        raise RuntimeError("failed migration")
    monkeypatch.setattr("migrations.authorization.upgrade_authorization_schema", fail)
    with pytest.raises(RuntimeError):
        manage.upgrade()
    assert path.read_text() == original


def test_failure_does_not_create_missing_manifest(monkeypatch, installation):
    data, _, _ = installation
    def fail(*args):
        raise RuntimeError("failed migration")
    monkeypatch.setattr("migrations.authorization.upgrade_authorization_schema", fail)
    with pytest.raises(RuntimeError):
        manage.upgrade()
    assert not (data / "manifest.json").exists()


def test_invalid_build_is_rejected_before_migration(monkeypatch, installation):
    data, code, _ = installation
    (code / "build-info.json").write_text('{"version":"2.0.0"}')
    def unexpected(*args):
        pytest.fail("Invalid build must be rejected before migrations")
    monkeypatch.setattr("migrations.authorization.upgrade_authorization_schema", unexpected)
    with pytest.raises(ValueError):
        manage.upgrade()
    assert not (data / "manifest.json").exists()


def test_legacy_migrations_are_safe_to_retry_on_current_schema(monkeypatch, installation):
    from app.services import db_initializer
    data, _, info = installation
    settings = data / "settings.db"
    monkeypatch.setattr(manage.Config, "get_settings_db_path", lambda: settings)
    monkeypatch.setattr(manage.Config, "get_recordings_db_path", lambda: data / "absent.db")
    db_initializer._create_database_schema()
    from migrations.authorization import upgrade_authorization_schema
    from migrations.dashboard_cutover import upgrade_dashboard_schema
    upgrade_authorization_schema(settings)
    upgrade_dashboard_schema(settings, data / "absent.db")
    # Simulate failure before writing the manifest: both migrations run again.
    manage.upgrade()
    with sqlite3.connect(settings) as database:
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_migrations" not in tables
        assert database.execute("SELECT COUNT(*) FROM groups WHERE name='Default'").fetchone()[0] == 1
    assert json.loads((data / "manifest.json").read_text())["api"] == info


def test_upgrade_requires_no_version_arguments():
    assert manage.build_parser().parse_args(["upgrade"]).command == "upgrade"


def test_authorization_legacy_upgrade_preserves_data_on_retry(tmp_path):
    from migrations.authorization import upgrade_authorization_schema
    settings = tmp_path / "legacy.db"
    with sqlite3.connect(settings) as database:
        database.executescript("""
            CREATE TABLE users (email TEXT PRIMARY KEY, role TEXT, profile TEXT);
            INSERT INTO users VALUES ('member@example.com', 'member', 'Dispatch');
            CREATE TABLE profiles (name TEXT, description TEXT, is_default INTEGER, features TEXT);
            INSERT INTO profiles VALUES ('Dispatch', 'Dispatch team', 0, '["inbox"]');
            CREATE TABLE channels (id INTEGER PRIMARY KEY);
            INSERT INTO channels VALUES (1);
        """)
    assert upgrade_authorization_schema(settings)
    with sqlite3.connect(settings) as database:
        expected = database.execute("SELECT groups FROM users").fetchone()[0]
        group_id = database.execute("SELECT id FROM groups WHERE name='Dispatch'").fetchone()[0]
        database.execute("DELETE FROM channel_owners")
        database.execute("INSERT INTO channel_owners VALUES (1,'group',?)", (str(group_id),))
    assert upgrade_authorization_schema(settings)
    with sqlite3.connect(settings) as database:
        assert database.execute("SELECT groups FROM users").fetchone()[0] == expected
        assert database.execute("SELECT COUNT(*) FROM groups WHERE name='Dispatch'").fetchone()[0] == 1
        assert database.execute("SELECT owner_id FROM channel_owners").fetchall() == [(str(group_id),)]
        assert not database.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
