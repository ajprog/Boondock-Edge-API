"""
Database initialization utility for settings.db.
Creates the database with default values if it doesn't exist.
"""

import json
import logging
import sqlite3
import threading
from config import Config, DATA_ROOT
from datetime import datetime, timezone
from app.utils.sqlite_utils import connect_sqlite
from .settings_manager import SettingsManager

logger = logging.getLogger(__name__)

# Module-level flag to track initialization status
_initialization_checked = False
_initialization_lock = threading.Lock()

def _create_database_schema():
    """Create the complete settings database schema."""
    conn = connect_sqlite(Config.get_settings_db_path(), row_factory=True)
    try:
        cursor = conn.cursor()
        is_new_database = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone() is None

        # Settings table (key-value pairs)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('bool', 'string', 'json', 'datetime', 'int', 'float'))
            )
        ''')

        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                password TEXT NOT NULL,
                role TEXT,
                mfa_enabled BOOLEAN DEFAULT FALSE,
                mfa_enforced BOOLEAN DEFAULT FALSE,
                mfa_secret TEXT,
                created_at TEXT,
                groups JSON NOT NULL DEFAULT '[]',
                preferences JSON NOT NULL DEFAULT '{}'
            )
        ''')

        # Tags table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT,
                usage_count INTEGER DEFAULT 0,
                color TEXT,
                created_at TEXT
            )
        ''')

        # Frequencies table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS frequencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                frequency REAL,
                type TEXT,
                tone TEXT,
                tag TEXT,
                person TEXT,
                status TEXT
            )
        ''')

        # Channels table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                status TEXT,
                model TEXT,
                src_language TEXT,
                target_language TEXT,
                color TEXT,
                background_color TEXT,
                team_color TEXT,
                car TEXT,
                driver TEXT,
                person TEXT,
                tag TEXT,
                mac TEXT UNIQUE NOT NULL,
                audio_stream_enabled BOOLEAN DEFAULT FALSE,
                threshold TEXT,
                silence TEXT,
                min_rec TEXT,
                max_rec TEXT,
                audio_gain TEXT,
                frequency REAL,
                tone TEXT,
                type TEXT,
                deleted BOOLEAN DEFAULT FALSE,
                audio_stream_port INTEGER,
                speaker_enabled BOOLEAN DEFAULT FALSE,
                speaker_volume INTEGER,
                device_hostname TEXT,
                device_ip TEXT
            )
        ''')

        # Try to add UNIQUE constraint if table already exists without it
        try:
            cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_mac_unique ON channels(mac)')
        except sqlite3.OperationalError:
            # Index might already exist or table might not exist yet
            pass

        # Authorization schema
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                permissions JSON NOT NULL DEFAULT '[]',
                default_preferences JSON NOT NULL DEFAULT
                    '{"display":{},"inbox":{},"reports":{}}',
                keywords JSON NOT NULL DEFAULT '[]'
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                match_type TEXT NOT NULL DEFAULT 'literal'
                    CHECK (match_type IN ('literal', 'regex')),
                case_sensitive BOOLEAN NOT NULL DEFAULT FALSE
            )
        ''')
        cursor.execute('''
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
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_credentials_principal ON credentials(principal_type, principal_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_credentials_expires_at ON credentials(expires_at)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                permissions JSON NOT NULL,
                owner TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channel_owners (
                channel_id INTEGER NOT NULL,
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                PRIMARY KEY (channel_id, owner_type, owner_id),
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        ''')
        if is_new_database:
            cursor.execute(
                '''INSERT INTO schema_migrations(name, applied_at)
                   VALUES ('2026_09_authorization_phase_1_complete', ?)''',
                (datetime.now(timezone.utc).isoformat(),)
            )

        # Branding table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS branding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_name TEXT,
                tagline TEXT,
                brand_colors JSON,
                font TEXT,
                assets JSON
            )
        ''')

        # Hallucinations table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hallucinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                match_type TEXT NOT NULL DEFAULT 'literal'
                    CHECK (match_type IN ('literal', 'regex')),
                case_sensitive BOOLEAN NOT NULL DEFAULT FALSE
            )
        ''')

        # Backup history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS backup_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT,
                end_time TEXT,
                duration INTEGER,
                status TEXT,
                manual BOOLEAN DEFAULT FALSE,
                backup_type TEXT,
                destination TEXT,
                uploaded_files INTEGER DEFAULT 0,
                skipped_files INTEGER DEFAULT 0,
                error_files INTEGER DEFAULT 0,
                total_files INTEGER DEFAULT 0
            )
        ''')

        # Reboot history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reboot_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address TEXT,
                timestamp TEXT,
                port TEXT
            )
        ''')

        # Scanner inventory table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scanner_inventory (
                scanner_id TEXT PRIMARY KEY,
                port TEXT,
                model TEXT,
                version TEXT,
                status TEXT
            )
        ''')

        # Recorders inventory table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recorders_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data JSON
            )
        ''')

        # Firmware metadata is now stored in firmware/firmware.json (not in database)

        # Queue table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT,
                relative_path TEXT,
                channel_id INTEGER,
                timestamp TEXT,
                error TEXT,
                attempt_time TEXT
            )
        ''')

        conn.commit()
        logger.info("Database schema initialized successfully")

    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

# Default settings rows that must always exist.
DEFAULT_SETTINGS = {
    "event_name": "default",
    "model": "tiny.en",
    "target_language": "english",
    "method": "local",
    "uniden_scanners_enabled": False,
    "edge_recorders_enabled": True,
    "transcription_queue_enabled": True,
    "transcription_endpoint": "https://api.boondock.cloud",
    "api_health_url": "https://api.boondock.cloud/health",
    "global_min_record_secs": 1,
    "global_max_record_secs": 30,
    "global_silence_secs": 1,
    "global_pre_record_ms": 200,
    "global_post_record_ms": 1000,
    "global_rms_threshold": 30,
    "global_discard_secs": 1,
    "s3_enabled": False,
    "s3_endpoint_url": "",
    "s3_access_key": "",
    "s3_secret_key": "",
    "s3_region": "us-east-1",
    "s3_bucket_name": "",
    "ssid": "boondockedge",
    "host_password": "edge@123",
    "host_ip": "10.42.0.1",
    "host_port": "4000",
    "button_enabled": False,
    "button_long_press_duration": 3.0,
    "relay_enabled": False,
    "usb_power_enabled": False,
    "samba_enabled": False,
    "samba_share_path": "",
    "samba_username": "",
    "samba_password": "",
    "scheduled_time": "03:00",
    "enabled_tasks": ["data_backup", "logs_cleanup", "health_checks"],
}

def _ensure_default_settings_rows(settings_manager, existing_settings=None):
    """
    Ensure all required settings rows exist.
    Missing keys are inserted; existing values are never overwritten.
    """
    if existing_settings is None:
        existing_settings = settings_manager.get_all_settings() or {}

    inserted = 0
    for key, default_value in DEFAULT_SETTINGS.items():
        if key not in existing_settings or existing_settings[key] is None:
            settings_manager.set_setting(key, default_value)
            inserted += 1
            logger.info(f"Initialized missing setting row: {key} = {default_value}")
    return inserted

def initialize_settings_database():
    """
    Initialize settings.db with default values.
    This should be called at application startup.
    Idempotent - safe to call multiple times.
    """
    global _initialization_checked

    # Quick check without lock for performance
    if _initialization_checked:
        return True

    # Thread-safe check and initialization
    with _initialization_lock:
        # Double-check pattern
        if _initialization_checked:
            return True

        _create_database_schema()
        settings_manager = SettingsManager()

        # Check if database already has data BEFORE logging
        try:
            existing_settings = settings_manager.get_all_settings()
            if existing_settings and len(existing_settings) > 0:
                # Ensure all required settings rows exist for upgraded installations.
                _ensure_default_settings_rows(settings_manager, existing_settings)
                _initialization_checked = True
                logger.debug("Database already initialized with data. Skipping defaults.")
                return True
        except Exception as e:
            logger.warning(f"Error checking database initialization status: {e}")
            # Continue with initialization if check fails

        # Only log if we're actually initializing
        logger.info("=" * 60)
        logger.info("Initializing Settings Database")
        logger.info("=" * 60)

    # Default authorization group
    DEFAULT_GROUP = {
        "name": "Default",
        "description": "Default user group",
        "is_default": True,
        "permissions": [
            "reports.read",
            "recording.read",
            "channel.read",
            "tag.read",
            "tag.write",
            "transcription.read",
        ],
    }

    # Default firmware metadata
    FIRMWARE_STORAGE_DIR = DATA_ROOT / 'firmware'
    DEFAULT_FIRMWARE_ID = "default"
    DEFAULT_FIRMWARE_DIR = FIRMWARE_STORAGE_DIR / DEFAULT_FIRMWARE_ID
    DEFAULT_FIRMWARE = {
        DEFAULT_FIRMWARE_ID: {
            "name": "Default Firmware",
            "version": "1.0.0",
            "description": "Default firmware included with the release",
            "folder": DEFAULT_FIRMWARE_ID,
            "created_at": datetime.utcnow().isoformat() + 'Z'
        }
    }

    try:
        # Initialize default data - setup wizard is disabled, so always populate defaults

        # Initialize settings
        logger.info("Setting default settings...")
        settings_manager.set_all_settings(DEFAULT_SETTINGS)
        # Double-check and insert any missing rows individually (future-safe on migrations)
        _ensure_default_settings_rows(settings_manager)

        # Initialize the default authorization group.
        logger.info("Setting default authorization group...")
        settings_manager.save_group(DEFAULT_GROUP)

        # Initialize default firmware metadata (JSON file)
        logger.info("Setting default firmware metadata...")
        firmware_json_path = FIRMWARE_STORAGE_DIR / 'firmware.json'

        # Load existing firmware metadata from JSON file
        existing_firmware = {}
        if firmware_json_path.exists():
            try:
                with open(firmware_json_path, 'r', encoding='utf-8') as f:
                    existing_firmware = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load existing firmware.json: {e}")

        # Only add default firmware if it doesn't exist and the files are present
        if DEFAULT_FIRMWARE_ID not in existing_firmware:
            # Check if default firmware files exist
            bootloader_path = DEFAULT_FIRMWARE_DIR / 'bootloader.bin'
            partitions_path = DEFAULT_FIRMWARE_DIR / 'partitions.bin'
            firmware_path = DEFAULT_FIRMWARE_DIR / 'firmware.bin'

            if all(p.exists() for p in [bootloader_path, partitions_path, firmware_path]):
                # Merge with existing firmware metadata (don't overwrite other firmware entries)
                merged_firmware = existing_firmware.copy()
                merged_firmware.update(DEFAULT_FIRMWARE)

                # Ensure firmware directory exists
                FIRMWARE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

                # Save to JSON file
                try:
                    with open(firmware_json_path, 'w', encoding='utf-8') as f:
                        json.dump(merged_firmware, f, indent=2, ensure_ascii=False)
                    logger.info("Default firmware metadata added to firmware.json (files found).")
                except Exception as e:
                    logger.error(f"Failed to save firmware.json: {e}")
            else:
                logger.warning("Default firmware files not found. Skipping default firmware entry.")
                logger.debug(f"Expected files: {bootloader_path}, {partitions_path}, {firmware_path}")
        else:
            logger.info("Default firmware already exists in firmware.json.")

        logger.info("=" * 60)
        logger.info("✓ Database initialization complete!")
        logger.info("=" * 60)

        # Mark as initialized only on success
        _initialization_checked = True
        return True

    except Exception as e:
        logger.error(f"\n✗ Database initialization FAILED: {e}")
        raise

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    initialize_settings_database()
