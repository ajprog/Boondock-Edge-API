"""Add channel delivery tracking to an existing installation."""

from app.utils.sqlite_utils import connect_sqlite


def upgrade_channel_settings_schema(settings_path):
    """Add the synced flag without changing existing channel settings."""
    db = connect_sqlite(settings_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(channels)')}
        if not columns:
            raise RuntimeError('Cannot upgrade channel settings: channels table is missing')
        if 'synced' not in columns:
            db.execute('ALTER TABLE channels ADD COLUMN synced BOOLEAN NOT NULL DEFAULT FALSE')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == '__main__':
    from config import Config

    upgrade_channel_settings_schema(Config.get_settings_db_path())
