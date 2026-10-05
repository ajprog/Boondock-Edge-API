"""Phase-one authorization schema setup and upgrade migration."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.utils.sqlite_utils import connect_sqlite

DEFAULT_GROUP_NAME = "Default"
MIGRATION_NAME = "2026_09_authorization_phase_1_complete"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection, table):
    if not _table_exists(connection, table):
        return set()
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _permissions(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        permissions = [name.replace(':', '.') for name, enabled in value.items() if enabled]
    elif isinstance(value, list):
        permissions = [name.replace(':', '.') for name in value if isinstance(name, str)]
    else:
        return []
    if 'inbox' in permissions or 'play_audio' in permissions:
        permissions.append('recording.read')
    if 'delete_audio' in permissions:
        permissions.append('recording.delete')
    if 'view_reports' in permissions:
        permissions.append('incident_report.read')
    if 'create_reports' in permissions:
        permissions.append('incident_report.create')
    if 'modify_reports' in permissions:
        permissions.append('incident_report.update')
    return list(dict.fromkeys(permissions))


def _create_authorization_tables(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            permissions TEXT NOT NULL DEFAULT '[]'
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            id TEXT PRIMARY KEY,
            principal_type TEXT NOT NULL CHECK (
                principal_type IN ('user', 'api_key', 'device')
            ),
            principal_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_credentials_principal "
        "ON credentials(principal_type, principal_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_credentials_expires_at "
        "ON credentials(expires_at)"
    )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS channel_owners (
            channel_id INTEGER NOT NULL,
            owner_type TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            PRIMARY KEY (channel_id, owner_type, owner_id),
            FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
        )
    """)
    connection.execute(
        """INSERT OR IGNORE INTO groups
           (name, description, is_default, permissions)
           VALUES (?, 'Default user group', 1, '[]')""",
        (DEFAULT_GROUP_NAME,),
    )


def _create_api_keys_table(connection):
    connection.execute("""
        CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            permissions TEXT NOT NULL,
            owner TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT
        )
    """)


def _migrate_api_keys(connection, default_group_id):
    if not _table_exists(connection, "api_keys"):
        _create_api_keys_table(connection)
        return

    columns = _columns(connection, "api_keys")
    if {"permissions", "owner"}.issubset(columns) and "key_hash" not in columns:
        return

    connection.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy")
    _create_api_keys_table(connection)
    now = datetime.now(timezone.utc)
    for api_key in connection.execute(
        "SELECT * FROM api_keys_legacy WHERE revoked=0"
    ).fetchall():
        expires_at = api_key["expires_at"]
        try:
            if expires_at:
                parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed <= now:
                    continue
        except (TypeError, ValueError):
            continue
        cursor = connection.execute(
            """INSERT INTO api_keys
               (name, permissions, owner, created_at, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (api_key["name"],
             json.dumps(_permissions(api_key["scopes"])),
             f"group:{default_group_id}", api_key["created_at"] or _now_iso(),
             api_key["created_by"]),
        )
        connection.execute(
            """INSERT INTO credentials
               (id, principal_type, principal_id, token_hash, created_at, expires_at)
               VALUES (?, 'api_key', ?, ?, ?, ?)""",
            (str(uuid.uuid4()), cursor.lastrowid, api_key["key_hash"],
             api_key["created_at"] or _now_iso(), expires_at),
        )
    connection.execute("DROP TABLE api_keys_legacy")


def upgrade_authorization_schema(db_path):
    """Upgrade an existing installation in one idempotent transaction.

    The migration completes the schema conversion in this transaction; legacy
    authorization tables are removed only after their data has been migrated.
    """
    connection = connect_sqlite(db_path, row_factory=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _create_authorization_tables(connection)
        if connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)
        ).fetchone():
            connection.commit()
            return False

        if "groups" not in _columns(connection, "users"):
            connection.execute(
                "ALTER TABLE users ADD COLUMN groups TEXT NOT NULL DEFAULT '[]'"
            )

        default_group_id = connection.execute(
            "SELECT id FROM groups WHERE name=?", (DEFAULT_GROUP_NAME,)
        ).fetchone()["id"]

        profile_ids = {}
        if _table_exists(connection, "profiles"):
            for profile in connection.execute("SELECT * FROM profiles").fetchall():
                if profile["name"].lower() == "admin":
                    continue
                if profile["name"] == DEFAULT_GROUP_NAME:
                    group_id = default_group_id
                    connection.execute(
                        """UPDATE groups SET description=?, is_default=?, permissions=?
                           WHERE id=?""",
                        (profile["description"], profile["is_default"],
                         json.dumps(_permissions(profile["features"])), group_id),
                    )
                else:
                    cursor = connection.execute(
                        """INSERT INTO groups
                           (name, description, is_default, permissions)
                           VALUES (?, ?, ?, ?)""",
                        (profile["name"], profile["description"], profile["is_default"],
                         json.dumps(_permissions(profile["features"]))),
                    )
                    group_id = cursor.lastrowid
                profile_ids[profile["name"]] = group_id

        if _table_exists(connection, "users"):
            for user in connection.execute("SELECT email, role, profile FROM users").fetchall():
                group_ids = [] if user["role"] == "admin" else [
                    profile_ids.get(user["profile"], default_group_id)
                ]
                connection.execute(
                    "UPDATE users SET groups=? WHERE email=?",
                    (json.dumps(group_ids), user["email"]),
                )
            connection.execute("ALTER TABLE users DROP COLUMN profile")

        if _table_exists(connection, "channels"):
            connection.execute(
                """INSERT OR IGNORE INTO channel_owners
                   (channel_id, owner_type, owner_id)
                   SELECT id, 'group', ? FROM channels""",
                (default_group_id,),
            )

        _migrate_api_keys(connection, default_group_id)
        connection.execute("DROP TABLE IF EXISTS tokens")
        connection.execute("DROP TABLE IF EXISTS profiles")

        connection.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, _now_iso()),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
