"""
Migration utility to migrate JSON files to SQLite settings.db database.
"""

import os
import json
import logging
from pathlib import Path
from .settings_manager import SettingsManager

logger = logging.getLogger(__name__)

# Get the database directory
# Use absolute path resolution like route_utils.py does
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_DB_DIR = os.path.join(_BASE_DIR, 'db')


def migrate_json_to_sqlite():
    """
    Migrate all JSON configuration files to SQLite database.
    This function should be called once during migration.
    """
    logger.info("=" * 60)
    logger.info("Starting JSON to SQLite Migration")
    logger.info("=" * 60)
    
    settings_manager = SettingsManager()
    migrated_count = 0
    error_count = 0
    
    # File paths
    json_files = {
        'settings.json': os.path.join(_DB_DIR, 'settings.json'),
        'users.json': os.path.join(_DB_DIR, 'users.json'),
        'profiles.json': os.path.join(_DB_DIR, 'profiles.json'),
        'tags.json': os.path.join(_DB_DIR, 'tags.json'),
        'frequencies.json': os.path.join(_DB_DIR, 'frequencies.json'),
        'channels.json': os.path.join(_DB_DIR, 'channels.json'),
        'tokens.json': os.path.join(_DB_DIR, 'tokens.json'),
        'branding.json': os.path.join(_DB_DIR, 'branding.json'),
        'hallucinations.json': os.path.join(_DB_DIR, 'hallucinations.json'),
        'backup_history.json': os.path.join(_DB_DIR, 'backup_history.json'),
        'reboot_history.json': os.path.join(_DB_DIR, 'reboot_history.json'),
        'scanner_inventory.json': os.path.join(_DB_DIR, 'scanner_inventory.json'),
        'recorders_inventory.json': os.path.join(_DB_DIR, 'recorders_inventory.json'),
        'firmware_metadata.json': os.path.join(_DB_DIR, 'firmware_metadata.json'),
        'queue.json': os.path.join(_DB_DIR, 'queue.json'),
    }
    
    # Migrate settings.json
    if os.path.exists(json_files['settings.json']):
        try:
            with open(json_files['settings.json'], 'r', encoding='utf-8') as f:
                settings_data = json.load(f)
            settings_manager.set_all_settings(settings_data)
            logger.info("✓ Migrated settings.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating settings.json: {e}")
            error_count += 1
    
    # Migrate users.json
    if os.path.exists(json_files['users.json']):
        try:
            with open(json_files['users.json'], 'r', encoding='utf-8') as f:
                users_data = json.load(f)
            for email, user_data in users_data.items():
                settings_manager.save_user(email, user_data)
            logger.info("✓ Migrated users.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating users.json: {e}")
            error_count += 1
    
    # Migrate profiles.json
    if os.path.exists(json_files['profiles.json']):
        try:
            with open(json_files['profiles.json'], 'r', encoding='utf-8') as f:
                profiles_data = json.load(f)
            for name, profile_data in profiles_data.items():
                settings_manager.save_profile(name, profile_data)
            logger.info("✓ Migrated profiles.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating profiles.json: {e}")
            error_count += 1
    
    # Migrate tags.json
    if os.path.exists(json_files['tags.json']):
        try:
            with open(json_files['tags.json'], 'r', encoding='utf-8') as f:
                tags_data = json.load(f)
            if isinstance(tags_data, list):
                for tag_data in tags_data:
                    settings_manager.save_tag(tag_data)
            logger.info("✓ Migrated tags.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating tags.json: {e}")
            error_count += 1
    
    # Migrate frequencies.json
    if os.path.exists(json_files['frequencies.json']):
        try:
            with open(json_files['frequencies.json'], 'r', encoding='utf-8') as f:
                frequencies_data = json.load(f)
            if isinstance(frequencies_data, list):
                for freq_data in frequencies_data:
                    settings_manager.save_frequency(freq_data)
            logger.info("✓ Migrated frequencies.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating frequencies.json: {e}")
            error_count += 1
    
    # Migrate channels.json
    if os.path.exists(json_files['channels.json']):
        try:
            with open(json_files['channels.json'], 'r', encoding='utf-8') as f:
                channels_data = json.load(f)
            if isinstance(channels_data, list):
                for channel_data in channels_data:
                    settings_manager.save_channel(channel_data)
            logger.info("✓ Migrated channels.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating channels.json: {e}")
            error_count += 1
    
    # Migrate tokens.json
    if os.path.exists(json_files['tokens.json']):
        try:
            with open(json_files['tokens.json'], 'r', encoding='utf-8') as f:
                tokens_data = json.load(f)
            if isinstance(tokens_data, dict):
                for token, token_data in tokens_data.items():
                    settings_manager.save_token(token, token_data)
            logger.info("✓ Migrated tokens.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating tokens.json: {e}")
            error_count += 1
    
    # Migrate branding.json
    if os.path.exists(json_files['branding.json']):
        try:
            with open(json_files['branding.json'], 'r', encoding='utf-8') as f:
                branding_data = json.load(f)
            settings_manager.save_branding(branding_data)
            logger.info("✓ Migrated branding.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating branding.json: {e}")
            error_count += 1
    
    # Migrate hallucinations.json
    if os.path.exists(json_files['hallucinations.json']):
        try:
            with open(json_files['hallucinations.json'], 'r', encoding='utf-8') as f:
                hallucinations_data = json.load(f)
            if isinstance(hallucinations_data, list):
                for hallucination in hallucinations_data:
                    settings_manager.save_hallucination(hallucination)
            logger.info("✓ Migrated hallucinations.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating hallucinations.json: {e}")
            error_count += 1
    
    # Migrate backup_history.json
    if os.path.exists(json_files['backup_history.json']):
        try:
            with open(json_files['backup_history.json'], 'r', encoding='utf-8') as f:
                backup_data = json.load(f)
            if isinstance(backup_data, list):
                for backup_entry in backup_data:
                    settings_manager.save_backup_history(backup_entry)
            elif isinstance(backup_data, dict) and 'backups' in backup_data:
                for backup_entry in backup_data['backups']:
                    settings_manager.save_backup_history(backup_entry)
            logger.info("✓ Migrated backup_history.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating backup_history.json: {e}")
            error_count += 1
    
    # Migrate reboot_history.json
    if os.path.exists(json_files['reboot_history.json']):
        try:
            with open(json_files['reboot_history.json'], 'r', encoding='utf-8') as f:
                reboot_data = json.load(f)
            if isinstance(reboot_data, dict):
                for mac_address, reboot_list in reboot_data.items():
                    if isinstance(reboot_list, list):
                        for reboot_entry in reboot_list:
                            settings_manager.save_reboot_history(
                                mac_address,
                                reboot_entry.get('timestamp', ''),
                                reboot_entry.get('port', '')
                            )
            logger.info("✓ Migrated reboot_history.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating reboot_history.json: {e}")
            error_count += 1
    
    # Migrate scanner_inventory.json
    if os.path.exists(json_files['scanner_inventory.json']):
        try:
            with open(json_files['scanner_inventory.json'], 'r', encoding='utf-8') as f:
                scanner_data = json.load(f)
            if isinstance(scanner_data, dict):
                for scanner_id, scanner_info in scanner_data.items():
                    settings_manager.save_scanner(scanner_id, scanner_info)
            logger.info("✓ Migrated scanner_inventory.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating scanner_inventory.json: {e}")
            error_count += 1
    
    # Migrate recorders_inventory.json
    if os.path.exists(json_files['recorders_inventory.json']):
        try:
            with open(json_files['recorders_inventory.json'], 'r', encoding='utf-8') as f:
                recorders_data = json.load(f)
            if isinstance(recorders_data, list):
                for recorder in recorders_data:
                    settings_manager.save_recorder(recorder)
            elif isinstance(recorders_data, dict):
                for recorder_id, recorder_info in recorders_data.items():
                    settings_manager.save_recorder(recorder_info)
            logger.info("✓ Migrated recorders_inventory.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating recorders_inventory.json: {e}")
            error_count += 1
    
    # Migrate firmware_metadata.json
    if os.path.exists(json_files['firmware_metadata.json']):
        try:
            with open(json_files['firmware_metadata.json'], 'r', encoding='utf-8') as f:
                firmware_data = json.load(f)
            settings_manager.save_firmware_metadata(firmware_data)
            logger.info("✓ Migrated firmware_metadata.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating firmware_metadata.json: {e}")
            error_count += 1
    
    # Migrate queue.json
    if os.path.exists(json_files['queue.json']):
        try:
            with open(json_files['queue.json'], 'r', encoding='utf-8') as f:
                queue_data = json.load(f)
            if isinstance(queue_data, list):
                for queue_item in queue_data:
                    settings_manager.save_queue_item(queue_item)
            elif isinstance(queue_data, dict) and 'tasks' in queue_data:
                for queue_item in queue_data['tasks']:
                    settings_manager.save_queue_item(queue_item)
            logger.info("✓ Migrated queue.json")
            migrated_count += 1
        except Exception as e:
            logger.error(f"✗ Error migrating queue.json: {e}")
            error_count += 1

    logger.info("=" * 60)
    logger.info(f"Migration complete: {migrated_count} files migrated, {error_count} errors")
    logger.info("=" * 60)
    
    return migrated_count, error_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    migrate_json_to_sqlite()
