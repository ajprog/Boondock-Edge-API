"""
Comprehensive database initialization utility.
Ensures all required JSON files are created with proper defaults if they don't exist.

This module should be called at application startup to guarantee all JSON files are present.
"""

import os
import json
import logging
from datetime import datetime

# Get the directory where this script is located
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.abspath(os.path.join(_SCRIPT_DIR, '..', '..'))
_DB_DIR = os.path.join(_BASE_DIR, 'db')
_RECORDINGS_DIR = os.path.join(_BASE_DIR, 'recordings')

# Ensure db directory exists
os.makedirs(_DB_DIR, exist_ok=True)

logger = logging.getLogger(__name__)


# File paths
SETTINGS_JSON = os.path.join(_DB_DIR, 'settings.json')
CHANNELS_JSON = os.path.join(_DB_DIR, 'channels.json')
USERS_JSON = os.path.join(_DB_DIR, 'users.json')
PROFILES_JSON = os.path.join(_DB_DIR, 'profiles.json')
TAGS_JSON = os.path.join(_DB_DIR, 'tags.json')
FREQUENCIES_JSON = os.path.join(_DB_DIR, 'frequencies.json')
HALLUCINATIONS_JSON = os.path.join(_DB_DIR, 'hallucinations.json')
BRANDING_JSON = os.path.join(_DB_DIR, 'branding.json')
TOKENS_JSON = os.path.join(_DB_DIR, 'tokens.json')
PAGINATION_PREFS_JSON = os.path.join(_DB_DIR, 'pagination_preferences.json')
BACKUP_HISTORY_JSON = os.path.join(_DB_DIR, 'backup_history.json')
REBOOT_HISTORY_JSON = os.path.join(_DB_DIR, 'reboot_history.json')
SCANNER_INVENTORY_JSON = os.path.join(_DB_DIR, 'scanner_inventory.json')
RECORDERS_INVENTORY_JSON = os.path.join(_DB_DIR, 'recorders_inventory.json')
FIRMWARE_METADATA_JSON = os.path.join(_DB_DIR, 'firmware_metadata.json')
QUEUE_JSON = os.path.join(_DB_DIR, 'queue.json')
CURRENT_QUEUE_JSON = os.path.join(_DB_DIR, 'current_queue.json')
QUEUE_HISTORY_JSON = os.path.join(_DB_DIR, 'queue_history.json')

# Default file contents
DEFAULT_SETTINGS = {
    "event_name": "default",
    "model": "base.en",
    "target_language": "english",
    "global_transcribe_local": True,
    "global_transcribe_openai": False,
    "global_hallucination": True,
    "keywords": [],
    "global_min_record_secs": 1,
    "global_max_record_secs": 30,
    "global_silence_secs": 1,
    "global_pre_record_ms": 200,
    "global_post_record_ms": 1000,
    "global_rms_threshold": 30,
    "global_discard_secs": 2
}

DEFAULT_PROFILES = {
    "Default": {
        "name": "Default",
        "description": "Default member profile",
        "features": {
            "access_settings": False,
            "inbox": True,
            "create_reports": False,
            "view_reports": True,
            "modify_reports": False,
            "play_audio": True,
            "delete_audio": False,
            "access_advanced_player": False
        }
    },
    "Admin": {
        "name": "Admin",
        "description": "Full administrator access",
        "features": {
            "access_settings": True,
            "inbox": True,
            "create_reports": True,
            "view_reports": True,
            "modify_reports": True,
            "play_audio": True,
            "delete_audio": True,
            "access_advanced_player": True
        }
    },
    "Analyst": {
        "name": "Analyst",
        "description": "Report creation and analysis",
        "features": {
            "access_settings": False,
            "inbox": True,
            "create_reports": True,
            "view_reports": True,
            "modify_reports": True,
            "play_audio": True,
            "delete_audio": False,
            "access_advanced_player": True
        }
    }
}

DEFAULT_USERS = {
    "admin@boondock.cloud": {
        "name": "Administrator",
        "password": "hashed_password_here",
        "role": "admin",
        "profile": "Admin",
        "mfa_enabled": False,
        "created_at": datetime.utcnow().isoformat() + 'Z'
    }
}

DEFAULT_BRANDING = {
    "organization_name": "Boondock Edge",
    "tagline": "Emergency Services Communication Platform",
    "brand_colors": {
        "primary": "#2563eb",
        "secondary": "#4f46e5",
        "accent": "#ec4899"
    },
    "font": "inter",
    "assets": {
        "logo": None,
        "favicon": None,
        "loader": None
    }
}


def _create_json_file(file_path, default_content, file_name):
    """Helper function to create a JSON file with default content."""
    try:
        if not os.path.exists(file_path):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(default_content, f, indent=4)
            logger.info(f"✓ Created {file_name}")
        else:
            logger.debug(f"→ {file_name} already exists")
    except Exception as e:
        logger.error(f"✗ Failed to create {file_name}: {e}")
        raise


def initialize_all_json_files():
    """
    Initialize all required JSON configuration files.
    Creates files with default content if they don't exist.
    
    Raises:
        Exception: If any critical file creation fails
    """
    logger.info("=" * 60)
    logger.info("Initializing Database Configuration Files")
    logger.info("=" * 60)
    
    try:
        # CRITICAL FILES - must exist for app to function
        logger.info("\n[CRITICAL FILES]")
        _create_json_file(SETTINGS_JSON, DEFAULT_SETTINGS, "settings.json")
        _create_json_file(USERS_JSON, DEFAULT_USERS, "users.json")
        _create_json_file(PROFILES_JSON, DEFAULT_PROFILES, "profiles.json")
        
        # IMPORTANT FILES - required for basic operation
        logger.info("\n[IMPORTANT FILES]")
        _create_json_file(TAGS_JSON, [], "tags.json")
        _create_json_file(FREQUENCIES_JSON, [], "frequencies.json")
        
        # OPTIONAL FILES - created on-demand but better to pre-create
        logger.info("\n[OPTIONAL FILES]")
        _create_json_file(HALLUCINATIONS_JSON, [], "hallucinations.json")
        _create_json_file(BRANDING_JSON, DEFAULT_BRANDING, "branding.json")
        _create_json_file(TOKENS_JSON, {}, "tokens.json")
        _create_json_file(PAGINATION_PREFS_JSON, {}, "pagination_preferences.json")
        _create_json_file(BACKUP_HISTORY_JSON, {"backups": []}, "backup_history.json")
        _create_json_file(REBOOT_HISTORY_JSON, {"reboots": []}, "reboot_history.json")
        # NOTE: scanner_inventory.json, recorders_inventory.json, and firmware_metadata.json
        # are now stored in settings.db - do not create these JSON files
        _create_json_file(QUEUE_JSON, {"tasks": []}, "queue.json")
        _create_json_file(CURRENT_QUEUE_JSON, {"last_updated": None, "tasks": {}, "queue_size": 0, "total_tasks": 0}, "current_queue.json")
        _create_json_file(QUEUE_HISTORY_JSON, {"last_updated": None, "entries": []}, "queue_history.json")
        
        # SPECIAL CASE: channels.json (requires manual setup)
        logger.info("\n[SPECIAL FILES - MANUAL SETUP REQUIRED]")
        if not os.path.exists(CHANNELS_JSON):
            logger.warning("⚠ channels.json MISSING - This must be manually created!")
            logger.warning("  → channels.json requires MAC addresses and channel definitions")
            logger.warning("  → See DB_FILES_REQUIREMENTS.md for template")
            # Create a template if it doesn't exist
            template_channels = [
                {
                    "id": 1,
                    "name": "Channel 1",
                    "mac": "AABBCCDDEEFF",
                    "frequency": 154.2800,
                    "isActive": True,
                    "threshold": "50",
                    "silence": "1600"
                }
            ]
            _create_json_file(CHANNELS_JSON, template_channels, "channels.json (TEMPLATE)")
        else:
            logger.info("✓ channels.json found")
        
        # Create directory structure
        logger.info("\n[DIRECTORIES]")
        directories = [
            os.path.join(_BASE_DIR, 'firmware'),
            os.path.join(_DB_DIR, 'recorder_configs'),
            os.path.join(_DB_DIR, 'reports'),
            _RECORDINGS_DIR
        ]
        
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
            rel_path = os.path.relpath(directory, _DB_DIR)
            logger.info(f"✓ {rel_path}/")
        
        logger.info("\n" + "=" * 60)
        logger.info("✓ Database initialization complete!")
        logger.info("=" * 60)
        
        return True
        
    except Exception as e:
        logger.error(f"\n✗ Database initialization FAILED: {e}")
        raise


def verify_json_files():
    """
    Verify that all required JSON files exist and are valid JSON.
    
    Returns:
        dict: Status report of all files
    """
    files_to_check = {
        "settings.json": SETTINGS_JSON,
        "channels.json": CHANNELS_JSON,
        "users.json": USERS_JSON,
        "profiles.json": PROFILES_JSON,
        "tags.json": TAGS_JSON,
        "frequencies.json": FREQUENCIES_JSON,
        "branding.json": BRANDING_JSON,
        "scanner_inventory.json": SCANNER_INVENTORY_JSON,
        "recorders_inventory.json": RECORDERS_INVENTORY_JSON,
    }
    
    status = {}
    
    for file_name, file_path in files_to_check.items():
        exists = os.path.exists(file_path)
        is_valid = False
        
        if exists:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    json.load(f)
                is_valid = True
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Invalid JSON in {file_name}: {e}")
        
        status[file_name] = {
            "exists": exists,
            "valid": is_valid,
            "path": file_path
        }
    
    return status


if __name__ == "__main__":
    # Setup logging for standalone execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Run initialization
    initialize_all_json_files()
    
    # Verify
    print("\n" + "=" * 60)
    print("VERIFICATION REPORT")
    print("=" * 60)
    status = verify_json_files()
    for file_name, file_status in status.items():
        status_str = "✓" if file_status["valid"] else "✗" if not file_status["exists"] else "⚠"
        print(f"{status_str} {file_name:30} | Exists: {file_status['exists']:5} | Valid: {file_status['valid']:5}")
