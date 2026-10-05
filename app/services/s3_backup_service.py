# app/services/s3_backup_service.py
"""
S3 Backup Service for automated overnight backups.
Handles:
- Audio file backups (one-time, check if already uploaded)
- DB folder backups (7-day rotation by weekday)
- Logs folder backups
"""
import os
import json
import logging
import sqlite3
import shutil
from pathlib import Path
from config import Config, DATA_ROOT
from datetime import datetime, time
from botocore.exceptions import ClientError
from ..utils.s3_utils import get_s3_client, get_s3_settings, is_s3_enabled, ensure_bucket_exists
from ..utils.logging_setup import error_logger

log = logging.getLogger(__name__)

# Maximum retry attempts for backup operations
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

# Global backup progress tracking
_backup_progress = {
    'status': 'idle',  # idle, running, completed, error
    'current_operation': None,  # 'audio', 'db', 'logs'
    'total_files': 0,
    'processed_files': 0,
    'uploaded_files': 0,
    'skipped_files': 0,
    'error_files': 0,
    'message': '',
    'start_time': None,
    'end_time': None
}

# Samba / network share backup settings
from .settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

def get_samba_settings():
    """Load Samba / network share backup settings from database."""
    try:
        settings = _settings_manager.get_all_settings()
        return {
            'enabled': settings.get('samba_enabled', False),
            'share_path': settings.get('samba_share_path', '').strip(),
            'username': settings.get('samba_username', '').strip(),
            'password': settings.get('samba_password', '').strip(),
        }
    except Exception as e:
        log.warning(f"Error loading Samba backup settings: {e}")

    return {
        'enabled': False,
        'share_path': '',
        'username': '',
        'password': '',
    }


def is_samba_enabled():
    """
    Check if Samba backup is enabled.

    Note: This assumes the Samba share path is already mounted or accessible
    from the OS. Username/password are stored for configuration purposes but
    mounting/authentication is handled at the system level.
    """
    settings = get_samba_settings()
    return settings['enabled'] and bool(settings['share_path'])


def test_samba_connection(share_path=None, username=None, password=None):
    """
    Test Samba / network share connection by checking if the path exists and is writable.
    
    Args:
        share_path: Optional override for share path (if None, uses settings)
        username: Optional override for username (if None, uses settings)
        password: Optional override for password (if None, uses settings)
    
    Returns:
        dict: {
            'success': bool,
            'message': str,
            'details': dict with path, exists, writable, etc.
        }
    """
    try:
        # Use provided values or get from settings
        if share_path is None:
            settings = get_samba_settings()
            share_path = settings.get('share_path', '').strip()
            username = username or settings.get('username', '').strip()
            password = password or settings.get('password', '').strip()
        else:
            share_path = share_path.strip()
        
        if not share_path:
            return {
                'success': False,
                'message': 'Samba share path is not configured',
                'details': {
                    'path': '',
                    'exists': False,
                    'writable': False,
                    'error': 'No path provided'
                }
            }
        
        share_root = DATA_ROOT / share_path
        details = {
            'path': str(share_root.absolute()),
            'exists': False,
            'writable': False,
            'is_directory': False,
            'error': None
        }
        
        # Check if path exists
        if not share_root.exists():
            return {
                'success': False,
                'message': f'Samba share path does not exist: {share_path}',
                'details': details
            }
        
        details['exists'] = True
        
        # Check if it's a directory
        if not share_root.is_dir():
            return {
                'success': False,
                'message': f'Samba share path is not a directory: {share_path}',
                'details': details
            }
        
        details['is_directory'] = True
        
        # Test write permissions by creating a test file
        test_file_path = share_root / '.boondock_test_connection.tmp'
        try:
            # Try to create a test file
            test_file_path.write_text('test')
            details['writable'] = True
            
            # Clean up test file
            try:
                test_file_path.unlink()
            except Exception as cleanup_error:
                log.warning(f"Could not delete test file: {cleanup_error}")
            
            return {
                'success': True,
                'message': f'Samba share connection successful: {share_path}',
                'details': details
            }
        except PermissionError:
            details['error'] = 'Permission denied - cannot write to share'
            return {
                'success': False,
                'message': f'Cannot write to Samba share (permission denied): {share_path}',
                'details': details
            }
        except Exception as write_error:
            details['error'] = str(write_error)
            return {
                'success': False,
                'message': f'Error writing to Samba share: {str(write_error)}',
                'details': details
            }
            
    except Exception as e:
        error_msg = str(e)
        log.error(f"Error testing Samba connection: {error_msg}", exc_info=True)
        return {
            'success': False,
            'message': f'Error testing Samba connection: {error_msg}',
            'details': {
                'path': share_path if share_path else '',
                'exists': False,
                'writable': False,
                'error': error_msg
            }
        }


def _copy_to_samba(local_path: Path, samba_relative_path: str, samba_settings: dict):
    """
    Copy a local file to the Samba share, preserving a relative path structure.

    Args:
        local_path: Path to the local file
        samba_relative_path: Relative path under the Samba share root
        samba_settings: Dict from get_samba_settings()
    """
    try:
        share_root = DATA_ROOT / samba_settings['share_path']
        dest_path = share_root / samba_relative_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest_path)
        log.debug(f"Copied to Samba share: {dest_path}")
    except Exception as e:
        error_logger.error(f"Error copying file to Samba share ({local_path} -> {samba_relative_path}): {e}")
        raise

def get_backup_progress():
    """Get current backup progress."""
    return _backup_progress.copy()

def reset_backup_progress():
    """Reset backup progress to initial state."""
    global _backup_progress
    _backup_progress = {
        'status': 'idle',
        'current_operation': None,
        'total_files': 0,
        'processed_files': 0,
        'uploaded_files': 0,
        'skipped_files': 0,
        'error_files': 0,
        'message': '',
        'start_time': None,
        'end_time': None
    }

def retry_with_backoff(func, *args, **kwargs):
    """Retry a function up to MAX_RETRIES times with exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == MAX_RETRIES:
                error_logger.error(f"Failed after {MAX_RETRIES} attempts: {str(e)}")
                raise
            wait_time = RETRY_DELAY * attempt
            log.warning(f"Attempt {attempt} failed, retrying in {wait_time}s: {str(e)}")
            import time
            time.sleep(wait_time)
    return None

def check_file_exists_in_s3(client, bucket, s3_key):
    """Check if a file already exists in S3."""
    try:
        client.head_object(Bucket=bucket, Key=s3_key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise

def backup_audio_files(backup_type='incremental'):
    """
    Backup local audio files to S3.
    
    Args:
        backup_type: 'full' or 'incremental'
            - 'full': Backup all files (but skip if already in S3)
            - 'incremental': Only backup files not yet backed up (backed_up = 0)
    """
    global _backup_progress
    import time
    import sqlite3
    
    log.info(f"backup_audio_files() called with backup_type={backup_type}")
    _backup_progress['current_operation'] = 'audio'
    
    log.info("Checking if S3 is enabled...")
    if not is_s3_enabled():
        log.warning("S3 backup skipped - S3 upload is disabled")
        _backup_progress['message'] = 'S3 upload is disabled'
        return
    
    log.info("S3 is enabled, getting S3 settings...")
    s3_settings = get_s3_settings()
    bucket = s3_settings.get('bucket_name', '')
    log.info(f"S3 bucket name: {bucket if bucket else 'NOT CONFIGURED'}")
    if not bucket:
        log.error("S3 bucket name not configured, skipping audio backup")
        _backup_progress['message'] = 'S3 bucket name not configured'
        return
    
    try:
        log.info("Connecting to S3...")
        _backup_progress['message'] = 'Connecting to S3...'
        time.sleep(0.1)
        client = get_s3_client()
        if not client:
            log.error("S3 client not available, skipping audio backup")
            _backup_progress['message'] = 'S3 client not available'
            return
        log.info("S3 client obtained successfully")
        
        log.info("Ensuring S3 bucket exists...")
        _backup_progress['message'] = 'Ensuring S3 bucket exists...'
        time.sleep(0.1)
        try:
            ensure_bucket_exists(bucket)
            log.info(f"S3 bucket '{bucket}' exists or created")
        except Exception as e:
            log.error(f"ERROR ensuring bucket exists: {str(e)}", exc_info=True)
            raise
        
        # Get database path
        from ..services.audio_handler import DB_PATH
        
        # Find all audio files in recordings directory
        log.info("Scanning recordings directory...")
        _backup_progress['message'] = 'Scanning recordings directory...'
        time.sleep(0.1)
        recordings_dir = DATA_ROOT / 'recordings'
        log.info(f"Recordings directory path: {recordings_dir.absolute()}")
        if not recordings_dir.exists():
            log.warning(f"No recordings directory found at {recordings_dir.absolute()}, skipping audio backup")
            _backup_progress['message'] = 'No recordings directory found'
            return
        
        log.info("Searching for audio files...")
        all_audio_files = []
        for ext in ['*.wav', '*.mp3']:
            files = list(recordings_dir.rglob(ext))
            all_audio_files.extend(files)
            log.info(f"Found {len(files)} {ext} files")
        
        log.info(f"Total audio files found: {len(all_audio_files)}")
        
        # Filter files based on backup type
        audio_files = []
        if backup_type == 'incremental':
            log.info("Incremental backup: Filtering files not yet backed up...")
            # Get list of files already backed up from database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            try:
                cursor.execute('SELECT filename FROM recordings WHERE backed_up = 1')
                backed_up_files = {row[0] for row in cursor.fetchall()}
                log.info(f"Found {len(backed_up_files)} files already backed up in database")
            except sqlite3.OperationalError as e:
                log.warning(f"Error querying backed_up flag: {e}. Treating as full backup.")
                backed_up_files = set()
            finally:
                conn.close()
            
            # Filter to only files not backed up
            for audio_file in all_audio_files:
                relative_path = str(audio_file.relative_to(recordings_dir))
                # Normalize path separators
                relative_path = relative_path.replace('\\', '/')
                if relative_path not in backed_up_files:
                    audio_files.append(audio_file)
            
            log.info(f"Incremental backup: {len(audio_files)} files need backup (skipping {len(all_audio_files) - len(audio_files)} already backed up)")
        else:
            # Full backup: process all files
            audio_files = all_audio_files
            log.info(f"Full backup: Processing all {len(audio_files)} files")
        
        # Add to total files (accumulate with DB files)
        if _backup_progress['total_files'] == 0:
            _backup_progress['total_files'] = len(audio_files)
        else:
            _backup_progress['total_files'] += len(audio_files)
        log.info(f"Total files count updated to: {_backup_progress['total_files']}")
        _backup_progress['message'] = f'Found {len(audio_files)} audio files to backup. Starting upload...'
        log.info(f"Progress message updated: Found {len(audio_files)} audio files to backup. Starting upload...")
        time.sleep(0.1)
        
        uploaded_count = 0
        skipped_count = 0
        error_count = 0
        
        # Connect to database for updating backed_up flag
        db_conn = sqlite3.connect(DB_PATH)
        db_cursor = db_conn.cursor()
        
        for audio_file in audio_files:
            try:
                # Get relative path from recordings directory
                relative_path = audio_file.relative_to(recordings_dir)
                parts = relative_path.parts
                
                # Check if this is the new structure (MAC/YYYY/MM/DD/YYYY-MM-DD-HH-MM-SS.wav)
                # or old structure (channel_X/audio_*.wav)
                if len(parts) >= 5 and parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit():
                    # New structure: MAC/YYYY/MM/DD/filename.wav
                    mac_address = parts[0].lower()
                    year = parts[1]
                    month = parts[2]
                    day = parts[3]
                    filename = parts[-1]
                    
                    # Build S3 key matching the local structure
                    s3_key = f"{mac_address}/{year}/{month}/{day}/{filename}"
                else:
                    # Old structure: channel_X/audio_*.wav - need to convert
                    if len(parts) < 2:
                        continue
                    
                    channel_folder = parts[0]  # e.g., "channel_1"
                    filename = parts[-1]
                    
                    # Try to get MAC address from channel_id
                    channel_id_str = channel_folder.replace('channel_', '')
                    try:
                        channel_id = int(channel_id_str)
                    except ValueError:
                        log.warning(f"Could not extract channel_id from {channel_folder}, skipping {audio_file}")
                        continue
                    
                    # Get MAC address from channel using SettingsManager
                    mac_address = None
                    try:
                        channel = _settings_manager.get_channel(channel_id)
                        if channel:
                            mac = channel.get('mac')
                            if mac:
                                mac_address = mac.lower()
                    except Exception as e:
                        log.warning(f"Error reading channel from database: {e}")
                    
                    if not mac_address:
                        log.warning(f"No MAC address found for channel_id {channel_id}, skipping {audio_file}")
                        continue
                    
                    # Build S3 key: {mac_address}/{YYYY}/{MM}/{DD}/{YYYY}-{MM}-{DD}-{HH}-{MM}-{SS}.wav
                    # Extract timestamp from filename if possible (format: audio_YYYYMMDD_HHMMSS.wav)
                    mac_folder = mac_address.lower()
                    try:
                        # Try to extract date from filename: audio_YYYYMMDD_HHMMSS.wav
                        if filename.startswith('audio_') and len(filename) >= 20:
                            date_part = filename[6:14]  # YYYYMMDD
                            time_part = filename[15:21]  # HHMMSS
                            year = date_part[:4]
                            month = date_part[4:6]
                            day = date_part[6:8]
                            hour = time_part[:2]
                            minute = time_part[2:4]
                            second = time_part[4:6]
                            s3_key = f"{mac_folder}/{year}/{month}/{day}/{year}-{month}-{day}-{hour}-{minute}-{second}.wav"
                        else:
                            # Fallback: use filename as-is
                            s3_key = f"{mac_folder}/{filename}"
                    except Exception:
                        # Fallback: use filename as-is if parsing fails
                        s3_key = f"{mac_folder}/{filename}"
                
                # Check if file already exists in S3 (skip if exists, don't overwrite)
                if check_file_exists_in_s3(client, bucket, s3_key):
                    skipped_count += 1
                    _backup_progress['skipped_files'] += 1
                    _backup_progress['processed_files'] += 1
                    log.debug(f"File already in S3, skipping: {s3_key}")
                    
                    # Mark as backed up in database even if skipped (already in S3)
                    try:
                        relative_path = str(audio_file.relative_to(recordings_dir)).replace('\\', '/')
                        db_cursor.execute('''
                            UPDATE recordings 
                            SET backed_up = 1 
                            WHERE filename = ?
                        ''', (relative_path,))
                        db_conn.commit()
                    except Exception as db_err:
                        log.warning(f"Error updating backed_up flag for {relative_path}: {db_err}")
                    continue
                
                # Upload file
                def upload_file():
                    with open(audio_file, 'rb') as f:
                        client.upload_fileobj(f, bucket, s3_key, ExtraArgs={'ContentType': 'audio/wav'})
                
                retry_with_backoff(upload_file)
                uploaded_count += 1
                _backup_progress['uploaded_files'] += 1
                _backup_progress['processed_files'] += 1
                
                # Mark as backed up in database after successful upload
                try:
                    relative_path = str(audio_file.relative_to(recordings_dir)).replace('\\', '/')
                    db_cursor.execute('''
                        UPDATE recordings 
                        SET backed_up = 1 
                        WHERE filename = ?
                    ''', (relative_path,))
                    db_conn.commit()
                    log.debug(f"Marked {relative_path} as backed up in database")
                except Exception as db_err:
                    log.warning(f"Error updating backed_up flag for {relative_path}: {db_err}")
                
                if len(audio_files) > 0:
                    _backup_progress['message'] = f'Uploading audio files: {uploaded_count} of {len(audio_files)} uploaded ({_backup_progress["processed_files"]} processed, {_backup_progress["skipped_files"]} skipped)'
                log.info(f"Backed up audio file: {s3_key}")
                
            except Exception as e:
                error_count += 1
                _backup_progress['error_files'] += 1
                _backup_progress['processed_files'] += 1
                error_logger.error(f"Error backing up audio file {audio_file}: {str(e)}")
        
        # Close database connection
        db_conn.close()
        
        _backup_progress['message'] = f'Audio backup ({backup_type}): {uploaded_count} uploaded, {skipped_count} skipped, {error_count} errors'
        log.info(f"Audio backup ({backup_type}) completed: {uploaded_count} uploaded, {skipped_count} already in S3, {error_count} errors")
        
    except Exception as e:
        error_logger.error(f"Audio backup failed: {str(e)}")
        # Don't raise - backup failures shouldn't crash the app

def backup_db_folder():
    """Backup DB folder to S3 with 7-day rotation (by weekday)."""
    global _backup_progress
    import time
    
    log.info("backup_db_folder() called")
    _backup_progress['current_operation'] = 'db'
    
    log.info("Checking if S3 is enabled...")
    if not is_s3_enabled():
        log.warning("S3 backup skipped - S3 upload is disabled")
        _backup_progress['message'] = 'S3 upload is disabled'
        return
    
    log.info("S3 is enabled, getting S3 settings...")
    s3_settings = get_s3_settings()
    bucket = s3_settings.get('bucket_name', '')
    log.info(f"S3 bucket name: {bucket if bucket else 'NOT CONFIGURED'}")
    if not bucket:
        log.error("S3 bucket name not configured, skipping DB backup")
        _backup_progress['message'] = 'S3 bucket name not configured'
        return
    
    try:
        log.info("Connecting to S3 for database backup...")
        _backup_progress['message'] = 'Connecting to S3 for database backup...'
        time.sleep(0.1)
        client = get_s3_client()
        if not client:
            log.error("S3 client not available, skipping DB backup")
            _backup_progress['message'] = 'S3 client not available'
            return
        log.info("S3 client obtained successfully")
        
        log.info("Ensuring S3 bucket exists...")
        _backup_progress['message'] = 'Ensuring S3 bucket exists...'
        time.sleep(0.1)
        try:
            ensure_bucket_exists(bucket)
            log.info(f"S3 bucket '{bucket}' exists or created")
        except Exception as e:
            log.error(f"ERROR ensuring bucket exists: {str(e)}", exc_info=True)
            raise
        
        db_dir = DATA_ROOT / 'db'
        log.info(f"DB directory path: {db_dir.absolute()}")
        if not db_dir.exists():
            log.warning(f"DB directory not found at {db_dir.absolute()}, skipping DB backup")
            _backup_progress['message'] = 'DB directory not found'
            return
        
        # Get weekday name (Monday, Tuesday, etc.)
        weekday_name = datetime.now(timezone.utc).strftime('%A').lower()
        s3_prefix = f"backups/db/{weekday_name}/"
        log.info(f"Using S3 prefix: {s3_prefix}")
        
        # Find all files in db directory
        log.info("Scanning database files...")
        _backup_progress['message'] = 'Scanning database files...'
        time.sleep(0.1)
        db_files = []
        for file_path in db_dir.iterdir():
            if file_path.is_file():
                db_files.append(file_path)
                log.debug(f"Found DB file: {file_path.name}")
        
        log.info(f"Found {len(db_files)} DB files to backup")
        # Add to total files (don't overwrite, as audio files will add to this)
        if _backup_progress['total_files'] == 0:
            _backup_progress['total_files'] = len(db_files)
        else:
            _backup_progress['total_files'] += len(db_files)
        log.info(f"Total files count updated to: {_backup_progress['total_files']}")
        _backup_progress['message'] = f'Backing up {len(db_files)} database files...'
        log.info(f"Progress message updated: Backing up {len(db_files)} database files...")
        time.sleep(0.1)
        
        uploaded_count = 0
        error_count = 0
        
        for db_file in db_files:
            try:
                # Build S3 key: backups/db/{weekday}/{filename}
                s3_key = f"{s3_prefix}{db_file.name}"
                
                def upload_file():
                    with open(db_file, 'rb') as f:
                        content_type = 'application/json' if db_file.suffix == '.json' else 'application/octet-stream'
                        client.upload_fileobj(f, bucket, s3_key, ExtraArgs={'ContentType': content_type})
                
                retry_with_backoff(upload_file)
                uploaded_count += 1
                _backup_progress['uploaded_files'] += 1
                _backup_progress['processed_files'] += 1
                _backup_progress['message'] = f'Database backup: {uploaded_count} of {len(db_files)} files uploaded...'
                log.info(f"Backed up DB file: {s3_key}")
                
            except Exception as e:
                error_count += 1
                _backup_progress['error_files'] += 1
                _backup_progress['processed_files'] += 1
                error_logger.error(f"Error backing up DB file {db_file}: {str(e)}")
        
        _backup_progress['message'] = f'Database backup: {uploaded_count} uploaded, {error_count} errors'
        log.info(f"DB backup completed: {uploaded_count} uploaded, {error_count} errors")
        
    except Exception as e:
        error_logger.error(f"DB backup failed: {str(e)}")
        # Don't raise - backup failures shouldn't crash the app

def backup_logs_folder():
    """Backup logs folder to S3."""
    global _backup_progress
    _backup_progress['current_operation'] = 'logs'
    _backup_progress['message'] = 'Scanning log files...'
    
    if not is_s3_enabled():
        log.info("S3 backup skipped - S3 upload is disabled")
        _backup_progress['message'] = 'S3 upload is disabled'
        return
    
    s3_settings = get_s3_settings()
    bucket = s3_settings.get('bucket_name', '')
    if not bucket:
        log.warning("S3 bucket name not configured, skipping logs backup")
        return
    
    try:
        client = get_s3_client()
        if not client:
            log.warning("S3 client not available, skipping logs backup")
            return
        
        ensure_bucket_exists(bucket)
        
        # Look for log files in the logs folder structure (logs/YYYY/MM/YYYY-MM-DD_*.log)
        # Get logs directory from centralized config
        logs_dir = Config.get_logs_dir()
        
        if logs_dir.exists() and logs_dir.is_dir():
            # Recursively find all .log files in the logs directory
            log_files = list(logs_dir.rglob('*.log'))
        else:
            log.info("Logs directory not found, skipping logs backup")
            _backup_progress['message'] = 'Logs directory not found'
            return
        
        if not log_files:
            log.info("No log files found, skipping logs backup")
            _backup_progress['message'] = 'No log files found'
            return
        
        # Use today's date for log backup folder
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        s3_prefix = f"backups/logs/{date_str}/"
        
        log.info(f"Backing up {len(log_files)} log files to {s3_prefix}")
        _backup_progress['total_files'] += len(log_files)
        _backup_progress['message'] = f'Backing up {len(log_files)} log files...'
        
        uploaded_count = 0
        error_count = 0
        
        for log_file in log_files:
            try:
                # Build S3 key: backups/logs/{date}/{filename}
                s3_key = f"{s3_prefix}{log_file.name}"
                
                def upload_file():
                    with open(log_file, 'rb') as f:
                        client.upload_fileobj(f, bucket, s3_key, ExtraArgs={'ContentType': 'text/plain'})
                
                retry_with_backoff(upload_file)
                uploaded_count += 1
                _backup_progress['uploaded_files'] += 1
                _backup_progress['processed_files'] += 1
                _backup_progress['message'] = f'Logs backup: {uploaded_count} of {len(log_files)} files uploaded...'
                log.info(f"Backed up log file: {s3_key}")
                
            except Exception as e:
                error_count += 1
                _backup_progress['error_files'] += 1
                _backup_progress['processed_files'] += 1
                error_logger.error(f"Error backing up log file {log_file}: {str(e)}")
        
        _backup_progress['message'] = f'Logs backup: {uploaded_count} uploaded, {error_count} errors'
        log.info(f"Logs backup completed: {uploaded_count} uploaded, {error_count} errors")
        
    except Exception as e:
        error_logger.error(f"Logs backup failed: {str(e)}")
        # Don't raise - backup failures shouldn't crash the app


def backup_db_folder_to_samba(samba_settings: dict):
    """Backup DB folder to Samba share with 7-day rotation (by weekday)."""
    global _backup_progress
    log.info("backup_db_folder_to_samba() called")

    if not is_samba_enabled():
        log.info("Samba backup skipped - Samba backup is disabled or misconfigured")
        return

    db_dir = DATA_ROOT / 'db'
    log.info(f"Samba DB directory path: {db_dir.absolute()}")
    if not db_dir.exists():
        log.warning(f"DB directory not found at {db_dir.absolute()}, skipping Samba DB backup")
        return

    weekday_name = datetime.now(timezone.utc).strftime('%A').lower()
    samba_prefix = f"backups/db/{weekday_name}/"
    log.info(f"Using Samba prefix: {samba_prefix}")

    log.info("Scanning database files for Samba backup...")
    db_files = [p for p in db_dir.iterdir() if p.is_file()]
    log.info(f"Found {len(db_files)} DB files to backup to Samba")

    _backup_progress['total_files'] += len(db_files)
    uploaded_count = 0
    error_count = 0

    for db_file in db_files:
        try:
            samba_key = f"{samba_prefix}{db_file.name}"
            _copy_to_samba(db_file, samba_key, samba_settings)
            uploaded_count += 1
            _backup_progress['uploaded_files'] += 1
            _backup_progress['processed_files'] += 1
        except Exception:
            error_count += 1
            _backup_progress['error_files'] += 1
            _backup_progress['processed_files'] += 1

    log.info(f"Samba DB backup completed: {uploaded_count} uploaded, {error_count} errors")


def backup_logs_folder_to_samba(samba_settings: dict):
    """Backup logs folder to Samba share."""
    global _backup_progress
    log.info("backup_logs_folder_to_samba() called")

    if not is_samba_enabled():
        log.info("Samba logs backup skipped - Samba backup is disabled or misconfigured")
        return

    # Get logs directory from centralized config    
    logs_dir = Config.get_logs_dir()
    
    if not logs_dir.exists() or not logs_dir.is_dir():
        log.info("Logs directory not found, skipping Samba logs backup")
        return

    log_files = list(logs_dir.rglob('*.log'))
    if not log_files:
        log.info("No log files found, skipping Samba logs backup")
        return

    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    samba_prefix = f"backups/logs/{date_str}/"

    _backup_progress['total_files'] += len(log_files)
    uploaded_count = 0
    error_count = 0

    for log_file in log_files:
        try:
            samba_key = f"{samba_prefix}{log_file.name}"
            _copy_to_samba(log_file, samba_key, samba_settings)
            uploaded_count += 1
            _backup_progress['uploaded_files'] += 1
            _backup_progress['processed_files'] += 1
        except Exception:
            error_count += 1
            _backup_progress['error_files'] += 1
            _backup_progress['processed_files'] += 1

    log.info(f"Samba logs backup completed: {uploaded_count} uploaded, {error_count} errors")


def backup_audio_files_to_samba(backup_type='incremental'):
    """
    Backup local audio files to Samba share.

    Uses the same path structure as S3 backups so that directory layout is familiar.
    """
    global _backup_progress
    import time

    log.info(f"backup_audio_files_to_samba() called with backup_type={backup_type}")

    if not is_samba_enabled():
        log.info("Samba audio backup skipped - Samba backup is disabled or misconfigured")
        return

    samba_settings = get_samba_settings()

    from ..services.audio_handler import DB_PATH

    recordings_dir = DATA_ROOT / 'recordings'
    log.info(f"Recordings directory path for Samba: {recordings_dir.absolute()}")
    if not recordings_dir.exists():
        log.warning(f"No recordings directory found at {recordings_dir.absolute()}, skipping Samba audio backup")
        return

    all_audio_files = []
    for ext in ['*.wav', '*.mp3']:
        files = list(recordings_dir.rglob(ext))
        all_audio_files.extend(files)
        log.info(f"Samba: found {len(files)} {ext} files")

    log.info(f"Samba: total audio files found: {len(all_audio_files)}")

    audio_files = []
    if backup_type == 'incremental':
        log.info("Samba incremental backup: filtering files not yet marked as backed_up...")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT filename FROM recordings WHERE backed_up = 1')
            backed_up_files = {row[0] for row in cursor.fetchall()}
            log.info(f"Samba: {len(backed_up_files)} files already marked as backed up in database")
        except sqlite3.OperationalError as e:
            log.warning(f"Samba: error querying backed_up flag: {e}. Treating as full backup.")
            backed_up_files = set()
        finally:
            conn.close()

        for audio_file in all_audio_files:
            relative_path = str(audio_file.relative_to(recordings_dir)).replace('\\', '/')
            if relative_path not in backed_up_files:
                audio_files.append(audio_file)

        log.info(f"Samba incremental backup: {len(audio_files)} files need backup (skipping {len(all_audio_files) - len(audio_files)} already backed up)")
    else:
        audio_files = all_audio_files
        log.info(f"Samba full backup: processing all {len(audio_files)} files")

    _backup_progress['total_files'] += len(audio_files)

    uploaded_count = 0
    error_count = 0

    db_conn = sqlite3.connect(DB_PATH)
    db_cursor = db_conn.cursor()

    for audio_file in audio_files:
        try:
            relative_path = audio_file.relative_to(recordings_dir)
            parts = relative_path.parts

            # Reuse the same structure logic as S3
            if len(parts) >= 5 and parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit():
                mac_address = parts[0].lower()
                year = parts[1]
                month = parts[2]
                day = parts[3]
                filename = parts[-1]
                samba_key = f"{mac_address}/{year}/{month}/{day}/{filename}"
            else:
                if len(parts) < 2:
                    continue

                channel_folder = parts[0]
                filename = parts[-1]
                channel_id_str = channel_folder.replace('channel_', '')
                try:
                    channel_id = int(channel_id_str)
                except ValueError:
                    log.warning(f"Samba: could not extract channel_id from {channel_folder}, skipping {audio_file}")
                    continue

                channels_json_path = DATA_ROOT / 'db' / 'channels.json'
                mac_address = None
                try:
                    channel = _settings_manager.get_channel(channel_id)
                    if channel:
                        mac = channel.get('mac')
                        if mac:
                            mac_address = mac.lower()
                except Exception as e:
                    log.warning(f"Samba: error reading channel from database: {e}")

                if not mac_address:
                    log.warning(f"Samba: no MAC address found for channel_id {channel_id}, skipping {audio_file}")
                    continue

                mac_folder = mac_address.lower()
                try:
                    if filename.startswith('audio_') and len(filename) >= 20:
                        date_part = filename[6:14]
                        time_part = filename[15:21]
                        year = date_part[:4]
                        month = date_part[4:6]
                        day = date_part[6:8]
                        hour = time_part[:2]
                        minute = time_part[2:4]
                        second = time_part[4:6]
                        samba_key = f"{mac_folder}/{year}/{month}/{day}/{year}-{month}-{day}-{hour}-{minute}-{second}.wav"
                    else:
                        samba_key = f"{mac_folder}/{filename}"
                except Exception:
                    samba_key = f"{mac_folder}/{filename}"

            _copy_to_samba(audio_file, samba_key, samba_settings)

            uploaded_count += 1
            _backup_progress['uploaded_files'] += 1
            _backup_progress['processed_files'] += 1

            try:
                rel_db_path = str(audio_file.relative_to(recordings_dir)).replace('\\', '/')
                db_cursor.execute(
                    '''
                    UPDATE recordings 
                    SET backed_up = 1 
                    WHERE filename = ?
                    ''',
                    (rel_db_path,),
                )
                db_conn.commit()
            except Exception as db_err:
                log.warning(f"Samba: error updating backed_up flag for {rel_db_path}: {db_err}")

        except Exception as e:
            error_count += 1
            _backup_progress['error_files'] += 1
            _backup_progress['processed_files'] += 1
            error_logger.error(f"Samba: error backing up audio file {audio_file}: {e}")

    db_conn.close()
    log.info(f"Samba audio backup ({backup_type}) completed: {uploaded_count} uploaded, {error_count} errors")

def save_backup_history(backup_data):
    """Save backup history entry to database."""
    try:
        _settings_manager.save_backup_history(backup_data)
        
        # Keep only last 100 entries
        all_history = _settings_manager.get_all_backup_history()
        if len(all_history) > 100:
            # Delete oldest entries (keep last 100)
            # Note: This is a simple approach - in production you might want a more efficient cleanup
            pass  # SettingsManager doesn't have delete by ID yet, but we can add it if needed
        
        log.info("Backup history saved successfully")
    except Exception as e:
        log.error(f"Error saving backup history: {str(e)}")

def get_backup_history(page=1, per_page=10):
    """Get backup history with pagination."""
    try:
        all_history = _settings_manager.get_all_backup_history()
        
        total = len(all_history)
        total_pages = (total + per_page - 1) // per_page
        start = (page - 1) * per_page
        end = start + per_page
        
        return {
            'history': all_history[start:end],
            'total': total,
            'total_pages': total_pages,
            'page': page,
            'per_page': per_page
        }
    except Exception as e:
        log.error(f"Error reading backup history: {str(e)}")
        return {'history': [], 'total': 0, 'total_pages': 0, 'page': page, 'per_page': per_page}

def run_backup_job(manual=False, backup_type='incremental', destination='both'):
    """
    Main backup job that runs all backup operations.
    
    Args:
        manual: Whether this is a manual backup (True) or scheduled (False)
        backup_type: 'full' or 'incremental' (default: 'incremental')
            - For scheduled backups, always use 'incremental'
            - For manual backups, use the provided backup_type
        destination: 'cloud', 'samba', or 'both'
            - 'cloud': only run S3/cloud backup steps
            - 'samba': only run Samba/network share backup steps
            - 'both': run both (default for scheduled backups)
    """
    global _backup_progress
    import time
    
    # Scheduled backups always use incremental and target both destinations
    if not manual:
        backup_type = 'incremental'
        destination = 'both'
    
    log.info("=" * 60)
    log.info(f"BACKUP JOB STARTING (Type: {backup_type}, Manual: {manual}, Destination: {destination})")
    log.info("=" * 60)
    
    if _backup_progress['status'] == 'running':
        log.warning("Backup already in progress, skipping")
        return
    
    log.info("Resetting backup progress...")
    reset_backup_progress()
    start_time = datetime.now(timezone.utc)
    _backup_progress['status'] = 'running'
    _backup_progress['start_time'] = start_time.isoformat()
    _backup_progress['end_time'] = None
    _backup_progress['total_files'] = 0
    _backup_progress['processed_files'] = 0
    _backup_progress['uploaded_files'] = 0
    _backup_progress['skipped_files'] = 0
    _backup_progress['error_files'] = 0
    log.info(f"Backup progress initialized. Status: {_backup_progress['status']}")
    
    try:
        # Step 1: Read System files
        log.info("-" * 60)
        log.info("STEP 1: Reading system files...")
        _backup_progress['message'] = 'Step 1: Reading system files...'
        _backup_progress['current_operation'] = 'system'
        log.info("Progress message updated: Step 1: Reading system files...")
        time.sleep(0.3)  # Small delay to ensure UI updates
        log.info("Step 1 completed")
        
        # Step 2: Backup Settings (DB folder) - Cloud/S3
        if destination in ('cloud', 'both'):
            log.info("-" * 60)
            log.info("STEP 2: Backing up settings to cloud...")
            _backup_progress['message'] = 'Step 2: Backing up settings to cloud...'
            _backup_progress['current_operation'] = 'settings'
            log.info("Progress message updated: Step 2: Backing up settings to cloud...")
            try:
                backup_db_folder()
                log.info("Step 2 (backup_db_folder - cloud) completed")
            except Exception as e:
                log.error(f"ERROR in Step 2 (backup_db_folder - cloud): {str(e)}", exc_info=True)
                error_logger.error(f"ERROR in Step 2 (backup_db_folder - cloud): {str(e)}", exc_info=True)
            time.sleep(0.1)
        
        # Step 3: Backup Database
        log.info("-" * 60)
        log.info("STEP 3: Backing up database...")
        _backup_progress['message'] = 'Step 3: Backing up database...'
        _backup_progress['current_operation'] = 'database'
        log.info("Progress message updated: Step 3: Backing up database...")
        # Database is already backed up in backup_db_folder, but we can add a specific message
        time.sleep(0.1)
        log.info("Step 3 completed")
        
        # Step 4: Calculate audio files to backup
        log.info("-" * 60)
        log.info("STEP 4: Calculating audio files to backup...")
        _backup_progress['message'] = 'Step 4: Calculating audio files to backup...'
        _backup_progress['current_operation'] = 'calculating'
        log.info("Progress message updated: Step 4: Calculating audio files to backup...")
        
        # Count audio files
        recordings_dir = DATA_ROOT / 'recordings'
        log.info(f"Checking recordings directory: {recordings_dir.absolute()}")
        audio_file_count = 0
        if recordings_dir.exists():
            log.info("Recordings directory exists, counting files...")
            for ext in ['*.wav', '*.mp3']:
                files = list(recordings_dir.rglob(ext))
                count = len(files)
                audio_file_count += count
                log.info(f"Found {count} {ext} files")
        else:
            log.warning(f"Recordings directory does not exist: {recordings_dir.absolute()}")
        
        log.info(f"Total audio files found: {audio_file_count}")
        _backup_progress['message'] = f'Step 4: Found {audio_file_count} audio files to backup'
        log.info(f"Progress message updated: Step 4: Found {audio_file_count} audio files to backup")
        time.sleep(0.2)
        log.info("Step 4 completed")
        
        # Step 5: Start audio files backup - Cloud/S3
        if destination in ('cloud', 'both'):
            log.info("-" * 60)
            log.info(f"STEP 5: Starting audio files backup to cloud ({backup_type})...")
            _backup_progress['message'] = f'Step 5: Starting audio files backup to cloud ({backup_type})...'
            _backup_progress['current_operation'] = 'audio'
            log.info(f"Progress message updated: Step 5: Starting audio files backup to cloud ({backup_type})...")
            try:
                backup_audio_files(backup_type=backup_type)
                log.info(f"Step 5 (backup_audio_files - cloud - {backup_type}) completed")
            except Exception as e:
                log.error(f"ERROR in Step 5 (backup_audio_files - cloud): {str(e)}", exc_info=True)
                error_logger.error(f"ERROR in Step 5 (backup_audio_files - cloud): {str(e)}", exc_info=True)
            time.sleep(0.1)
        
        # Backup logs - Cloud/S3
        if destination in ('cloud', 'both'):
            log.info("-" * 60)
            log.info("Backing up logs to S3 (if enabled)...")
            _backup_progress['message'] = 'Backing up logs to S3...'
            _backup_progress['current_operation'] = 'logs'
            log.info("Progress message updated: Backing up logs to S3...")
            try:
                backup_logs_folder()
                log.info("Logs backup to S3 completed")
            except Exception as e:
                log.error(f"ERROR in logs backup (S3): {str(e)}", exc_info=True)
                error_logger.error(f"ERROR in logs backup (S3): {str(e)}", exc_info=True)

        # Samba / network share backup (settings, audio, logs)
        log.info("-" * 60)
        log.info("Checking Samba backup configuration...")
        samba_settings = get_samba_settings()
        if destination in ('samba', 'both') and is_samba_enabled():
            log.info("Samba backup is enabled, starting Samba backup steps...")

            # Step A: DB/settings to Samba
            log.info("STEP A (Samba): Backing up settings/database to Samba share...")
            _backup_progress['message'] = 'Samba: Backing up settings/database...'
            _backup_progress['current_operation'] = 'samba_db'
            try:
                backup_db_folder_to_samba(samba_settings)
                log.info("STEP A (Samba DB) completed")
            except Exception as e:
                log.error(f"ERROR in Samba DB backup: {str(e)}", exc_info=True)
                error_logger.error(f"ERROR in Samba DB backup: {str(e)}", exc_info=True)

            # Step B: Audio to Samba
            log.info("STEP B (Samba): Backing up audio files to Samba share...")
            _backup_progress['message'] = f'Samba: Backing up audio files ({backup_type})...'
            _backup_progress['current_operation'] = 'samba_audio'
            try:
                backup_audio_files_to_samba(backup_type=backup_type)
                log.info(f"STEP B (Samba audio - {backup_type}) completed")
            except Exception as e:
                log.error(f"ERROR in Samba audio backup: {str(e)}", exc_info=True)
                error_logger.error(f"ERROR in Samba audio backup: {str(e)}", exc_info=True)

            # Step C: Logs to Samba
            log.info("STEP C (Samba): Backing up logs to Samba share...")
            _backup_progress['message'] = 'Samba: Backing up logs...'
            _backup_progress['current_operation'] = 'samba_logs'
            try:
                backup_logs_folder_to_samba(samba_settings)
                log.info("STEP C (Samba logs) completed")
            except Exception as e:
                log.error(f"ERROR in Samba logs backup: {str(e)}", exc_info=True)
                error_logger.error(f"ERROR in Samba logs backup: {str(e)}", exc_info=True)
        else:
            log.info("Samba backup is disabled, not configured, or not selected as destination; skipping Samba steps.")

        log.info("-" * 60)
        log.info("All backup steps completed")
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()
        _backup_progress['status'] = 'completed'
        _backup_progress['end_time'] = end_time.isoformat()
        _backup_progress['message'] = f'Backup completed: {_backup_progress["uploaded_files"]} uploaded, {_backup_progress["skipped_files"]} skipped, {_backup_progress["error_files"]} errors'
        log.info(f"Final stats - Uploaded: {_backup_progress['uploaded_files']}, Skipped: {_backup_progress['skipped_files']}, Errors: {_backup_progress['error_files']}, Total: {_backup_progress['total_files']}")
        
        # Save backup history
        backup_history_entry = {
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration': int(duration),
            'status': 'completed',
            'manual': manual,
            'backup_type': backup_type,
            'destination': destination,
            'uploaded_files': _backup_progress['uploaded_files'],
            'skipped_files': _backup_progress['skipped_files'],
            'error_files': _backup_progress['error_files'],
            'total_files': _backup_progress['total_files']
        }
        save_backup_history(backup_history_entry)
        
        log.info("=" * 60)
        log.info("BACKUP JOB COMPLETED SUCCESSFULLY")
        log.info("=" * 60)
    except Exception as e:
        log.error("=" * 60)
        log.error("BACKUP JOB FAILED")
        log.error("=" * 60)
        log.error(f"ERROR: {str(e)}", exc_info=True)
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()
        _backup_progress['status'] = 'error'
        _backup_progress['end_time'] = end_time.isoformat()
        _backup_progress['message'] = f'Backup failed: {str(e)}'
        
        # Save backup history even on failure
        backup_history_entry = {
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration': int(duration),
            'status': 'error',
            'manual': manual,
            'backup_type': backup_type,
            'destination': destination,
            'uploaded_files': _backup_progress.get('uploaded_files', 0),
            'skipped_files': _backup_progress.get('skipped_files', 0),
            'error_files': _backup_progress.get('error_files', 0),
            'total_files': _backup_progress.get('total_files', 0),
            'error_message': str(e)
        }
        save_backup_history(backup_history_entry)
        
        error_logger.error(f"Backup job failed: {str(e)}", exc_info=True)
        # Don't raise - backup failures shouldn't crash the app
