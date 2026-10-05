"""API-key principal creation, listing, and deletion."""
import json
import secrets
import uuid
from datetime import datetime, timezone

from config import Config
from app.utils.sqlite_utils import connect_sqlite
from .settings_manager import get_settings_manager

KEY_PREFIX = 'bk_live_'
DEFAULT_SCOPES = ['transcriptions.read']


class APIKeyManager:
    """Manage API-key principals; credential validation is handled centrally."""
    def __init__(self, db_path=None):
        self.db_path = db_path or Config.get_settings_db_path()
        self.settings = get_settings_manager()

    def _connect(self):
        return connect_sqlite(self.db_path, row_factory=True)

    def create_key(self, name, scopes=None, created_by=None, expires_at=None, owner=None):
        raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
        permissions = scopes or list(DEFAULT_SCOPES)
        created_at = datetime.now(timezone.utc).isoformat()
        owner = owner or (f'user:{created_by}' if created_by else self._default_owner())
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO api_keys
                   (name, permissions, owner, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, json.dumps(permissions), owner, created_at, created_by),
            )
            key_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO credentials
                   (id, principal_type, principal_id, token_hash, created_at, expires_at)
                   VALUES (?, 'api_key', ?, ?, ?, ?)""",
                (str(uuid.uuid4()), key_id, self.settings.hash_credential(raw_key),
                 created_at, expires_at),
            )
        return self.get_key(key_id), raw_key

    def _default_owner(self):
        with self._connect() as connection:
            row = connection.execute(
                'SELECT id FROM groups WHERE is_default=1 ORDER BY id LIMIT 1'
            ).fetchone()
        return f"group:{row['id']}" if row else 'group:1'

    def list_keys(self):
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT k.*, c.expires_at FROM api_keys k
                   LEFT JOIN credentials c ON c.principal_type='api_key'
                     AND c.principal_id=k.id ORDER BY k.created_at DESC"""
            ).fetchall()
        return [self._metadata(row) for row in rows]

    def get_key(self, key_id):
        with self._connect() as connection:
            row = connection.execute(
                """SELECT k.*, c.expires_at FROM api_keys k
                   LEFT JOIN credentials c ON c.principal_type='api_key'
                     AND c.principal_id=k.id WHERE k.id=?""", (key_id,),
            ).fetchone()
        return self._metadata(row) if row else None

    def revoke_key(self, key_id):
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM credentials WHERE principal_type='api_key' AND principal_id=?",
                (key_id,),
            )
            cursor = connection.execute('DELETE FROM api_keys WHERE id=?', (key_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _metadata(row):
        permissions = json.loads(row['permissions'] or '[]')
        return {
            'id': row['id'], 'name': row['name'], 'permissions': permissions,
            'scopes': permissions, 'owner': row['owner'],
            'created_at': row['created_at'], 'created_by': row['created_by'],
            'expires_at': row['expires_at'], 'revoked': False,
        }


_manager = None


def get_api_key_manager():
    global _manager
    if _manager is None:
        _manager = APIKeyManager()
    return _manager
