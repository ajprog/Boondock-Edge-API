"""
Settings Manager - Centralized settings management with SQLite backend.

This module provides a fully encapsulated interface for all application settings.
No other part of the application should access settings.db directly.
All access must go through this SettingsManager class.
"""

import sqlite3
import hashlib
import secrets
import uuid
import json
import logging
import threading
import time
from config import Config, DATA_ROOT
from app.utils.sqlite_utils import connect_sqlite, log_slow_operation
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Get the database directory from centralized config
SETTINGS_DB_PATH = Config.get_settings_db_path()

# Thread lock for database access
_db_lock = threading.RLock()


def _deserialize_setting(value: str, setting_type: str) -> Any:
    """Convert a persisted setting string back to its declared Python type."""
    try:
        if setting_type == 'bool':
            parsed = json.loads(value)
            if not isinstance(parsed, bool):
                raise ValueError(f'Invalid bool value: {value}')
            return parsed
        if setting_type == 'int':
            return int(value)
        if setting_type == 'float':
            return float(value)
        if setting_type == 'json':
            return json.loads(value)
        if setting_type == 'datetime':
            return datetime.fromisoformat(value)
        return value
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Could not deserialize setting value %r as %s", value, setting_type)
        return value


def _serialize_setting(value: Any) -> tuple[str, str]:
    """Serialize a setting and return its persisted value and type."""
    if isinstance(value, bool):
        return json.dumps(value), 'bool'
    if isinstance(value, datetime):
        return value.isoformat(), 'datetime'
    if isinstance(value, int):
        return json.dumps(value), 'int'
    if isinstance(value, float):
        return json.dumps(value), 'float'
    if isinstance(value, str):
        return value, 'string'
    return json.dumps(value), 'json'

def normalize_mac_address(mac: str) -> str:
    """
    Normalize MAC address to uppercase and remove colons/dashes.
    
    Args:
        mac: MAC address in any format (e.g., "AA:BB:CC:DD:EE:FF" or "aabbccddeeff")
        
    Returns:
        Normalized MAC address (12 uppercase characters, no separators)
    """
    if not mac:
        return ""
    # Remove colons, dashes, and spaces, then convert to uppercase
    normalized = mac.replace(':', '').replace('-', '').replace(' ', '').upper()
    return normalized


class SettingsManager:
    """
    Centralized settings manager with full encapsulation.
    All settings are stored in settings.db SQLite database.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Singleton pattern to ensure only one instance exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(SettingsManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the SettingsManager (only once)."""
        if self._initialized:
            return
        
        self.db_path = SETTINGS_DB_PATH
        self._ensure_db_dir()
        self._initialized = True
    
    def _ensure_db_dir(self):
        """Ensure the database directory exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _get_connection(self):
        """Get a database connection with proper isolation."""
        return connect_sqlite(self.db_path, row_factory=True)
    
    # ==================== SETTINGS METHODS ====================
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value by key."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT value, type FROM settings WHERE key = ?', (key,))
                row = cursor.fetchone()
                if row:
                    return _deserialize_setting(row['value'], row['type'])
                return default
            finally:
                conn.close()
    
    def set_setting(self, key: str, value: Any) -> bool:
        """Set a setting value."""
        with _db_lock:
            conn = self._get_connection()
            try:
                # Convert value to JSON string if it's not a string
                value_str, setting_type = _serialize_setting(value)
                
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO settings (key, value, type) VALUES (?, ?, ?)',
                    (key, value_str, setting_type)
                )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting {key}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def get_all_settings(self) -> Dict[str, Any]:
        """Get all settings as a dictionary."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT key, value, type FROM settings')
                settings = {}
                for row in cursor.fetchall():
                    settings[row['key']] = _deserialize_setting(row['value'], row['type'])

                return settings
            finally:
                conn.close()
    
    def set_all_settings(self, settings: Dict[str, Any]) -> bool:
        """Set multiple settings at once."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                for key, value in settings.items():
                    value_str, setting_type = _serialize_setting(value)
                    
                    cursor.execute(
                        'INSERT OR REPLACE INTO settings (key, value, type) VALUES (?, ?, ?)',
                        (key, value_str, setting_type)
                    )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting all settings: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== USERS METHODS ====================
    
    def get_user(self, email: str) -> Optional[Dict[str, Any]]:
        """Get a user by email."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
                row = cursor.fetchone()
                if row:
                    user = dict(row)
                    # Parse JSON fields
                    if user.get('login_history'):
                        try:
                            user['login_history'] = json.loads(user['login_history'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['login_history'] = []
                    if user.get('devices'):
                        try:
                            user['devices'] = json.loads(user['devices'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['devices'] = []
                    if user.get('groups'):
                        try:
                            user['groups'] = json.loads(user['groups'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['groups'] = []
                    return user
                return None
            finally:
                conn.close()
    
    def get_all_users(self) -> Dict[str, Dict[str, Any]]:
        """Get all users as a dictionary keyed by email."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM users')
                users = {}
                for row in cursor.fetchall():
                    user = dict(row)
                    # Parse JSON fields
                    if user.get('login_history'):
                        try:
                            user['login_history'] = json.loads(user['login_history'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['login_history'] = []
                    if user.get('devices'):
                        try:
                            user['devices'] = json.loads(user['devices'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['devices'] = []
                    if user.get('groups'):
                        try:
                            user['groups'] = json.loads(user['groups'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['groups'] = []
                    users[user['email']] = user
                return users
            finally:
                conn.close()
    
    def save_user(self, email: str, user_data: Dict[str, Any]) -> bool:
        """Save or update a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                # Convert JSON fields to strings
                login_history = json.dumps(user_data.get('login_history', []))
                devices = json.dumps(user_data.get('devices', []))
                groups = json.dumps(user_data.get('groups', []))
                
                cursor.execute('''
                    INSERT OR REPLACE INTO users 
                    (email, name, password, role, status, access_level,
                     mfa_enabled, created_at, login_history, devices, groups)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    email,
                    user_data.get('name'),
                    user_data.get('password'),
                    user_data.get('role'),
                    user_data.get('status'),
                    user_data.get('accessLevel'),
                    user_data.get('mfa_enabled', 0),
                    user_data.get('created_at'),
                    login_history,
                    devices,
                    groups
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving user {email}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_user(self, email: str) -> bool:
        """Delete a user and all credentials issued to that user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM credentials WHERE principal_type='user' AND principal_id=?",
                    (email,),
                )
                cursor.execute('DELETE FROM users WHERE email = ?', (email,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting user {email}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== GROUP METHODS ====================

    def get_group(self, name: str) -> Optional[Dict[str, Any]]:
        """Get an authorization group by name."""
        with _db_lock:
            conn = self._get_connection()
            try:
                row = conn.execute('SELECT * FROM groups WHERE name = ?', (name,)).fetchone()
                if not row:
                    return None
                group = dict(row)
                group['permissions'] = json.loads(group.get('permissions') or '[]')
                group['is_default'] = bool(group.get('is_default'))
                return group
            finally:
                conn.close()

    def get_group_by_id(self, group_id: int) -> Optional[Dict[str, Any]]:
        """Get an authorization group by its stable integer ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                row = conn.execute('SELECT * FROM groups WHERE id=?', (group_id,)).fetchone()
                if not row:
                    return None
                group = dict(row)
                group['permissions'] = json.loads(group.get('permissions') or '[]')
                group['is_default'] = bool(group.get('is_default'))
                return group
            finally:
                conn.close()

    def get_all_groups(self) -> Dict[int, Dict[str, Any]]:
        """Get all authorization groups keyed by stable group ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                groups = {}
                for row in conn.execute('SELECT * FROM groups').fetchall():
                    group = dict(row)
                    group['permissions'] = json.loads(group.get('permissions') or '[]')
                    group['is_default'] = bool(group.get('is_default'))
                    groups[group['id']] = group
                return groups
            finally:
                conn.close()

    def save_group(self, group_data: Dict[str, Any], group_id: Optional[int] = None) -> int:
        """Create or update an authorization group and return its integer ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                if group_id is None:
                    existing = conn.execute(
                        'SELECT id FROM groups WHERE name=?', (group_data.get('name'),)
                    ).fetchone()
                    group_id = existing['id'] if existing else None
                if group_id is None:
                    cursor = conn.execute(
                        """INSERT INTO groups
                           (name, description, is_default, permissions)
                           VALUES (?, ?, ?, ?)""",
                        (group_data.get('name'), group_data.get('description'),
                         int(bool(group_data.get('is_default', False))),
                         json.dumps(group_data.get('permissions', []))),
                    )
                    group_id = cursor.lastrowid
                else:
                    conn.execute(
                        """UPDATE groups SET name=?, description=?, is_default=?, permissions=?
                           WHERE id=?""",
                        (group_data.get('name'), group_data.get('description'),
                         int(bool(group_data.get('is_default', False))),
                         json.dumps(group_data.get('permissions', [])), group_id),
                    )
                conn.commit()
                return group_id
            except Exception as e:
                logger.error(f"Error saving group {group_id}: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()

    def delete_group(self, group_id: int) -> bool:
        """Delete an unreferenced, non-default authorization group."""
        with _db_lock:
            conn = self._get_connection()
            try:
                group = conn.execute(
                    'SELECT is_default FROM groups WHERE id=?', (group_id,)
                ).fetchone()
                if not group or group['is_default']:
                    return False
                if any(
                    group_id in self._json_list(row['groups'])
                    for row in conn.execute('SELECT groups FROM users').fetchall()
                ):
                    return False
                owner = f'group:{group_id}'
                if conn.execute('SELECT 1 FROM api_keys WHERE owner=? LIMIT 1', (owner,)).fetchone():
                    return False
                if conn.execute(
                    """SELECT 1 FROM channel_owners
                       WHERE owner_type='group' AND owner_id=? LIMIT 1""",
                    (str(group_id),),
                ).fetchone():
                    return False
                cursor = conn.execute('DELETE FROM groups WHERE id=?', (group_id,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error deleting group {group_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    # ==================== TAGS METHODS ====================
    
    def get_all_tags(self) -> List[Dict[str, Any]]:
        """Get all tags."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tags ORDER BY id')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_tag(self, tag_data: Dict[str, Any]) -> int:
        """Save a tag and return its ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                
                # Check if tag exists in database (if id is provided)
                tag_exists = False
                if 'id' in tag_data and tag_data['id']:
                    cursor.execute('SELECT id FROM tags WHERE id = ?', (tag_data['id'],))
                    tag_exists = cursor.fetchone() is not None
                
                # Ensure created_at has a value
                created_at = tag_data.get('created_at')
                if not created_at:
                    created_at = datetime.utcnow().isoformat() + 'Z'
                
                if tag_exists:
                    # Update existing
                    cursor.execute('''
                        UPDATE tags SET name=?, category=?, usage_count=?, 
                        color=?, created_at=? WHERE id=?
                    ''', (
                        tag_data.get('name'),
                        tag_data.get('category'),
                        tag_data.get('usageCount', 0),
                        tag_data.get('color'),
                        created_at,
                        tag_data['id']
                    ))
                    conn.commit()
                    return tag_data['id']
                else:
                    # Insert new (ignore provided id if tag doesn't exist, let DB auto-increment)
                    cursor.execute('''
                        INSERT INTO tags (name, category, usage_count, color, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        tag_data.get('name'),
                        tag_data.get('category'),
                        tag_data.get('usageCount', 0),
                        tag_data.get('color'),
                        created_at
                    ))
                    conn.commit()
                    return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving tag: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_tag(self, tag_id: int) -> bool:
        """Delete a tag."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM tags WHERE id = ?', (tag_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting tag {tag_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def increment_tag_usage(self, tag_name: str) -> bool:
        """Increment usage_count for a tag by name."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE tags SET usage_count = usage_count + 1 
                    WHERE name = ?
                ''', (tag_name,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error incrementing usage count for tag '{tag_name}': {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def decrement_tag_usage(self, tag_name: str) -> bool:
        """Decrement usage_count for a tag by name (minimum 0)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE tags SET usage_count = MAX(usage_count - 1, 0) 
                    WHERE name = ?
                ''', (tag_name,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error decrementing usage count for tag '{tag_name}': {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== FREQUENCIES METHODS ====================
    
    def get_all_frequencies(self) -> List[Dict[str, Any]]:
        """Get all frequencies."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM frequencies ORDER BY id')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_frequency(self, freq_data: Dict[str, Any]) -> int:
        """Save a frequency and return its ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if 'id' in freq_data and freq_data['id']:
                    cursor.execute('''
                        UPDATE frequencies SET name=?, frequency=?, type=?, 
                        tone=?, tag=?, person=?, status=? WHERE id=?
                    ''', (
                        freq_data.get('name'),
                        freq_data.get('frequency'),
                        freq_data.get('type'),
                        freq_data.get('tone'),
                        freq_data.get('tag'),
                        freq_data.get('person'),
                        freq_data.get('status'),
                        freq_data['id']
                    ))
                    conn.commit()
                    return freq_data['id']
                else:
                    cursor.execute('''
                        INSERT INTO frequencies (name, frequency, type, tone, tag, person, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        freq_data.get('name'),
                        freq_data.get('frequency'),
                        freq_data.get('type'),
                        freq_data.get('tone'),
                        freq_data.get('tag'),
                        freq_data.get('person'),
                        freq_data.get('status')
                    ))
                    conn.commit()
                    return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving frequency: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_frequency(self, freq_id: int) -> bool:
        """Delete a frequency."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM frequencies WHERE id = ?', (freq_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting frequency {freq_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== CHANNELS METHODS ====================
    
    @staticmethod
    def _channel_owner_filter(owner_ids):
        """Build a portable channel-owner predicate and its bound values."""
        owners = []
        for owner in owner_ids or []:
            if not isinstance(owner, str) or ':' not in owner:
                continue
            owner_type, owner_id = owner.split(':', 1)
            if owner_type in {'user', 'group'} and owner_id:
                owners.append((owner_type, owner_id))
        if not owners:
            return '0', []
        clauses = ['(co.owner_type = ? AND co.owner_id = ?)' for _ in owners]
        values = [value for owner in owners for value in owner]
        return ' OR '.join(clauses), values

    def get_all_channels(self, owner_ids=None) -> List[Dict[str, Any]]:
        """Get active channels visible to the supplied owner scope."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                query = 'SELECT channels.* FROM channels WHERE channels.deleted = 0'
                parameters = []
                if owner_ids is not None:
                    predicate, parameters = self._channel_owner_filter(owner_ids)
                    query += (
                        ' AND EXISTS (SELECT 1 FROM channel_owners co '
                        f'WHERE co.channel_id = channels.id AND ({predicate}))'
                    )
                query += ' ORDER BY channels.id'
                cursor.execute(query, parameters)
                channels = []
                for row in cursor.fetchall():
                    channel = dict(row)
                    # Convert deleted integer to boolean for compatibility
                    channel['deleted'] = bool(channel.get('deleted', 0))
                    channels.append(channel)
                return channels
            finally:
                conn.close()
    
    def get_channel(self, channel_id: int, owner_ids=None) -> Optional[Dict[str, Any]]:
        """Get a channel by ID (excludes soft-deleted channels)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                query = 'SELECT channels.* FROM channels WHERE id = ? AND deleted = 0'
                parameters = [channel_id]
                if owner_ids is not None:
                    predicate, owner_parameters = self._channel_owner_filter(owner_ids)
                    query += (
                        ' AND EXISTS (SELECT 1 FROM channel_owners co '
                        f'WHERE co.channel_id = channels.id AND ({predicate}))'
                    )
                    parameters.extend(owner_parameters)
                cursor.execute(query, parameters)
                row = cursor.fetchone()
                if row:
                    channel = dict(row)
                    # Convert deleted integer to boolean for compatibility
                    channel['deleted'] = bool(channel.get('deleted', 0))
                    return channel
                return None
            finally:
                conn.close()
    
    def get_channel_by_mac(self, mac: str, owner_ids=None) -> Optional[Dict[str, Any]]:
        """Get a channel by MAC address."""
        # Normalize MAC address before querying
        normalized_mac = normalize_mac_address(mac)
        if not normalized_mac:
            return None
            
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                query = 'SELECT channels.* FROM channels WHERE mac = ? AND deleted = 0'
                parameters = [normalized_mac]
                if owner_ids is not None:
                    predicate, owner_parameters = self._channel_owner_filter(owner_ids)
                    query += (
                        ' AND EXISTS (SELECT 1 FROM channel_owners co '
                        f'WHERE co.channel_id = channels.id AND ({predicate}))'
                    )
                    parameters.extend(owner_parameters)
                cursor.execute(query, parameters)
                row = cursor.fetchone()
                if row:
                    channel = dict(row)
                    # Convert deleted integer to boolean for compatibility
                    channel['deleted'] = bool(channel.get('deleted', 0))
                    return channel
                return None
            finally:
                conn.close()
    
    def save_channel(self, channel_data: Dict[str, Any]) -> int:
        """Save a channel and return its ID."""
        # Normalize MAC address before saving
        if 'mac' in channel_data and channel_data['mac']:
            channel_data['mac'] = normalize_mac_address(channel_data['mac'])
        
        logger.debug(f"save_channel called with channel_data keys: {list(channel_data.keys())}, has 'id': {'id' in channel_data and channel_data.get('id')}")
        
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if 'id' in channel_data and channel_data['id']:
                    cursor.execute('''
                        UPDATE channels SET name=?, status=?, model=?, src_language=?,
                        target_language=?, color=?, background_color=?, team_color=?,
                        car=?, driver=?, person=?, tag=?, mac=?, audio_stream_enabled=?,
                        threshold=?, silence=?, min_rec=?, max_rec=?, audio_gain=?,
                        frequency=?, tone=?, type=?, deleted=?, audio_stream_port=?,
                        speaker_enabled=?, speaker_volume=? WHERE id=?
                    ''', (
                        channel_data.get('name'),
                        channel_data.get('status'),
                        channel_data.get('model'),
                        channel_data.get('src_language'),
                        channel_data.get('target_language'),
                        channel_data.get('color'),
                        channel_data.get('background_color'),
                        channel_data.get('team_color'),
                        channel_data.get('car'),
                        channel_data.get('driver'),
                        channel_data.get('person'),
                        channel_data.get('tag'),
                        channel_data.get('mac'),
                        channel_data.get('audio_stream_enabled', 0),
                        channel_data.get('threshold'),
                        channel_data.get('silence'),
                        channel_data.get('min_rec'),
                        channel_data.get('max_rec'),
                        channel_data.get('audio_gain'),
                        channel_data.get('frequency'),
                        channel_data.get('tone'),
                        channel_data.get('type'),
                        channel_data.get('deleted', 0),
                        channel_data.get('audio_stream_port'),
                        channel_data.get('speaker_enabled', 0),
                        channel_data.get('speaker_volume'),
                        channel_data['id']
                    ))
                    conn.commit()
                    logger.debug(f"Updated channel with id {channel_data['id']}")
                    return channel_data['id']
                else:
                    # Check if channel with this MAC already exists (handles race conditions)
                    normalized_mac = channel_data.get('mac')
                    if normalized_mac:
                        cursor.execute('SELECT id FROM channels WHERE mac = ? AND deleted = 0', (normalized_mac,))
                        existing = cursor.fetchone()
                        if existing:
                            logger.warning(f"Channel with MAC {normalized_mac} already exists (id: {existing[0]}), returning existing ID")
                            return existing[0]
                    
                    logger.debug(f"Inserting new channel with MAC: {channel_data.get('mac')}")
                    try:
                        cursor.execute('''
                            INSERT INTO channels (name, status, model, src_language, target_language,
                            color, background_color, team_color, car, driver, person, tag, mac,
                            audio_stream_enabled, threshold, silence, min_rec, max_rec, audio_gain,
                            frequency, tone, type, deleted, audio_stream_port, speaker_enabled, speaker_volume)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            channel_data.get('name'),
                            channel_data.get('status'),
                            channel_data.get('model'),
                            channel_data.get('src_language'),
                            channel_data.get('target_language'),
                            channel_data.get('color'),
                            channel_data.get('background_color'),
                            channel_data.get('team_color'),
                            channel_data.get('car'),
                            channel_data.get('driver'),
                            channel_data.get('person'),
                            channel_data.get('tag'),
                            channel_data.get('mac'),
                            channel_data.get('audio_stream_enabled', 0),
                            channel_data.get('threshold'),
                            channel_data.get('silence'),
                            channel_data.get('min_rec'),
                            channel_data.get('max_rec'),
                            channel_data.get('audio_gain'),
                            channel_data.get('frequency'),
                            channel_data.get('tone'),
                            channel_data.get('type'),
                            channel_data.get('deleted', 0),
                            channel_data.get('audio_stream_port'),
                            channel_data.get('speaker_enabled', 0),
                            channel_data.get('speaker_volume')
                        ))
                        lastrowid = cursor.lastrowid
                        default_group = cursor.execute(
                            'SELECT id FROM groups WHERE is_default=1 ORDER BY id LIMIT 1'
                        ).fetchone()
                        if not default_group:
                            raise RuntimeError('Default authorization group is not configured')
                        cursor.execute(
                            """INSERT INTO channel_owners
                               (channel_id, owner_type, owner_id)
                               VALUES (?, 'group', ?)""",
                            (lastrowid, str(default_group['id'])),
                        )
                        conn.commit()
                        logger.debug(f"Inserted new channel, lastrowid: {lastrowid}")
                        return lastrowid
                    except Exception as insert_error:
                        # Handle unique constraint violation (race condition)
                        error_str = str(insert_error).lower()
                        if 'unique' in error_str or 'constraint' in error_str:
                            logger.warning(f"Unique constraint violation for MAC {normalized_mac}, checking for existing channel")
                            cursor.execute('SELECT id FROM channels WHERE mac = ? AND deleted = 0', (normalized_mac,))
                            existing = cursor.fetchone()
                            if existing:
                                logger.info(f"Found existing channel with MAC {normalized_mac} (id: {existing[0]})")
                                return existing[0]
                        # Re-raise if it's not a unique constraint error
                        raise
            except Exception as e:
                logger.error(f"Error saving channel: {e}", exc_info=True)
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_channel(self, channel_id: int) -> bool:
        """Delete a channel (soft delete by setting deleted flag)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('UPDATE channels SET deleted = 1 WHERE id = ?', (channel_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting channel {channel_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== CREDENTIAL METHODS ====================

    @staticmethod
    def hash_credential(token: str) -> str:
        """Hash a plaintext credential for lookup and persistence."""
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def issue_credential(self, principal_type: str, principal_id: str,
                         expires_at: Optional[str] = None,
                         token: Optional[str] = None) -> tuple[str, str]:
        """Issue and persist a hashed credential."""
        token = token or secrets.token_urlsafe(32)
        credential_id = str(uuid.uuid4())
        with _db_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """INSERT INTO credentials
                       (id, principal_type, principal_id, token_hash, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (credential_id, principal_type, str(principal_id),
                     self.hash_credential(token), datetime.now(timezone.utc).isoformat(),
                     expires_at),
                )
                conn.commit()
                return token, credential_id
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def inspect_credential(self, token: str):
        """Return a credential and diagnostic result without mutating it."""
        if not token:
            return None, 'missing'
        with _db_lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    'SELECT * FROM credentials WHERE token_hash = ?',
                    (self.hash_credential(token),),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None, 'not_found'
        try:
            expires_at = row['expires_at']
            if expires_at:
                parsed = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed <= datetime.now(timezone.utc):
                    return None, 'expired'
        except (TypeError, ValueError):
            return None, 'expired'
        return dict(row), 'success'

    def get_credential_record(self, token: str) -> Optional[Dict[str, Any]]:
        """Return a credential row regardless of expiry for diagnostics."""
        if not token:
            return None
        with _db_lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    'SELECT * FROM credentials WHERE token_hash=?',
                    (self.hash_credential(token),),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_credential(self, token: str) -> Optional[Dict[str, Any]]:
        """Return a current credential for a plaintext token."""
        credential, _ = self.inspect_credential(token)
        return credential

    def get_principal(self, principal_type: str, principal_id: str) -> Optional[Dict[str, Any]]:
        """Materialize the current authorization principal for a credential."""
        with _db_lock:
            conn = self._get_connection()
            try:
                if principal_type == 'user':
                    row = conn.execute(
                        """SELECT email, name, role, status, access_level, groups
                           FROM users WHERE email=?""",
                        (principal_id,),
                    ).fetchone()
                    if not row:
                        return None
                    principal = dict(row)
                    group_ids = self._json_list(principal.pop('groups', None))
                    permissions = self._group_permissions(conn, group_ids)
                    principal.update({
                        'id': principal['email'],
                        'type': 'user',
                        'groups': group_ids,
                        'permissions': permissions,
                        'owner_ids': None if principal.get('role') == 'admin' else [
                            f"user:{principal['email']}",
                            *(f'group:{group_id}' for group_id in group_ids),
                        ],
                    })
                    return principal

                if principal_type == 'api_key':
                    row = conn.execute(
                        'SELECT * FROM api_keys WHERE id=?', (principal_id,)
                    ).fetchone()
                    if not row:
                        return None
                    principal = dict(row)
                    principal['permissions'] = self._json_list(principal.get('permissions'))
                    owner = principal.get('owner')
                    owner_ids = [owner] if owner else []
                    if owner and owner.startswith('user:'):
                        owner_user = conn.execute(
                            'SELECT role, groups FROM users WHERE email=?',
                            (owner[5:],),
                        ).fetchone()
                        if owner_user and owner_user['role'] == 'admin':
                            owner_ids = None
                        elif owner_user:
                            owner_ids.extend(
                                f'group:{group_id}'
                                for group_id in self._json_list(owner_user['groups'])
                            )
                    principal.update({'type': 'api_key', 'owner_ids': owner_ids})
                    return principal

                if principal_type == 'device':
                    row = conn.execute(
                        'SELECT * FROM channels WHERE id=? AND deleted=0',
                        (principal_id,),
                    ).fetchone()
                    if not row:
                        return None
                    principal = dict(row)
                    principal.update({
                        'type': 'device',
                        'permissions': ['device'],
                        'owner_ids': [f"channel:{principal['id']}"],
                    })
                    return principal
                return None
            finally:
                conn.close()

    @staticmethod
    def _json_list(value) -> list:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(value or '[]')
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @classmethod
    def _group_permissions(cls, conn, group_ids) -> list[str]:
        if not group_ids:
            return []
        placeholders = ','.join('?' for _ in group_ids)
        permissions = set()
        for row in conn.execute(
            f'SELECT permissions FROM groups WHERE id IN ({placeholders})', group_ids
        ).fetchall():
            permissions.update(cls._json_list(row['permissions']))
        return sorted(permissions)

    def delete_credential(self, token: str) -> bool:
        """Delete a credential by its plaintext token."""
        if not token:
            return False
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    'DELETE FROM credentials WHERE token_hash = ?',
                    (self.hash_credential(token),),
                )
                conn.commit()
                return cursor.rowcount > 0
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def delete_expired_credentials(self, now: Optional[str] = None,
                                   principal_type: Optional[str] = None,
                                   principal_id: Optional[str] = None) -> int:
        """Delete expired credentials and return the number removed."""
        now = now or datetime.now(timezone.utc).isoformat()
        where = 'expires_at IS NOT NULL AND expires_at <= ?'
        parameters = [now]
        if principal_type is not None:
            where += ' AND principal_type=?'
            parameters.append(principal_type)
        if principal_id is not None:
            where += ' AND principal_id=?'
            parameters.append(str(principal_id))
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    f'DELETE FROM credentials WHERE {where}',
                    parameters,
                )
                conn.commit()
                return cursor.rowcount
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def count_credentials(self) -> int:
        """Return the number of stored credentials."""
        with _db_lock:
            conn = self._get_connection()
            try:
                return conn.execute('SELECT COUNT(*) FROM credentials').fetchone()[0]
            finally:
                conn.close()

    def has_current_credential(self, principal_type: str, principal_id: str) -> bool:
        """Return whether a principal has at least one unexpired credential."""
        now = datetime.now(timezone.utc).isoformat()
        with _db_lock:
            conn = self._get_connection()
            try:
                return conn.execute(
                    """SELECT 1 FROM credentials
                       WHERE principal_type=? AND principal_id=?
                         AND (expires_at IS NULL OR expires_at > ?)
                       LIMIT 1""",
                    (principal_type, str(principal_id), now),
                ).fetchone() is not None
            finally:
                conn.close()

    # ==================== PAGINATION PREFERENCES METHODS ====================
    
    def get_pagination_prefs(self, email: str) -> Optional[Dict[str, Any]]:
        """Get pagination preferences for a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM pagination_preferences WHERE email = ?', (email,))
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
    
    def save_pagination_prefs(self, email: str, prefs: Dict[str, Any]) -> bool:
        """Save pagination preferences for a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO pagination_preferences 
                    (email, records_per_page, current_page, reverse_sort, show_full_timestamps)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    email,
                    prefs.get('recordsPerPage'),
                    prefs.get('currentPage'),
                    prefs.get('reverseSort', 0),
                    prefs.get('showFullTimestamps', 0)
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving pagination prefs for {email}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== BRANDING METHODS ====================
    
    def get_branding(self) -> Optional[Dict[str, Any]]:
        """Get branding configuration."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM branding ORDER BY id DESC LIMIT 1')
                row = cursor.fetchone()
                if row:
                    branding = dict(row)
                    if branding.get('brand_colors'):
                        try:
                            branding['brand_colors'] = json.loads(branding['brand_colors'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            branding['brand_colors'] = {}
                    if branding.get('assets'):
                        try:
                            branding['assets'] = json.loads(branding['assets'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            branding['assets'] = {}
                    return branding
                return None
            finally:
                conn.close()
    
    def save_branding(self, branding_data: Dict[str, Any]) -> bool:
        """Save branding configuration (upsert — always keeps a single row)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                brand_colors = json.dumps(branding_data.get('brand_colors', {}))
                assets = json.dumps(branding_data.get('assets', {}))

                cursor.execute('SELECT id FROM branding ORDER BY id DESC LIMIT 1')
                existing = cursor.fetchone()
                if existing:
                    cursor.execute('''
                        UPDATE branding
                        SET organization_name=?, tagline=?, brand_colors=?, font=?, assets=?
                        WHERE id=?
                    ''', (
                        branding_data.get('organization_name'),
                        branding_data.get('tagline'),
                        brand_colors,
                        branding_data.get('font'),
                        assets,
                        existing['id']
                    ))
                else:
                    cursor.execute('''
                        INSERT INTO branding (organization_name, tagline, brand_colors, font, assets)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        branding_data.get('organization_name'),
                        branding_data.get('tagline'),
                        brand_colors,
                        branding_data.get('font'),
                        assets
                    ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving branding: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== HALLUCINATIONS METHODS ====================
    
    def get_all_hallucinations(self) -> List[Dict[str, Any]]:
        """Get all hallucinations."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM hallucinations ORDER BY id')
                results = []
                for row in cursor.fetchall():
                    data = dict(row)
                    if data.get('data'):
                        try:
                            data['data'] = json.loads(data['data'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            pass
                    results.append(data)
                return results
            finally:
                conn.close()
    
    def delete_hallucination(self, hallucination_id: int) -> bool:
        """Delete a hallucination by ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM hallucinations WHERE id = ?', (hallucination_id,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error deleting hallucination {hallucination_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def save_hallucination(self, data: Any) -> int:
        """Save a hallucination entry."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                data_str = json.dumps(data) if not isinstance(data, str) else data
                cursor.execute('INSERT INTO hallucinations (data) VALUES (?)', (data_str,))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving hallucination: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    # ==================== BACKUP HISTORY METHODS ====================
    
    def get_all_backup_history(self) -> List[Dict[str, Any]]:
        """Get all backup history entries."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM backup_history ORDER BY start_time DESC')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_backup_history(self, backup_data: Dict[str, Any]) -> int:
        """Save a backup history entry."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO backup_history 
                    (start_time, end_time, duration, status, manual, backup_type,
                     destination, uploaded_files, skipped_files, error_files, total_files)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    backup_data.get('start_time'),
                    backup_data.get('end_time'),
                    backup_data.get('duration'),
                    backup_data.get('status'),
                    backup_data.get('manual', 0),
                    backup_data.get('backup_type'),
                    backup_data.get('destination'),
                    backup_data.get('uploaded_files', 0),
                    backup_data.get('skipped_files', 0),
                    backup_data.get('error_files', 0),
                    backup_data.get('total_files', 0)
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving backup history: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    # ==================== REBOOT HISTORY METHODS ====================
    
    def get_reboot_history(self, mac_address: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get reboot history, optionally filtered by MAC address."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if mac_address:
                    cursor.execute(
                        'SELECT * FROM reboot_history WHERE mac_address = ? ORDER BY timestamp DESC',
                        (mac_address,)
                    )
                else:
                    cursor.execute('SELECT * FROM reboot_history ORDER BY timestamp DESC')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_reboot_history(self, mac_address: str, timestamp: str, port: str) -> int:
        """Save a reboot history entry."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO reboot_history (mac_address, timestamp, port)
                    VALUES (?, ?, ?)
                ''', (mac_address, timestamp, port))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving reboot history: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    # ==================== SCANNER INVENTORY METHODS ====================
    
    def get_all_scanners(self) -> Dict[str, Dict[str, Any]]:
        """Get all scanners as a dictionary keyed by scanner_id."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM scanner_inventory')
                scanners = {}
                for row in cursor.fetchall():
                    scanners[row['scanner_id']] = dict(row)
                return scanners
            finally:
                conn.close()
    
    def save_scanner(self, scanner_id: str, scanner_data: Dict[str, Any]) -> bool:
        """Save or update a scanner."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO scanner_inventory 
                    (scanner_id, port, model, version, status)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    scanner_id,
                    scanner_data.get('port'),
                    scanner_data.get('model'),
                    scanner_data.get('version'),
                    scanner_data.get('status')
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving scanner {scanner_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_scanner(self, scanner_id: str) -> bool:
        """Delete a scanner."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM scanner_inventory WHERE scanner_id = ?', (scanner_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting scanner {scanner_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== RECORDERS INVENTORY METHODS ====================
    
    def get_all_recorders(self) -> List[Dict[str, Any]]:
        """Get all recorders."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM recorders_inventory ORDER BY id')
                results = []
                for row in cursor.fetchall():
                    data = dict(row)
                    if data.get('data'):
                        try:
                            data['data'] = json.loads(data['data'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            pass
                    results.append(data)
                return results
            finally:
                conn.close()
    
    def save_recorder(self, recorder_data: Any) -> int:
        """Save a recorder entry. Updates if a recorder with the same port exists, otherwise inserts."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                data_str = json.dumps(recorder_data) if not isinstance(recorder_data, str) else recorder_data
                
                # Check if recorder with same port already exists
                port = recorder_data.get('port') if isinstance(recorder_data, dict) else None
                if port:
                    # Search for existing recorder with same port
                    cursor.execute('SELECT id, data FROM recorders_inventory')
                    for row in cursor.fetchall():
                        try:
                            existing_data = json.loads(row['data']) if isinstance(row['data'], str) else row['data']
                            if isinstance(existing_data, dict) and existing_data.get('port') == port:
                                # Update existing record
                                cursor.execute('UPDATE recorders_inventory SET data = ? WHERE id = ?', (data_str, row['id']))
                                conn.commit()
                                return row['id']
                        except (json.JSONDecodeError, TypeError):
                            continue
                
                # No existing recorder found, insert new one
                cursor.execute('INSERT INTO recorders_inventory (data) VALUES (?)', (data_str,))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving recorder: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_recorder(self, port: str) -> bool:
        """Delete a recorder entry by port."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                # Find and delete recorder with matching port
                cursor.execute('SELECT id, data FROM recorders_inventory')
                deleted = False
                for row in cursor.fetchall():
                    try:
                        data = json.loads(row['data']) if isinstance(row['data'], str) else row['data']
                        if isinstance(data, dict) and data.get('port') == port:
                            cursor.execute('DELETE FROM recorders_inventory WHERE id = ?', (row['id'],))
                            deleted = True
                            break
                    except (json.JSONDecodeError, TypeError):
                        continue
                
                if deleted:
                    conn.commit()
                    logger.info(f"Deleted recorder with port: {port}")
                else:
                    logger.warning(f"No recorder found with port: {port}")
                
                return deleted
            except Exception as e:
                logger.error(f"Error deleting recorder: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== FIRMWARE METADATA METHODS ====================
    # Firmware metadata is stored in firmware/firmware.json (not in the database).

    def _get_firmware_json_path(self) -> str:
        """Return the path to firmware/firmware.json."""
        return DATA_ROOT / 'firmware' / 'firmware.json'

    def get_firmware_metadata(self) -> Dict[str, Any]:
        """Get firmware metadata from firmware/firmware.json."""
        firmware_path = self._get_firmware_json_path()
        try:
            if not firmware_path.exists():
                return {}
            with open(firmware_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading firmware metadata from {firmware_path}: {e}")
            return {}

    def save_firmware_metadata(self, metadata: Dict[str, Any]) -> bool:
        """Save firmware metadata to firmware/firmware.json."""
        firmware_path = self._get_firmware_json_path()
        try:
            firmware_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = firmware_path.with_name(firmware_path.name + ".tmp")
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            tmp_path.replace(firmware_path)
            return True
        except Exception as e:
            logger.error(f"Error saving firmware metadata to {firmware_path}: {e}")
            return False
    
    # ==================== QUEUE METHODS ====================
    
    def get_all_queue_items(self) -> List[Dict[str, Any]]:
        """Get all queue items."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM queue ORDER BY id')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_queue_item(self, queue_data: Dict[str, Any]) -> int:
        """Save a queue item."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO queue (mac, relative_path, channel_id, timestamp, error, attempt_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    queue_data.get('mac'),
                    queue_data.get('relative_path'),
                    queue_data.get('channel_id'),
                    queue_data.get('timestamp'),
                    queue_data.get('error'),
                    queue_data.get('attempt_time')
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving queue item: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_queue_item(self, item_id: int) -> bool:
        """Delete a queue item."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM queue WHERE id = ?', (item_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting queue item {item_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

# Global instance getter
def get_settings_manager() -> SettingsManager:
    """Get the global SettingsManager instance."""
    return SettingsManager()
