"""
Shared utility functions for route handlers.
"""
import json
import os
import logging

from config import DATA_ROOT
from datetime import datetime, timezone
from ..utils.logging_setup import error_logger
from ..services.settings_manager import get_settings_manager
from ..services.recording_state import update_transcription as persist_transcription

# Path constants - use absolute paths based on this file's location
RECORDINGS_DIR = DATA_ROOT / 'recordings'
REPORTS_FOLDER = DATA_ROOT / 'db' / 'reports'

# Ensure directories exist
if not REPORTS_FOLDER.exists():
    REPORTS_FOLDER.mkdir(parents=True)

# Initialize SettingsManager
_settings_manager = get_settings_manager()

# Thread lock for channel operations (prevents concurrent channel creation)
# Note: Database operations have their own locking, but this prevents race conditions
# when multiple threads try to create the same channel simultaneously
import threading
channels_lock = threading.Lock()

def _acquire_lock_with_timeout(lock, timeout=10.0, mac_address="unknown"):
    """
    Acquire a lock with timeout and logging.
    Returns True if acquired, False if timeout.
    """
    logging.debug(f"Attempting to acquire lock for MAC {mac_address} (timeout: {timeout}s)...")
    acquired = lock.acquire(timeout=timeout)
    if acquired:
        logging.debug(f"Successfully acquired lock for MAC {mac_address}")
    else:
        error_logger.error(f"Timeout ({timeout}s) waiting for lock to create channel for MAC {mac_address} - another operation may be blocking")
        logging.error(f"Timeout ({timeout}s) waiting for lock to create channel for MAC {mac_address}")
    return acquired

def get_channel_id_from_mac(mac, refresh=False):
    """
    Retrieve channel_id from MAC address (excludes soft-deleted channels).
    Queries database directly - refresh parameter is kept for API compatibility but ignored.
    
    Args:
        mac (str): The MAC address to look up
        refresh (bool): Ignored - always queries database directly
    
    Returns:
        int or None: The channel ID if found and not deleted, None otherwise
    """
    if not mac:
        return None
    
    # Normalize MAC address (remove colons/dashes, convert to uppercase)
    from ..services.settings_manager import normalize_mac_address
    normalized_mac = normalize_mac_address(mac)
    
    # Query database directly instead of using cache
    try:
        channel = _settings_manager.get_channel_by_mac(normalized_mac)
        if channel and not channel.get('deleted'):
            return channel.get('id')
    except Exception as e:
        error_logger.error(f"Error querying channel by MAC from database: {str(e)}")
    
    return None

def get_mac_from_channel_id(channel_id):
    """
    Get MAC address from channel_id for S3 bucket naming.
    
    Args:
        channel_id (int): The channel ID to look up
        
    Returns:
        str or None: MAC address in lowercase, or None if not found
    """
    try:
        channel = _settings_manager.get_channel(channel_id)
        if channel:
            mac = channel.get('mac')
            if mac:
                return mac.lower()
        return None
    except Exception as e:
        error_logger.error(f"Error getting MAC from channel_id: {str(e)}")
        return None

def create_channel_for_mac(mac_address):
    """
    Create a new channel entry in database for the given MAC address,
    or restore a soft-deleted channel if one exists for this MAC.
    
    Args:
        mac_address (str): The MAC address to create a channel for (will be normalized to uppercase, no colons)
        
    Returns:
        int or None: The newly created or restored channel ID, or None if creation failed
    """
    mac = None  # Initialize to avoid NameError in exception handler
    lock_acquired = False  # Track lock state for cleanup
    try:
        logging.info(f"create_channel_for_mac called for MAC: {mac_address}")
        # Normalize MAC address (remove colons/dashes, convert to uppercase)
        from ..services.settings_manager import normalize_mac_address
        mac = normalize_mac_address(mac_address) if mac_address else None
        if not mac:
            error_logger.error("Cannot create channel: MAC address is empty")
            logging.error("Cannot create channel: MAC address is empty")
            return None
        
        logging.info(f"Normalized MAC address: {mac}")
        
        # Use lock to prevent concurrent channel creation (database has its own locking, but this prevents race conditions)
        lock_acquired = _acquire_lock_with_timeout(channels_lock, timeout=10.0, mac_address=mac)
        if not lock_acquired:
            return None
        
        try:
            logging.info(f"Acquired lock for MAC {mac}, checking for existing channel...")
            # Check if channel with this MAC already exists (including soft-deleted)
            try:
                existing_channel = _settings_manager.get_channel_by_mac(mac)
            except Exception as db_exc:
                error_logger.error(f"Database error checking for existing channel for MAC {mac}: {db_exc}")
                logging.error(f"Database error checking for existing channel for MAC {mac}: {db_exc}")
                raise  # Re-raise to be caught by outer exception handler
            if existing_channel:
                # Check if it's a soft-deleted channel that needs restoration
                if existing_channel.get('deleted'):
                    # Restore the channel
                    existing_channel['deleted'] = 0
                    existing_channel['status'] = 'restored'
                    _settings_manager.save_channel(existing_channel)
                    
                    logging.info(f"Restored soft-deleted channel for MAC {mac}: channel_id={existing_channel.get('id')}")
                    from ..utils.logging_setup import event_logger
                    event_logger.info(f"Restored channel {existing_channel.get('id')} for MAC address {mac}")
                    
                    return existing_channel.get('id')
                else:
                    # Channel exists and is active - just return it, don't log creation
                    logging.debug(f"Channel already exists for MAC {mac}: channel_id={existing_channel.get('id')}")
                    return existing_channel.get('id')
            
            # Create new channel entry with default values
            # Don't set 'id' - let save_channel auto-generate it via INSERT
            logging.info(f"No existing channel found for MAC {mac}, creating new channel...")
            new_channel = {
                'name': f'Device {mac}',  # Default name based on MAC
                'status': 'audio_uploaded',
                'model': 'tiny.en',
                'src_language': 'english',
                'target_language': 'english',
                'color': '#000000',
                'background_color': '#FFFFFF',
                'team_color': '#FFFFFF',
                'car': '',
                'driver': '',
                'person': '',
                'tag': 'tag',
                'mac': mac,
                'audio_stream_enabled': False,
                'auto_transcribe': True,  # Enable auto-transcription by default for new channels
                'threshold': '50',
                'silence': '1000',
                'min_rec': '1000',
                'max_rec': '30000',
                'audio_gain': '3',
                'deleted': 0
            }
            
            logging.info(f"Calling save_channel for MAC {mac}...")
            # Save the new channel
            saved_id = _settings_manager.save_channel(new_channel)
            logging.info(f"save_channel returned: {saved_id} for MAC {mac}")
            
            # Validate that save was successful (save_channel returns -1 on error)
            if saved_id is None or saved_id == -1 or saved_id <= 0:
                error_logger.error(f"Failed to save channel for MAC {mac}: save_channel returned {saved_id}")
                logging.error(f"Failed to save channel for MAC {mac}: save_channel returned {saved_id}")
                return None
            
            logging.info(f"Created new channel for MAC {mac}: channel_id={saved_id}")
            from ..utils.logging_setup import event_logger
            event_logger.info(f"Auto-created channel {saved_id} for MAC address {mac}")
            
            # Create notification for new channel
            try:
                from ..services.notification_service import create_channel_creation_notification
                channel_name = new_channel.get('name', f'Device {mac}')
                create_channel_creation_notification(mac, channel_name)
            except Exception as notif_error:
                logging.warning(f"Failed to create notification for channel creation: {notif_error}")
            
            return saved_id
        finally:
            # Always release the lock if we acquired it
            if lock_acquired:
                channels_lock.release()
                logging.debug(f"Released lock for MAC {mac}")
            
    except Exception as e:
        error_logger.error(f"Error creating channel for MAC {mac_address if mac_address else 'unknown'}: {str(e)}")
        logging.exception(f"Failed to create channel for MAC {mac_address if mac_address else 'unknown'}")
        # Ensure lock is released even on exception
        if 'lock_acquired' in locals() and lock_acquired:
            try:
                channels_lock.release()
            except Exception:
                pass  # Lock might have been released already
        return None

def get_recording_path(mac_address, timestamp_dt, uploaded_filename=None):
    """
    Generate the new recording file path structure: recordings/<MAC>/YYYY/MM/DD/YYYY-MM-DD-HH-MM-SS.wav
    
    Args:
        mac_address (str): Device MAC address (will be normalized to lowercase)
        timestamp_dt (datetime): Datetime object (timezone-aware or naive)
        uploaded_filename (str, optional): Sanitized client filename to
            preserve. If a file with that name already exists, the current
            six-digit microsecond value is appended before the extension.
        
    Returns:
        tuple: (relative_path, absolute_path, filename)
            - relative_path: Path relative to recordings directory (e.g., 'B8D61A5AD264/2024/01/15/2024-01-15-14-30-45.wav')
            - absolute_path: Full absolute path
            - filename: Just the filename (e.g., '2024-01-15-14-30-45.wav')
    """
    # Normalize MAC address to lowercase
    mac = mac_address.lower() if mac_address else None
    if not mac:
        raise ValueError("MAC address is required for recording path")
    
    # Device uploads retain their original name, while existing callers that do
    # not supply a filename retain the legacy timestamp-based naming behavior.
    if uploaded_filename is not None:
        if not uploaded_filename:
            raise ValueError("Uploaded filename is invalid")
        filename = uploaded_filename
    else:
        filename = timestamp_dt.strftime('%Y-%m-%d-%H-%M-%S.wav')
    
    # Extract date components for directory structure
    year = timestamp_dt.strftime('%Y')
    month = timestamp_dt.strftime('%m')
    day = timestamp_dt.strftime('%d')
    
    # Build relative path: recordings/<MAC>/YYYY/MM/DD/YYYY-MM-DD-HH-MM-SS.wav
    relative_path = os.path.join('recordings', mac, year, month, day, filename)

    # Build absolute path
    absolute_path = DATA_ROOT / relative_path

    if uploaded_filename is not None and absolute_path.exists():
        stem, extension = os.path.splitext(filename)
        microseconds = datetime.now(timezone.utc).microsecond
        filename = f"{stem}_{microseconds:06d}{extension}"
        relative_path = os.path.join(
            'recordings', mac, year, month, day, filename
        )
        absolute_path = DATA_ROOT / relative_path
    
    return absolute_path

def get_channel_details(channel_id):
    """
    Fetch the latest channel details from the database.
    
    Args:
        channel_id (int): The channel ID to look up
        
    Returns:
        dict: Channel details including threshold, silence, etc.
    """
    channel_details = {}
    
    try:
        # Get channel from database
        channel = _settings_manager.get_channel(channel_id)
        if channel:
            channel_details = {
                'threshold': channel.get('threshold', '50'),
                'silence': channel.get('silence', '1600'),
                'min_rec': channel.get('min_rec', '1000'),
                'max_rec': channel.get('max_rec', '30000'),
                'audio_gain': channel.get('audio_gain', '3'),
                'state': 'stop' if channel.get('status') == 'disabled' else channel.get('status', 'resume'),
            }
    except Exception as e:
        error_logger.error(f"Error fetching channel details: {str(e)}")
    
    return channel_details

def calculate_wav_duration(file_size):
    """
    Calculate audio duration from WAV file size.
    Assumes: 8000 Hz sample rate, 1 channel, 16-bit (2 bytes per sample), 44 byte WAV header.
    
    Args:
        file_size (int): Size of the WAV file in bytes
        
    Returns:
        float: Duration in seconds, rounded to 1 decimal place
    """
    WAV_HEADER_SIZE = 44
    BYTES_PER_SAMPLE = 2  # 16-bit = 2 bytes
    SAMPLE_RATE = 8000  # Hz
    
    if file_size <= WAV_HEADER_SIZE:
        return 0.0
    
    data_size = file_size - WAV_HEADER_SIZE
    num_samples = data_size / BYTES_PER_SAMPLE
    duration = num_samples / SAMPLE_RATE
    
    return round(duration, 1)

def allowed_file(filename, allowed_extensions=None):
    """Check if the file extension is allowed."""
    if allowed_extensions is None:
        allowed_extensions = {'wav', 'mp3'}
    return '.' not in filename or filename.rsplit('.', 1)[1].lower() in allowed_extensions

def load_tags():
    """Load the list of tags from database."""
    try:
        return _settings_manager.get_all_tags()
    except Exception as e:
        error_logger.error(f"Error loading tags from database: {str(e)}")
        # Return empty list on error
        return []

def save_tags(tags):
    """Persist the list of tags to database."""
    try:
        # Get existing tags to preserve IDs
        existing_tags = {tag['id']: tag for tag in _settings_manager.get_all_tags()}
        
        # Update or create tags
        for tag in tags:
            _settings_manager.save_tag(tag)
        
        # Delete tags that are no longer in the list
        existing_ids = {tag['id'] for tag in existing_tags.values()}
        new_ids = {tag.get('id') for tag in tags if tag.get('id')}
        for tag_id in existing_ids - new_ids:
            _settings_manager.delete_tag(tag_id)
    except Exception as e:
        error_logger.error(f"Error saving tags to database: {str(e)}")

# Database constants
import threading
import sqlite3
import sys
from config import Config

db_lock = threading.Lock()
# Get recordings database path from centralized config
DB_PATH = Config.get_recordings_db_path()

# Log types (legacy - kept for backward compatibility)
# All logs are now stored in logs.db database via DatabaseLoggingManager
LOG_TYPES = {
    'error': 'errors',
    'warning': 'warnings',
    'transcription': 'transcription',
    'database': 'database',
    'event': 'event',
    'app': 'app'
}

def load_settings():
    """Load settings from database."""
    try:
        return _settings_manager.get_all_settings()
    except Exception as e:
        error_logger.error(f"Error loading settings from database: {str(e)}")
        return {}

def format_utc_timestamp(timestamp_value):
    """Normalize a datetime or timestamp string and format it explicitly as UTC."""
    try:
        if isinstance(timestamp_value, datetime):
            dt = timestamp_value
        elif isinstance(timestamp_value, str):
            if len(timestamp_value) == 19 and timestamp_value.count('-') == 2 and timestamp_value.count(':') == 2:
                dt = datetime.strptime(timestamp_value, '%Y-%m-%d %H:%M:%S')
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(timestamp_value.replace('Z', '+00:00'))
        else:
            dt = timestamp_value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception as e:
        print(f"Error formatting UTC timestamp: {e}")
        return timestamp_value

# History management functions
import shutil

def create_history_entry(recording_id, transcription, audio_filename=None, description=None):
    """Create a new history entry for a recording with audio file backup."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    try:
        # Check if this is the first history entry (original version)
        cur.execute(
            "SELECT COUNT(*) FROM recording_history WHERE recording_id = ?",
            (recording_id,)
        )
        count = cur.fetchone()[0]
        
        if count == 0:
            # This is the original version - save it as version 0
            version_number = 0
            description = "Original"
        else:
            # Get current version number for this recording
            cur.execute(
                "SELECT MAX(version_number) FROM recording_history WHERE recording_id = ?",
                (recording_id,)
            )
            result = cur.fetchone()
            version_number = (result[0] or 0) + 1
            
            # If we have more than 5 versions (excluding original), delete the oldest non-original
            cur.execute(
                "SELECT COUNT(*) FROM recording_history WHERE recording_id = ? AND version_number > 0",
                (recording_id,)
            )
            non_original_count = cur.fetchone()[0]
            
            if non_original_count >= 5:
                # Get the oldest non-original version to delete its audio file
                cur.execute(
                    "SELECT audio_filename FROM recording_history WHERE recording_id = ? AND version_number = (SELECT MIN(version_number) FROM recording_history WHERE recording_id = ? AND version_number > 0)",
                    (recording_id, recording_id)
                )
                oldest_audio = cur.fetchone()
                if oldest_audio and oldest_audio[0]:
                    # Delete the oldest audio file
                    try:
                        oldest_audio_path = RECORDINGS_DIR / oldest_audio[0]
                        if oldest_audio_path.exists():
                            oldest_audio_path.unlink()
                    except Exception as e:
                        print(f"Warning: Failed to delete oldest audio file: {e}")
                
                # Delete the oldest non-original version from database
                cur.execute(
                    "DELETE FROM recording_history WHERE recording_id = ? AND version_number = (SELECT MIN(version_number) FROM recording_history WHERE recording_id = ? AND version_number > 0)",
                    (recording_id, recording_id)
                )
                # Adjust version numbers to be sequential (but keep original as 0)
                cur.execute(
                    "UPDATE recording_history SET version_number = version_number - 1 WHERE recording_id = ? AND version_number > 0",
                    (recording_id,)
                )
                version_number = 5  # Keep the latest 5 non-original versions
        
        # If audio_filename is provided, create a backup copy
        history_audio_filename = None
        if audio_filename:
            try:
                # Get the original file path
                cur.execute("SELECT filename FROM recordings WHERE id = ?", (recording_id,))
                result = cur.fetchone()
                if result:
                    original_filename = result[0]
                    original_path = DATA_ROOT / original_filename
                    
                    if original_path.exists():
                        # Create history audio filename with version number
                        history_audio_filename = f"{original_path.stem}_v{version_number}{original_path.suffix}"
                        history_path = RECORDINGS_DIR / history_audio_filename
                        
                        # Copy the current audio file to history
                        shutil.copy2(original_path, history_path)
            except Exception as e:
                print(f"Warning: Failed to backup audio file for history: {e}")
        
        # Get current time in UTC for the created_at field
        current_time = datetime.now(timezone.utc)
        formatted_time = current_time.strftime('%Y-%m-%d %H:%M:%S')
        
        # Insert new history entry with UTC timestamp
        cur.execute(
            "INSERT INTO recording_history (recording_id, version_number, transcription, audio_filename, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (recording_id, version_number, transcription, history_audio_filename, description, formatted_time)
        )
        
        conn.commit()
        return version_number
        
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_history_versions(recording_id):
    """Get all history versions with timestamps formatted in UTC."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    try:
        cur.execute(
            "SELECT id, version_number, transcription, audio_filename, created_at, description FROM recording_history WHERE recording_id = ? ORDER BY version_number DESC",
            (recording_id,)
        )
        rows = cur.fetchall()
        
        history = []
        for row in rows:
            created_at = format_utc_timestamp(row[4]) if row[4] else row[4]
            
            history.append({
                'id': row[0],
                'version_number': row[1],
                'transcription': row[2],
                'audio_filename': row[3],
                'created_at': created_at,
                'description': row[5]
            })
        
        return history
        
    except sqlite3.Error as e:
        raise e
    finally:
        conn.close()

def revert_to_version(recording_id, version_number):
    """Revert a recording to a specific version including audio file."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    try:
        # Get the history entry
        cur.execute(
            "SELECT transcription, audio_filename FROM recording_history WHERE recording_id = ? AND version_number = ?",
            (recording_id, version_number)
        )
        row = cur.fetchone()
        
        if not row:
            raise ValueError(f"Version {version_number} not found for recording {recording_id}")
        
        transcription, history_audio_filename = row
        
        # Get current recording info
        cur.execute("SELECT filename FROM recordings WHERE id = ?", (recording_id,))
        result = cur.fetchone()
        if not result:
            raise ValueError(f"Recording {recording_id} not found")
        
        current_filename = result[0]
        current_path = DATA_ROOT / current_filename
        
        # Update the current recording with the historical transcription
        persist_transcription(conn, recording_id, transcription)
        
        # If there was a history audio file, restore it
        if history_audio_filename:
            try:
                history_audio_path = RECORDINGS_DIR / history_audio_filename
                if history_audio_path.exists():
                    # Backup current audio file before replacing
                    if current_path.exists():
                        backup_path = current_path.with_name(current_path.name + ".backup")
                        shutil.copy2(current_path, backup_path)
                    
                    # Copy history audio file to current location
                    shutil.copy2(history_audio_path, current_path)
                    print(f"Successfully restored audio file from {history_audio_path} to {current_path}")
                else:
                    print(f"Warning: History audio file not found: {history_audio_path}")
            except Exception as e:
                print(f"Warning: Failed to restore audio file: {e}")
        else:
            print("No history audio file to restore")
        
        conn.commit()
        return {
            'transcription': transcription,
            'audio_filename': current_filename,  # Return the current filename, not history filename
            'message': 'Successfully reverted to version'
        }
        
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_settings():
    """Initialize settings in database if they don't exist"""
    try:
        from ..services.db_initializer import initialize_settings_database
        initialize_settings_database()
        return _settings_manager.get_all_settings()
    except Exception as e:
        logging.error(f"Error initializing settings: {str(e)}")
        raise

def init_users():
    """Initialize users in database if they don't exist"""
    # Users are initialized as part of database initialization
    pass
