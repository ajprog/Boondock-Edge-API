"""One-time dashboard-contract database cutover.

Unlike the clean-install initializers, this module contains every compatibility
operation needed by an existing installation.  It deliberately does not use
``schema_migrations``: schema shape and the absence of legacy data make each
operation idempotent until a dedicated database versioning system is adopted.
"""
from __future__ import annotations

import fnmatch
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.utils.sqlite_utils import connect_sqlite

_OBSOLETE_SETTINGS = {
    "global_transcribe_node", "global_inbox_view_mode", "global_live_mode_enabled",
    "global_enable_usb_audio_devices", "s3_backup_time", "backup_time",
    "global_inbox_records_per_page", "global_show_duplicate_files", "global_hallucination",
}
_SETTING_RENAMES = {
    "global_target_language": "target_language",
    "global_model": "model",
    "global_transcribe_method": "method",
    "global_transcription_api_key": "transcription_api_key",
    "global_transcription_queue_enabled": "transcription_queue_enabled",
    "global_enable_uniden_scanners": "uniden_scanners_enabled",
    "global_enable_edge_devices": "edge_recorders_enabled",
    "global_enable_s3_upload": "s3_enabled",
    "samba_backup_enabled": "samba_enabled",
    "host_ssid": "ssid",
    "maintenance_time": "scheduled_time",
    "maintenance_enabled_tasks": "enabled_tasks",
}


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table):
    if table not in _tables(db):
        return set()
    return {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}


def _decode(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return _decode(row[0], default) if row else default


def _default_preferences(db, admin=False, pagination=None):
    pagination = pagination or {}
    view = _setting(db, "global_inbox_view_mode", "pagination")
    page_size = pagination.get("records_per_page")
    if page_size is None:
        page_size = _setting(db, "global_inbox_records_per_page", 20)
    inbox = {
        "records_per_page": 0 if view == "continuous" else int(page_size or 20),
        "sort_direction": "oldest_first" if pagination.get("reverse_sort") else "newest_first",
        "show_full_timestamps": bool(pagination.get("show_full_timestamps", False)),
        "show_time": True, "show_car": False, "show_channel": True, "show_person": False,
        "time_format": _setting(db, "cached_time_format", "24h")
    }
    if admin:
        inbox.update({
            "show_duplicate_recordings": bool(_setting(db, "global_show_duplicate_files", False)),
            "show_hallucinations": not bool(_setting(db, "global_hallucination", False)),
        })
    return {
        "inbox": inbox,
        "reports": {"density": _setting(db, "reports_density_mode", "comfortable")},
    }


def _upgrade_groups(db):
    columns = _columns(db, "groups")
    if "default_preferences" not in columns:
        db.execute("ALTER TABLE groups ADD COLUMN default_preferences JSON NOT NULL DEFAULT '{}'")
        db.execute("UPDATE groups SET default_preferences=?",
                   (json.dumps({"inbox": {}, "reports": {}}),))
    if "keywords" not in columns:
        db.execute("ALTER TABLE groups ADD COLUMN keywords JSON NOT NULL DEFAULT '[]'")
    db.execute("""CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern TEXT NOT NULL,
        match_type TEXT NOT NULL DEFAULT 'literal'
            CHECK(match_type IN ('literal','regex')),
        case_sensitive BOOLEAN NOT NULL DEFAULT FALSE)""")
    legacy_keywords = _setting(db, "keywords", [])
    if legacy_keywords:
        row = db.execute("SELECT id FROM groups WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()
        if row and db.execute("SELECT keywords FROM groups WHERE id=?", (row[0],)).fetchone()[0] == '[]':
            keyword_ids = []
            for value in dict.fromkeys(legacy_keywords):
                cursor = db.execute(
                    "INSERT INTO keywords(pattern,match_type,case_sensitive) VALUES(?,'literal',FALSE)",
                    (value,),
                )
                keyword_ids.append(cursor.lastrowid)
            db.execute("UPDATE groups SET keywords=? WHERE id=?", (json.dumps(keyword_ids), row[0]))


def _upgrade_users(db):
    if "users" not in _tables(db):
        return
    columns = _columns(db, "users")
    pagination = {}
    if "pagination_preferences" in _tables(db):
        pagination = {r["email"]: dict(r) for r in db.execute("SELECT * FROM pagination_preferences")}
    if {"id", "preferences"}.issubset(columns):
        return

    rows = db.execute("SELECT * FROM users ORDER BY email").fetchall()
    db.execute("ALTER TABLE users RENAME TO users_dashboard_legacy")
    db.execute("""CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, password TEXT NOT NULL, role TEXT,
        mfa_enabled BOOLEAN DEFAULT FALSE,
        mfa_enforced BOOLEAN DEFAULT FALSE, mfa_secret TEXT,
        created_at TEXT,
        groups JSON NOT NULL DEFAULT '[]',
        preferences JSON NOT NULL DEFAULT '{}'
    )""")
    for row in rows:
        old = dict(row)
        values = (
            old.get("id"), old["email"], old.get("name") or old["email"], old.get("password") or "",
            old.get("role"), old.get("mfa_enabled", 0),
            old.get("mfa_enforced", 0), old.get("mfa_secret"), old.get("created_at"),
            old.get("groups") or "[]",
            old.get("preferences") or json.dumps(_default_preferences(
                db, old.get("role") == "admin", pagination.get(old["email"])
            )),
        )
        db.execute("""INSERT INTO users
            (id,email,name,password,role,mfa_enabled,mfa_enforced,
             mfa_secret,created_at,groups,preferences)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", values)
    db.execute("DROP TABLE users_dashboard_legacy")
    db.execute("DROP TABLE IF EXISTS pagination_preferences")


def _upgrade_hallucinations(db):
    if "hallucinations" not in _tables(db):
        return
    if {"pattern", "match_type", "case_sensitive"}.issubset(_columns(db, "hallucinations")):
        return
    rows = db.execute("SELECT * FROM hallucinations").fetchall()
    db.execute("ALTER TABLE hallucinations RENAME TO hallucinations_dashboard_legacy")
    db.execute("""CREATE TABLE hallucinations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL,
        match_type TEXT NOT NULL DEFAULT 'literal' CHECK(match_type IN ('literal','regex')),
        case_sensitive BOOLEAN NOT NULL DEFAULT FALSE)""")
    for row in rows:
        data = _decode(dict(row).get("data"), {})
        if isinstance(data, str):
            pattern = data
        elif isinstance(data, dict):
            pattern = data.get("pattern") or data.get("text") or data.get("value") or ""
        else:
            continue
        # Legacy patterns used shell-style wildcards. Preserve their meaning as Python regex.
        wildcard = any(char in pattern for char in "*?[")
        db.execute("INSERT INTO hallucinations(id,pattern,match_type,case_sensitive) VALUES(?,?,?,?)",
                   (dict(row).get("id"), fnmatch.translate(pattern) if wildcard else pattern,
                    "regex" if wildcard else "literal", 0))
    db.execute("DROP TABLE hallucinations_dashboard_legacy")


def _upgrade_settings(db):
    if _setting(db, "method") is None:
        method = "openai" if _setting(db, "global_transcribe_openai", False) else "local"
        db.execute("INSERT OR IGNORE INTO settings(key,value,type) VALUES('method',?,'string')", (method,))
    for old, new in _SETTING_RENAMES.items():
        db.execute("UPDATE OR IGNORE settings SET key=? WHERE key=?", (new, old))
        db.execute("DELETE FROM settings WHERE key=?", (old,))
    db.execute("DELETE FROM settings WHERE key IN (%s)" % ",".join("?" * len(_OBSOLETE_SETTINGS)),
               tuple(_OBSOLETE_SETTINGS))
    db.execute("DELETE FROM settings WHERE key IN ('keywords','cached_time_format','reports_density_mode')")
    db.execute("DELETE FROM settings WHERE key IN ('global_transcribe_local','global_transcribe_openai')")


def _to_milliseconds(value):
    if value is None:
        return 0
    try:
        number = float(value)
        return int(number if number >= 100_000_000_000 else number * 1000)
    except (TypeError, ValueError):
        try:
            text = str(value)
            try:
                parsed = datetime.strptime(text, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return 0


def _matches(text, patterns):
    if isinstance(text, str) and not text.strip():
        return 1
    for pattern, match_type, case_sensitive in patterns:
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = pattern if match_type == "regex" else re.escape(pattern)
        try:
            if re.search(expression, text or "", flags):
                return 1
        except re.error:
            continue
    return 0


def _upgrade_recordings(path, patterns):
    path = Path(path)
    if not path.exists():
        return
    db = connect_sqlite(path, row_factory=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        columns = _columns(db, "recordings")
        if "is_hallucination" not in columns:
            db.execute("""ALTER TABLE recordings ADD COLUMN is_hallucination
                BOOLEAN NOT NULL DEFAULT FALSE""")
        if "updated_at" not in columns:
            db.execute("ALTER TABLE recordings ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")
        history_exists = "recording_history" in _tables(db)
        history_expression = (
            "(SELECT MAX(created_at) FROM recording_history WHERE recording_id=recordings.id)"
            if history_exists else "NULL"
        )
        rows = db.execute(
            f"SELECT id,timestamp,transcription,updated_at,{history_expression} AS modified_at "
            "FROM recordings"
        ).fetchall()
        for row in rows:
            updated = (row["updated_at"] or _to_milliseconds(row["modified_at"])
                       or _to_milliseconds(row["timestamp"]))
            db.execute("UPDATE recordings SET updated_at=?,is_hallucination=? WHERE id=?",
                       (updated, _matches(row["transcription"], patterns), row["id"]))
        timestamp_type = next(
            (row[2].upper() for row in db.execute("PRAGMA table_info(recordings)")
             if row[1] == "timestamp"),
            "",
        )
        if timestamp_type != "INTEGER":
            rows = db.execute("SELECT * FROM recordings ORDER BY id").fetchall()
            db.execute("""CREATE TABLE recordings_dashboard_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                filename TEXT,
                timestamp INTEGER,
                transcription TEXT,
                status TEXT DEFAULT 'new',
                backed_up BOOLEAN DEFAULT FALSE,
                is_duplicate BOOLEAN DEFAULT FALSE,
                filesize INTEGER DEFAULT 0,
                duration REAL,
                crc TEXT,
                is_hallucination BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at INTEGER NOT NULL DEFAULT
                    (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
            )""")
            names = [column[1] for column in db.execute("PRAGMA table_info(recordings)")]
            insert_names = [name for name in names if name in _columns(db, "recordings_dashboard_new")]
            placeholders = ",".join("?" for _ in insert_names)
            for row in rows:
                values = dict(row)
                values["timestamp"] = _to_milliseconds(values["timestamp"])
                db.execute(
                    f"INSERT INTO recordings_dashboard_new ({','.join(insert_names)}) "
                    f"VALUES ({placeholders})",
                    [values[name] for name in insert_names],
                )
            db.execute("DROP TABLE recordings")
            db.execute("ALTER TABLE recordings_dashboard_new RENAME TO recordings")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recordings_timestamp ON recordings(timestamp DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_channel_timestamp ON recordings(channel_id,timestamp DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recordings_updated_at ON recordings(updated_at, id)")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upgrade_dashboard_schema(settings_path, recordings_path):
    """Upgrade settings and recordings databases; return whether work succeeded."""
    db = connect_sqlite(settings_path, row_factory=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        _upgrade_groups(db)
        _upgrade_users(db)
        if "channels" in _tables(db):
            if "device_hostname" not in _columns(db, "channels"):
                db.execute("ALTER TABLE channels ADD COLUMN device_hostname TEXT")
            if "device_ip" not in _columns(db, "channels"):
                db.execute("ALTER TABLE channels ADD COLUMN device_ip TEXT")
        _upgrade_hallucinations(db)
        patterns = [tuple(r) for r in db.execute(
            "SELECT pattern,match_type,case_sensitive FROM hallucinations"
        )] if "hallucinations" in _tables(db) else []
        _upgrade_settings(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _upgrade_recordings(recordings_path, patterns)
    return True
