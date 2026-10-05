# app/services/audio_handler.py
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from queue import Queue
import sqlite3
import json
from config import Config, DATA_ROOT
from ..utils.logging_setup import error_logger, warning_logger, transcription_logger, db_logger
from .transcription_service import (
    TranscriptionAuthenticationError,
    TranscriptionService,
)
from .recording_state import is_hallucination, update_transcription
from .settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

class UploadTask:
    """Represents a pending upload transcription task."""
    def __init__(self, file_path, channel_id, timestamp, is_duplicate=False):
        self.file_path = file_path
        self.channel_id = channel_id
        self.timestamp = timestamp
        self.is_duplicate = is_duplicate
        self.status = "pending"  # pending, processing, completed, failed
        self.transcription = None
        self.error = None
        self.processing_started_at = None  # Track when processing started for timeout
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.enqueued_monotonic = time.perf_counter()
        self.completed_at = None
        # Transcription method details
        self.transcription_method = None  # 'local' or 'api'
        self.transcription_model = None  # Model name if local

def _get_db_path():
    """Return the current recordings DB path, resolved dynamically so event_name changes are picked up."""
    return Config.get_recordings_db_path()

# Two queue JSON files: current (pending/processing only) and history (resolved: completed/failed/killed)
CURRENT_QUEUE_JSON = Config.get_db_dir() / 'current_queue.json'
QUEUE_HISTORY_JSON = Config.get_db_dir() / 'queue_history.json'
QUEUE_HISTORY_MAX_ENTRIES = 500  # cap history file size

def _task_to_dict(task):
    """Serialize UploadTask to a JSON-serializable dict."""
    return {
        'file_path': task.file_path,
        'channel_id': task.channel_id,
        'timestamp': task.timestamp,
        'is_duplicate': getattr(task, 'is_duplicate', False),
        'status': task.status,
        'transcription': task.transcription,
        'error': task.error,
        'processing_started_at': task.processing_started_at.isoformat() if task.processing_started_at and hasattr(task.processing_started_at, 'isoformat') else task.processing_started_at,
        'created_at': task.created_at,
        'completed_at': task.completed_at,
        'transcription_method': getattr(task, 'transcription_method', None),
        'transcription_model': getattr(task, 'transcription_model', None),
    }

def _task_from_dict(filename, d):
    """Build UploadTask from a dict (from JSON)."""
    task = UploadTask(
        d['file_path'],
        d['channel_id'],
        d['timestamp'],
        is_duplicate=d.get('is_duplicate', False)
    )
    task.status = d.get('status', 'pending')
    task.transcription = d.get('transcription')
    task.error = d.get('error')
    task.created_at = d.get('created_at', datetime.now(timezone.utc).isoformat())
    task.enqueued_monotonic = None
    task.completed_at = d.get('completed_at')
    task.transcription_method = d.get('transcription_method')
    task.transcription_model = d.get('transcription_model')
    ps = d.get('processing_started_at')
    if ps:
        try:
            task.processing_started_at = datetime.fromisoformat(ps.replace('Z', '+00:00')) if isinstance(ps, str) else ps
        except Exception:
            task.processing_started_at = None
    return task

def load_settings():
    """Load settings from database."""
    try:
        settings = _settings_manager.get_all_settings()
        db_logger.info("Settings loaded successfully")
        return settings
    except Exception as e:
        error_logger.error(f"Error loading settings: {str(e)}")
        return {}   

class AudioChannel:
    """Handle individual audio channel operations."""
    
    def __init__(self, channel_id, output_dir):
        self.channel_id = channel_id
        self.output_dir = output_dir
        self.recording_lock = threading.Lock()
        os.makedirs(output_dir, exist_ok=True)
        db_logger.info(f"AudioChannel {channel_id} initialized successfully")

    def save_recording(self, filename, timestamp, transcription):
        """Store one successfully returned transcription and its classification."""
        with self.recording_lock:
            conn = sqlite3.connect(_get_db_path(), isolation_level='IMMEDIATE')
            cursor = None
            try:
                if not filename or not timestamp or not isinstance(transcription, str):
                    raise ValueError("Missing required fields for recording")
                    
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT id FROM recordings 
                    WHERE channel_id = ? AND filename = ?
                ''', (self.channel_id, filename))
                
                existing = cursor.fetchone()
                if existing:
                    update_transcription(conn, existing[0], transcription)
                else:
                    # Insert new record - calculate filesize and duration
                    file_path = (DATA_ROOT / filename).resolve()
                    file_size = 0
                    duration = 0.0
                    
                    if file_path.exists():
                        file_size = file_path.stat().st_size
                        # Calculate duration: (file_size - 44) / 2 / 8000
                        WAV_HEADER_SIZE = 44
                        BYTES_PER_SAMPLE = 2
                        SAMPLE_RATE = 8000
                        if file_size > WAV_HEADER_SIZE:
                            data_size = file_size - WAV_HEADER_SIZE
                            num_samples = data_size / BYTES_PER_SAMPLE
                            duration = round(num_samples / SAMPLE_RATE, 1)
                    
                    cursor.execute('''
                        INSERT INTO recordings (
                            channel_id, filename, timestamp, transcription, filesize,
                            duration, is_hallucination, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        self.channel_id, filename, timestamp, transcription,
                        file_size, duration, is_hallucination(transcription),
                        time.time_ns() // 1_000_000,
                    ))
                
                conn.commit()
                db_logger.info(f"Recording saved successfully: Channel {self.channel_id}, File: {filename}")
                return True
                
            except sqlite3.Error as e:
                error_logger.error(f"Database error while saving recording: {str(e)}")
                if conn:
                    conn.rollback()
                return False
                
            except Exception as e:
                error_logger.error(f"Unexpected error while saving recording: {str(e)}")
                if conn:
                    conn.rollback()
                return False
                
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()

    def get_recordings(self):
        """Retrieve recordings with validation against filesystem."""
        with self.recording_lock:
            conn = sqlite3.connect(_get_db_path())
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, channel_id, filename, timestamp, transcription, duration, filesize
                    FROM recordings
                    WHERE channel_id = ?
                    ORDER BY timestamp DESC
                ''', (self.channel_id,))
                
                recordings = []
                for row in cursor.fetchall():
                    file_path = row[2]
                    if os.path.exists(file_path):
                        recordings.append({
                            'id': row[0],
                            'channel_id': row[1],
                            'filename': row[2],
                            'timestamp': row[3],
                            'transcription': row[4],
                            'duration': row[5] if len(row) > 5 else None,
                            'filesize': row[6] if len(row) > 6 else None
                        })
                    else:
                        warning_logger.warning(f"Audio file not found: {file_path}")
                        cursor.execute('''
                            DELETE FROM recordings
                            WHERE id = ?
                        ''', (row[0],))
                
                conn.commit()
                return recordings
                
            except sqlite3.Error as e:
                error_logger.error(f"Error retrieving recordings for channel {self.channel_id}: {str(e)}")
                return []
                
            finally:
                conn.close()

class MultiChannelAudioHandler:
    """Handle multiple audio channels and their operations."""
    
    def __init__(self, model_name="small", transcribe_method="local"):
        try:
            self.running = False
            self.threads = []
            self.db_lock = threading.Lock()
            self.upload_queue = Queue()
            self.upload_tasks = {}
            self.upload_processor_thread = None
            self.upload_processor_lock = threading.Lock()
            self._pending_filenames_in_queue = set()  # filenames we've put in queue and not yet got (avoids duplicate put)
            self.queue_error = None
            self.channels = {}  # Dictionary to store channels dynamically
            # One authoritative timeout for watchdog and transcription-loop checks.
            self.processing_timeout_seconds = 120
            self.long_stuck_check_interval_seconds = 120  # Check every 2 min for long-stuck tasks

            self.transcription_service = TranscriptionService(model_name=model_name)
            self.transcribe_method = transcribe_method

            db_logger.info("MultiChannelAudioHandler initialized")
        except Exception as e:
            error_logger.error(f"Failed to initialize MultiChannelAudioHandler: {str(e)}")
            raise

    def get_or_create_channel(self, channel_id):
        """Get existing channel or create new one dynamically."""
        if channel_id not in self.channels:
            channel_dir = os.path.join('recordings', f'channel_{channel_id}')
            self.channels[channel_id] = AudioChannel(channel_id, channel_dir)
            db_logger.info(f"Created new channel: {channel_id}")
        return self.channels[channel_id]

    def _check_channel_auto_transcribe(self, channel_id):
        """
        Check if auto-transcription is enabled for a channel.
        
        Args:
            channel_id (int): The channel ID to check
            
        Returns:
            bool: True if auto-transcription is enabled, False otherwise.
                 Defaults to True if channel not found or setting not present.
        """
        try:
            channel = _settings_manager.get_channel(channel_id)
            if channel:
                # Return auto_transcribe setting, defaulting to True if not present
                return channel.get('auto_transcribe', True)
            
            # Channel not found - default to enabled
            return True
        except Exception as e:
            error_logger.error(f"Error checking channel auto_transcribe setting for channel {channel_id}: {str(e)}")
            # On error, default to enabled to ensure transcription happens
            return True

    def _update_recording_status(self, filename, status):
        """
        Update the status of a recording in the database.
        
        Args:
            filename (str): The filename of the recording
            status (str): The new status ('transcribed', 'skipped', 'failed', etc.)
        """
        try:
            conn = sqlite3.connect(_get_db_path(), timeout=5.0)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE recordings 
                SET status = ?, updated_at = MAX(updated_at + 1, ?)
                WHERE filename = ?
            ''', (status, time.time_ns() // 1_000_000, filename))
            conn.commit()
            conn.close()
        except Exception as e:
            error_logger.error(f"Error updating recording status for {filename}: {str(e)}")

    def _save_current_queue(self):
        """Persist only pending and processing tasks to current_queue.json."""
        try:
            with self.upload_processor_lock:
                tasks_dict = {}
                for filename, task in self.upload_tasks.items():
                    if task.status in ('pending', 'processing'):
                        tasks_dict[filename] = _task_to_dict(task)
                data = {
                    'last_updated': datetime.now(timezone.utc).isoformat(),
                    'is_running': self.running,
                    'queue_size': self.upload_queue.qsize(),
                    'total_tasks': len(tasks_dict),
                    'queue_error': self.queue_error,
                    'tasks': tasks_dict,
                }
                tmp_path = CURRENT_QUEUE_JSON.with_name(CURRENT_QUEUE_JSON.name + ".tmp")
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                tmp_path.replace(CURRENT_QUEUE_JSON)
        except Exception as e:
            error_logger.error(f"Error saving current queue to JSON: {str(e)}")

    def _add_to_queue_history(self, filename, task, resolution):
        """
        Append a resolved task to queue_history.json. resolution: 'completed' | 'failed' | 'killed'.
        Call after moving task out of current queue.
        """
        try:
            task_dict = _task_to_dict(task)
            entry = {
                'filename': filename,
                'task': task_dict,
                'resolved_at': datetime.now(timezone.utc).isoformat(),
                'resolution': resolution,
            }
            entries = []
            if QUEUE_HISTORY_JSON.exists():
                try:
                    with open(QUEUE_HISTORY_JSON, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        entries = data.get('entries', [])
                except (json.JSONDecodeError, ValueError) as parse_err:
                    error_logger.warning(f"queue_history.json was corrupt, resetting: {parse_err}")
                    entries = []
            entries.insert(0, entry)
            entries = entries[:QUEUE_HISTORY_MAX_ENTRIES]
            tmp_path = QUEUE_HISTORY_JSON.with_name(QUEUE_HISTORY_JSON.name + ".tmp")
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({'last_updated': datetime.now(timezone.utc).isoformat(), 'entries': entries}, f, indent=2, ensure_ascii=False)
            tmp_path.replace(QUEUE_HISTORY_JSON)
        except Exception as e:
            error_logger.error(f"Error appending to queue history: {str(e)}")

    def _load_current_queue(self):
        """Load current queue from current_queue.json and rebuild in-memory queue for pending/processing."""
        try:
            if not CURRENT_QUEUE_JSON.exists():
                return
            with open(CURRENT_QUEUE_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            tasks_data = data.get('tasks', {})
            self.queue_error = data.get('queue_error')
            if not tasks_data:
                return
            with self.upload_processor_lock:
                self.upload_tasks.clear()
                self._pending_filenames_in_queue.clear()
                while not self.upload_queue.empty():
                    try:
                        self.upload_queue.get_nowait()
                    except Exception:
                        break
                for filename, d in tasks_data.items():
                    task = _task_from_dict(filename, d)
                    self.upload_tasks[task.file_path] = task
                    if task.status in ('pending', 'processing'):
                        task.status = 'pending'
                        task.processing_started_at = None
                        self.upload_queue.put(task)
                        self._pending_filenames_in_queue.add(task.file_path)
            transcription_logger.info(f"Loaded current queue from JSON: {len(self.upload_tasks)} tasks, {self.upload_queue.qsize()} pending for processing")
        except Exception as e:
            error_logger.error(f"Error loading current queue from JSON: {str(e)}")

    def _move_task_to_history_and_remove_from_current(self, filename, resolution):
        """Add task to queue_history, remove from upload_tasks, save current_queue. Call with lock released for task access."""
        with self.upload_processor_lock:
            task = self.upload_tasks.get(filename)
            if not task:
                return
            task_dict = _task_to_dict(task)
            del self.upload_tasks[filename]
            self._pending_filenames_in_queue.discard(filename)
        self._add_to_queue_history(filename, task, resolution)
        self._save_current_queue()

    def start(self):
        """Start upload processing."""
        self.queue_error = None
        self.running = True
        self._save_current_queue()
        self.upload_processor_thread = threading.Thread(
            target=self.process_upload_queue,
            daemon=True
        )
        self.threads.append(self.upload_processor_thread)
        self.upload_processor_thread.start()
        db_logger.info("Started upload processor thread")

    def kill_task(self, filename):
        """
        Force kill a processing task and mark it as failed.
        This allows the queue to continue processing other tasks.
        
        Args:
            filename (str): Filename of the task to kill
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            file_path = None
            
            with self.upload_processor_lock:
                if filename not in self.upload_tasks:
                    return False, f"Task {filename} not found"
                
                task = self.upload_tasks[filename]
                
                # Only allow killing processing tasks
                if task.status != "processing":
                    return False, f"Cannot kill task with status: {task.status}. Only processing tasks can be killed."
                
                error_logger.warning(f"Force killing task: {filename}")
                task.status = "failed"
                task.error = "Task killed manually - processing exceeded timeout"
                task.processing_started_at = None
                task.completed_at = datetime.now(timezone.utc).isoformat()
                
                # Capture task attributes
                file_path = task.file_path
            
            # Update database outside lock
            if file_path:
                self._update_recording_status(file_path, 'failed')
                
                transcription_logger.info(f"Killed task {filename} - queue can now process next task")
                self._move_task_to_history_and_remove_from_current(filename, 'killed')
                return True, f"Task {filename} killed successfully"
            
            return False, "Failed to update task status"
                
        except Exception as e:
            error_logger.error(f"Error killing task {filename}: {str(e)}")
            return False, f"Error killing task: {str(e)}"

    def requeue_task(self, filename):
        """
        Requeue a failed or stuck task for processing.
        
        Args:
            filename (str): Filename of the task to requeue
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            with self.upload_processor_lock:
                if filename not in self.upload_tasks:
                    return False, f"Task {filename} not found"
                
                task = self.upload_tasks[filename]
                
                # Only allow requeueing failed or stuck processing tasks
                if task.status not in ['failed', 'processing']:
                    return False, f"Cannot requeue task with status: {task.status}"
                
                # Reset task state
                task.status = "pending"
                task.error = None
                task.processing_started_at = None
                task.created_at = datetime.now(timezone.utc).isoformat()
                task.enqueued_monotonic = time.perf_counter()
                
                # Re-add to queue
                self.upload_queue.put(task)
                self._pending_filenames_in_queue.add(filename)

                transcription_logger.info(f"Requeued task: {filename}")
                self._save_current_queue()
                return True, f"Task {filename} requeued successfully"
                
        except Exception as e:
            error_logger.error(f"Error requeueing task {filename}: {str(e)}")
            return False, f"Error requeueing task: {str(e)}"

    def purge_queue_logs(self, status_filter=None, date_filter=None, older_than_days=None):
        """
        Purge queue logs from queue_history.json (and in-memory upload_tasks) based on filters.
        The UI reads from queue_history.json, so we must purge that file for logs to disappear.
        """
        try:
            purged_count = 0
            now = datetime.now(timezone.utc)
            
            # Cutoff: entries older than this (created_at or resolved_at < cutoff) can be purged
            cutoff_date = None
            if older_than_days:
                cutoff_date = now - timedelta(days=older_than_days)
            elif date_filter == 'today':
                cutoff_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif date_filter == 'week':
                cutoff_date = now - timedelta(days=7)
            elif date_filter == 'month':
                cutoff_date = now - timedelta(days=30)
            
            # Map status_filter to history resolution: 'failed' includes 'killed'
            def resolution_matches(entry_resolution):
                if not status_filter:
                    return True
                if status_filter == 'completed':
                    return entry_resolution == 'completed'
                if status_filter == 'failed':
                    return entry_resolution in ('failed', 'killed')
                return entry_resolution == status_filter

            def entry_old_enough_to_purge(entry):
                if not cutoff_date:
                    return True
                task_dict = entry.get('task', {})
                created_at = task_dict.get('created_at') or entry.get('resolved_at', '')
                if not created_at:
                    return True
                try:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00')) if isinstance(created_at, str) else created_at
                    return dt < cutoff_date
                except Exception:
                    return False

            # Purge from queue_history.json (source of truth for UI logs)
            if QUEUE_HISTORY_JSON.exists():
                try:
                    try:
                        with open(QUEUE_HISTORY_JSON, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                    except (json.JSONDecodeError, TypeError) as read_err:
                        error_logger.warning(f"queue_history.json invalid or empty, treating as empty: {read_err}")
                        data = {'entries': [], 'last_updated': now.isoformat()}
                    if not isinstance(data, dict):
                        data = {'entries': [], 'last_updated': now.isoformat()}
                    entries = data.get('entries')
                    if not isinstance(entries, list):
                        entries = []
                    kept = []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            kept.append(entry)
                            continue
                        try:
                            resolution = entry.get('resolution', 'completed')
                            if resolution_matches(resolution) and entry_old_enough_to_purge(entry):
                                purged_count += 1
                            else:
                                kept.append(entry)
                        except Exception as entry_err:
                            error_logger.warning(f"Purge skip malformed history entry: {entry_err}")
                            kept.append(entry)
                    data['entries'] = kept
                    data['last_updated'] = now.isoformat()
                    tmp_path = QUEUE_HISTORY_JSON.with_name(QUEUE_HISTORY_JSON.name + ".tmp")
                    with open(tmp_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    try:
                        tmp_path.replace(QUEUE_HISTORY_JSON)
                    except Exception:
                        tmp_path.unlink()
                        raise
                except Exception as file_err:
                    error_logger.error(f"Error purging queue_history.json: {file_err}")
                    raise
            
            # Also remove from in-memory upload_tasks (completed/failed that might still be there)
            try:
                with self.upload_processor_lock:
                    tasks_to_remove = []
                    for filename, task in list(self.upload_tasks.items()):
                        try:
                            if getattr(task, 'status', None) in ['pending', 'processing']:
                                continue
                            if status_filter and getattr(task, 'status', None) != status_filter:
                                continue
                            if cutoff_date:
                                task_created = None
                                if hasattr(task, 'created_at') and task.created_at:
                                    try:
                                        task_created = datetime.fromisoformat(task.created_at.replace('Z', '+00:00')) if isinstance(task.created_at, str) else task.created_at
                                    except Exception:
                                        pass
                                if not task_created or task_created >= cutoff_date:
                                    continue
                            tasks_to_remove.append(filename)
                        except Exception as task_err:
                            error_logger.warning(f"Purge skip task {filename}: {task_err}")
                    for filename in tasks_to_remove:
                        try:
                            del self.upload_tasks[filename]
                            purged_count += 1
                        except Exception:
                            pass
                    self._save_current_queue()
            except Exception as mem_err:
                error_logger.error(f"Error purging in-memory queue: {mem_err}")
            
            transcription_logger.info(f"Purged {purged_count} tasks from queue logs (queue_history.json)")
            return {'purged_count': purged_count}
            
        except Exception as e:
            error_logger.error(f"Error purging queue logs: {str(e)}")
            return {'error': str(e), 'purged_count': 0}

    def queue_upload_for_processing(self, file_path, channel_id, is_duplicate=False):
        """Queue an uploaded file for processing."""
        queue_started_at = time.perf_counter()
        try:
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            task = UploadTask(file_path, channel_id, timestamp, is_duplicate=is_duplicate)

            filename = os.path.basename(file_path)
            with self.upload_processor_lock:
                self.upload_tasks[file_path] = task
                self.upload_queue.put(task)
                self._pending_filenames_in_queue.add(file_path)
            self._save_current_queue()

            # Ensure channel exists
            self.get_or_create_channel(channel_id)

            return True, {
                'filename': filename,
                'timestamp': timestamp,
                'status': 'pending',
                'is_duplicate': is_duplicate
            }
        except Exception as e:
            error_logger.error(f"Error queueing upload: {str(e)}", exc_info=True,)
            return False, str(e)

    def _check_stuck_tasks(self):
        """Check for tasks stuck in processing state and mark them as failed."""
        try:
            current_time = datetime.now(timezone.utc)
            timeout_delta = timedelta(seconds=self.processing_timeout_seconds)
            
            stuck_tasks = []
            with self.upload_processor_lock:
                for filename, task in list(self.upload_tasks.items()):
                    if task.status == "processing" and task.processing_started_at:
                        elapsed = current_time - task.processing_started_at
                        if elapsed > timeout_delta:
                            stuck_tasks.append((filename, task))
            
            # Mark stuck tasks as failed (outside lock to avoid deadlock)
            for filename, task in stuck_tasks:
                file_path = None
                
                with self.upload_processor_lock:
                    # Double-check status hasn't changed
                    if task.status == "processing" and task.processing_started_at:
                        error_logger.warning(f"Task {filename} stuck in processing for {self.processing_timeout_seconds} seconds, marking as failed")
                        task.status = "failed"
                        task.error = f"Processing timeout after {self.processing_timeout_seconds} seconds"
                        task.processing_started_at = None
                        # Capture task attributes before releasing lock
                        file_path = task.file_path
                
                # Update database outside lock
                if file_path:
                    self._update_recording_status(file_path, 'failed')
                    
                    transcription_logger.info(f"Marked stuck task {filename} as failed due to timeout")
                    self._move_task_to_history_and_remove_from_current(filename, 'failed')
                
        except Exception as e:
            error_logger.error(f"Error checking stuck tasks: {str(e)}")

    def _check_long_stuck_tasks(self):
        """
        Check for tasks stuck in processing for more than 2 minutes.
        Marks them as failed and returns the count. Used by the 2-min cycle
        to then re-queue unprocessed (pending) tasks.
        """
        try:
            current_time = datetime.now(timezone.utc)
            timeout_delta = timedelta(seconds=self.processing_timeout_seconds)
            marked_count = 0

            stuck_tasks = []
            with self.upload_processor_lock:
                for filename, task in list(self.upload_tasks.items()):
                    if task.status == "processing" and task.processing_started_at:
                        elapsed = current_time - task.processing_started_at
                        if elapsed > timeout_delta:
                            stuck_tasks.append((filename, task))

            for filename, task in stuck_tasks:
                file_path = None
                with self.upload_processor_lock:
                    if task.status == "processing" and task.processing_started_at:
                        error_logger.warning(
                            f"Task {filename} stuck in processing for > {self.processing_timeout_seconds}s, marking as failed"
                        )
                        task.status = "failed"
                        task.error = f"Processing stuck for more than {self.processing_timeout_seconds} seconds - queue restarted"
                        task.processing_started_at = None
                        file_path = task.file_path
                        marked_count += 1

                if file_path:
                    self._update_recording_status(file_path, 'failed')
                    transcription_logger.info(f"Marked long-stuck task {filename} as failed (2 min cycle)")
                    self._move_task_to_history_and_remove_from_current(filename, 'failed')

            return marked_count
        except Exception as e:
            error_logger.error(f"Error checking long stuck tasks: {str(e)}")
            return 0

    def _requeue_stuck_pending_tasks(self):
        """
        Re-add tasks that are 'pending' in upload_tasks but not currently in the queue
        (e.g. after processor was stuck or restarted). Avoids duplicates using _pending_filenames_in_queue.
        """
        try:
            with self.upload_processor_lock:
                for filename, task in list(self.upload_tasks.items()):
                    if task.status != "pending":
                        continue
                    if filename in self._pending_filenames_in_queue:
                        continue
                    self.upload_queue.put(task)
                    self._pending_filenames_in_queue.add(filename)
                    transcription_logger.debug(f"Re-queued pending task for unprocessed: {filename}")
        except Exception as e:
            error_logger.error(f"Error re-queuing stuck pending tasks: {str(e)}")

    def process_upload_queue(self):
        """Process queued upload tasks."""
        transcription_logger.info("Upload queue processor thread started")
        last_stuck_check = time.time()
        last_long_stuck_check = time.time()

        while self.running:
            try:
                current_time = time.time()
                # Check for stuck tasks every 2 seconds (30 sec timeout)
                if current_time - last_stuck_check >= 2:
                    self._check_stuck_tasks()
                    last_stuck_check = current_time

                # Every 2 min: check for tasks stuck > 2 min, mark failed, re-queue pending so unprocessed run again
                if current_time - last_long_stuck_check >= self.long_stuck_check_interval_seconds:
                    last_long_stuck_check = current_time
                    marked = self._check_long_stuck_tasks()
                    if marked > 0:
                        transcription_logger.info(
                            f"2 min cycle: marked {marked} long-stuck task(s) as failed, re-queuing pending for unprocessed"
                        )
                        self._requeue_stuck_pending_tasks()

                if not self.upload_queue.empty():
                    task = self.upload_queue.get()
                    self._pending_filenames_in_queue.discard(task.file_path)
                    transcription_logger.debug("Processing queued file: %s for channel %s", task.file_path, task.channel_id)
                    
                    channel = None
                    try:
                        task.status = "processing"
                        task.processing_started_at = datetime.now(timezone.utc)
                        
                        channel = self.get_or_create_channel(task.channel_id)
                        
                        # Update status to processing (don't save "...." to DB during processing)
                        self._update_recording_status(task.file_path, 'processing')
                        
                        absolute_path = (DATA_ROOT / task.file_path).resolve()
                        
                        # Verify file exists
                        if not absolute_path.exists():
                            error_logger.error(f"Audio file not found: {absolute_path}")
                            self._update_recording_status(task.file_path, 'failed')
                            task.status = "failed"
                            task.error = f"File not found: {absolute_path}"
                            task.processing_started_at = None
                            continue
                        
                        # Check if auto-transcription is enabled for this channel
                        auto_transcribe_enabled = self._check_channel_auto_transcribe(task.channel_id)
                        transcription_logger.debug(f"Channel {task.channel_id} auto_transcribe setting: {auto_transcribe_enabled}")
                        
                        # Skip transcription if file is a duplicate
                        if task.is_duplicate:
                            transcription_logger.info(f"Skipping transcription for duplicate file: {task.file_path} (is_duplicate=True)")
                            # Update status in database
                            self._update_recording_status(task.file_path, 'skipped')
                            task.status = "completed"
                            task.transcription = 'Duplicate file - not transcribed'
                            task.processing_started_at = None
                        elif not auto_transcribe_enabled:
                            transcription_logger.info(f"Skipping transcription for channel {task.channel_id} (auto_transcribe disabled)")
                            # Update status in database
                            self._update_recording_status(task.file_path, 'skipped')
                            task.status = "completed"
                            task.transcription = 'No transcription available'
                            task.processing_started_at = None
                        else:
                            transcription_logger.debug(f"Starting transcription for uploaded file: {task.file_path}")
                            
                            # Record transcription method details
                            task.transcription_method = self.transcribe_method
                            if self.transcribe_method == "local":
                                task.transcription_model = self.transcription_service.model_name

                            # Run transcription in a thread so we can check every ~10s and abort after 2 min if stuck
                            result_holder = [None]
                            error_holder = [None]

                            def run_transcribe():
                                try:
                                    result_holder[0] = self.transcription_service.transcribe_audio(
                                        absolute_path,
                                        use_local=self.transcribe_method == "local",
                                    )
                                except Exception as e:
                                    error_holder[0] = e

                            transcribe_thread = threading.Thread(target=run_transcribe, daemon=True)
                            transcribe_thread.start()

                            join_interval_seconds = 10
                            while transcribe_thread.is_alive():
                                transcribe_thread.join(timeout=join_interval_seconds)
                                if not transcribe_thread.is_alive():
                                    break
                                # Check if current task has been processing > 2 min -> mark failed and restart queue for unprocessed
                                now_utc = datetime.now(timezone.utc)
                                if task.processing_started_at and (now_utc - task.processing_started_at).total_seconds() >= self.processing_timeout_seconds:
                                    error_logger.warning(
                                        f"Task {task.file_path} stuck in processing for > {self.processing_timeout_seconds}s, marking as failed and re-queuing unprocessed"
                                    )
                                    with self.upload_processor_lock:
                                        task.status = "failed"
                                        task.error = f"Processing stuck for more than {self.processing_timeout_seconds} seconds - queue restarted"
                                        task.processing_started_at = None
                                    self._update_recording_status(task.file_path, 'failed')
                                    transcription_logger.info("2 min cycle: marked long-stuck task as failed, re-queuing pending for unprocessed")
                                    self._requeue_stuck_pending_tasks()
                                    break

                            transcription = None
                            try:
                                # If we broke out due to 2-min timeout, task is already failed
                                if task.status == "failed":
                                    transcription_logger.warning(f"Task {task.file_path} was marked as failed (2 min timeout), skipping save")
                                    continue
                                transcription = result_holder[0]
                                if error_holder[0]:
                                    raise error_holder[0]
                                
                                # Check again after thread finished (could have been marked by _check_long_stuck_tasks)
                                if task.status == "failed":
                                    transcription_logger.warning(f"Task {task.file_path} was marked as failed during transcription, skipping save")
                                    continue
                                
                                transcription_logger.debug(f"Transcription completed for uploaded file: {task.file_path}")
                                
                                if not isinstance(transcription, str):
                                    raise RuntimeError("Transcription did not return text")
                                if not channel.save_recording(
                                    task.file_path, task.timestamp, transcription
                                ):
                                    raise RuntimeError("Failed to save transcription")
                                self._update_recording_status(task.file_path, 'transcribed')
                                task.status = "completed"
                                task.transcription = transcription
                                task.processing_started_at = None
                                task.completed_at = datetime.now(timezone.utc).isoformat()
                            except Exception as trans_error:
                                # Check if task was already marked as failed due to timeout
                                if task.status != "failed":
                                    raise trans_error
                            
                    except TranscriptionAuthenticationError as error:
                        error_logger.error(
                            "Stopping transcription queue: %s", error
                        )
                        with self.upload_processor_lock:
                            task.status = 'pending'
                            task.error = str(error)
                            task.processing_started_at = None
                            self.running = False
                            self.queue_error = {
                                'type': 'authentication',
                                'message': str(error),
                            }
                            self.upload_queue.put(task)
                            self._pending_filenames_in_queue.add(task.file_path)
                        self._update_recording_status(task.file_path, 'pending')
                        _settings_manager.set_setting(
                            'transcription_queue_enabled', False
                        )
                    except Exception as e:
                        error_logger.error(f"Error processing upload: {str(e)}")
                        # Only update if not already failed due to timeout
                        if task.status != "failed":
                            # Update status in database
                            self._update_recording_status(task.file_path, 'failed')
                            task.status = "failed"
                            task.error = str(e)
                        task.processing_started_at = None
                    
                    finally:
                        self.upload_queue.task_done()
                        if task.status in ('completed', 'failed'):
                            self._move_task_to_history_and_remove_from_current(task.file_path, task.status)
                        else:
                            self._save_current_queue()
                        
            except Exception as e:
                error_logger.error(f"Error in upload queue processor: {str(e)}")
            time.sleep(0.1)

    def get_upload_status(self, filename):
        """Get the status of an uploaded file's processing."""
        with self.upload_processor_lock:
            task = self.upload_tasks.get(filename)
            if task is None:
                matches = [candidate for candidate in self.upload_tasks.values()
                           if os.path.basename(candidate.file_path) == filename]
                task = matches[0] if len(matches) == 1 else None
            if task:
                return {
                    'filename': filename,
                    'status': task.status,
                    'timestamp': task.timestamp,
                    'transcription': task.transcription if task.status == "completed" else None,
                    'error': task.error if task.status == "failed" else None
                }
            return None

    def _read_current_queue_json(self):
        """Read current_queue.json and return dict (tasks, queue_size, etc.). Data for Transcription Engine comes from this file."""
        try:
            if not CURRENT_QUEUE_JSON.exists():
                return {'tasks': {}, 'queue_size': 0, 'total_tasks': 0, 'is_running': False}
            with open(CURRENT_QUEUE_JSON, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            error_logger.error(f"Error reading current_queue.json: {str(e)}")
            return {'tasks': {}, 'queue_size': 0, 'total_tasks': 0, 'is_running': False}

    def _read_queue_history_json(self):
        """Read queue_history.json and return list of entries. Data for Transcription Engine comes from this file."""
        try:
            if not QUEUE_HISTORY_JSON.exists():
                return []
            with open(QUEUE_HISTORY_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get('entries', [])
        except Exception as e:
            error_logger.error(f"Error reading queue_history.json: {str(e)}")
            return []

    def get_queue_logs(self, status_filter=None, limit=None, page=1, date_filter=None):
        """
        Get queue logs from current_queue.json + queue_history.json (source of truth for Transcription Engine UI).
        """
        tasks_list = []
        pending_count = 0
        processing_count = 0
        completed_count = 0
        failed_count = 0

        now = datetime.now(timezone.utc)
        date_cutoff = None
        if date_filter == 'today':
            date_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif date_filter == 'week':
            date_cutoff = now - timedelta(days=7)
        elif date_filter == 'month':
            date_cutoff = now - timedelta(days=30)

        # Current queue from current_queue.json
        current_data = self._read_current_queue_json()
        tasks_data = current_data.get('tasks', {})
        for filename, task_dict in tasks_data.items():
            status = task_dict.get('status', 'pending')
            if status_filter and status != status_filter:
                continue
            task_created = task_dict.get('created_at', '')
            if date_cutoff and task_created:
                try:
                    created_dt = datetime.fromisoformat(task_created.replace('Z', '+00:00')) if isinstance(task_created, str) else task_created
                    if created_dt < date_cutoff:
                        continue
                except Exception:
                    pass
            task_info = {
                'filename': filename,
                'file_path': task_dict.get('file_path', ''),
                'channel_id': task_dict.get('channel_id', 0),
                'timestamp': task_dict.get('timestamp', ''),
                'status': status,
                'is_duplicate': task_dict.get('is_duplicate', False),
                'transcription': task_dict.get('transcription') if status == 'completed' else None,
                'error': task_dict.get('error') if status == 'failed' else None,
                'created_at': task_created,
                'completed_at': task_dict.get('completed_at'),
                'transcription_method': task_dict.get('transcription_method'),
                'transcription_model': task_dict.get('transcription_model'),
                'source': 'current',
            }
            tasks_list.append(task_info)
            if status == 'pending':
                pending_count += 1
            elif status == 'processing':
                processing_count += 1
            elif status == 'completed':
                completed_count += 1
            elif status == 'failed':
                failed_count += 1

        # History from queue_history.json
        history_entries = self._read_queue_history_json()
        for entry in history_entries:
            filename = entry.get('filename', '')
            task_dict = entry.get('task', {})
            resolution = entry.get('resolution', 'completed')
            resolved_at = entry.get('resolved_at', '')
            task_created = task_dict.get('created_at', resolved_at)
            if date_cutoff and task_created:
                try:
                    created_dt = datetime.fromisoformat(task_created.replace('Z', '+00:00')) if isinstance(task_created, str) else task_created
                    if created_dt < date_cutoff:
                        continue
                except Exception:
                    pass
            status = resolution if resolution == 'killed' else task_dict.get('status', resolution)
            if status_filter:
                if resolution == 'killed' and status_filter != 'failed':
                    continue
                if resolution != 'killed' and resolution != status_filter:
                    continue
            task_info = {
                'filename': filename,
                'file_path': task_dict.get('file_path', ''),
                'channel_id': task_dict.get('channel_id', 0),
                'timestamp': task_dict.get('timestamp', ''),
                'status': status,
                'is_duplicate': task_dict.get('is_duplicate', False),
                'transcription': task_dict.get('transcription'),
                'error': task_dict.get('error'),
                'created_at': task_created,
                'completed_at': task_dict.get('completed_at') or resolved_at,
                'transcription_method': task_dict.get('transcription_method'),
                'transcription_model': task_dict.get('transcription_model'),
                'source': 'history',
                'resolved_at': resolved_at,
            }
            tasks_list.append(task_info)
            if task_info['status'] == 'completed':
                completed_count += 1
            elif task_info['status'] == 'failed':
                failed_count += 1

        tasks_list.sort(key=lambda x: x.get('created_at', ''), reverse=True)

        per_page = limit if limit and limit > 0 else 50
        total_tasks = len(tasks_list)
        total_pages = (total_tasks + per_page - 1) // per_page if per_page > 0 else 1
        page = max(1, min(page, total_pages)) if total_pages > 0 else 1
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_tasks = tasks_list[start_idx:end_idx]

        queue_size = current_data.get('queue_size', 0)
        total_current = len(tasks_data)
        with self.upload_processor_lock:
            is_running = self.running

        return {
            'queue_size': queue_size,
            'total_tasks': total_current + len(history_entries),
            'pending': pending_count,
            'processing': processing_count,
            'completed': completed_count,
            'failed': failed_count,
            'is_running': is_running,
            'queue_error': current_data.get('queue_error') or self.queue_error,
            'tasks': paginated_tasks,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_tasks,
                'total_pages': total_pages
            },
            'filter': status_filter,
            'date_filter': date_filter,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'source': 'current_queue.json + queue_history.json',
        }

    def get_queue_status(self):
        """
        Get queue status from current_queue.json + queue_history.json (source of truth for Transcription Engine UI).
        If there are pending tasks but the processor thread is dead, start it.
        """
        current_data = self._read_current_queue_json()
        tasks_data = current_data.get('tasks', {})
        pending_count = sum(1 for t in tasks_data.values() if t.get('status') == 'pending')
        processing_count = sum(1 for t in tasks_data.values() if t.get('status') == 'processing')
        queue_size = current_data.get('queue_size', 0)
        total_current = len(tasks_data)

        history_entries = self._read_queue_history_json()
        completed_count = sum(1 for e in history_entries if e.get('resolution') == 'completed')
        failed_count = sum(1 for e in history_entries if e.get('resolution') in ('failed', 'killed'))

        with self.upload_processor_lock:
            is_running = self.running

        result = {
            'queue_size': queue_size,
            'total_tasks': total_current + len(history_entries),
            'pending': pending_count,
            'processing': processing_count,
            'completed': completed_count,
            'failed': failed_count,
            'is_running': is_running,
            'queue_error': current_data.get('queue_error') or self.queue_error,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'source': 'current_queue.json + queue_history.json',
        }
        return result

    def stop_queue(self):
        """Stop the transcription queue processor only (so user can stop/start from UI). Processor thread will exit on next loop check."""
        try:
            self.running = False
            self.queue_error = None
            self._save_current_queue()
            db_logger.info("Transcription queue stopped by user")
        except Exception as e:
            error_logger.error(f"Error stopping transcription queue: {str(e)}")

    def stop(self):
        """Stop all threads."""
        try:
            self.running = False
            # Shutdown transcription service to cleanup model and background thread
            if hasattr(self, 'transcription_service'):
                self.transcription_service.shutdown()
            for thread in self.threads:
                thread.join(timeout=1.0)
            db_logger.info("MultiChannelAudioHandler stopped successfully")
        except Exception as e:
            error_logger.error(f"Error stopping MultiChannelAudioHandler: {str(e)}")

    def get_all_recordings(self):
        """Get all recordings across all channels without preference filtering."""
        with self.db_lock:
            conn = sqlite3.connect(_get_db_path())
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, channel_id, filename, timestamp, transcription,
                           status, is_duplicate, duration, filesize,
                           is_hallucination, updated_at
                    FROM recordings
                    ORDER BY timestamp DESC
                ''')
                recordings = cursor.fetchall()
                
                filtered_recordings = []
                for row in recordings:
                    transcription = row[4]
                    is_duplicate = row[6]
                    duration = row[7] if len(row) > 7 else None
                    filesize = row[8] if len(row) > 8 else None
                    
                    filtered_recordings.append({
                        'id': row[0],
                        'channel_id': row[1],
                        'filename': row[2],
                        'timestamp': row[3],
                        'status': row[5],
                        'transcription': transcription,
                        'is_duplicate': bool(is_duplicate),
                        'duration': duration,
                        'filesize': filesize,
                        'is_hallucination': bool(row[9]),
                        'updated_at': row[10],
                    })
                
                return filtered_recordings
            
            except sqlite3.Error as e:
                error_logger.error(f"Error retrieving all recordings: {str(e)}")
                return []
            finally:
                conn.close()

    def get_channel_recordings(self, channel_id):
        """Get recordings for a specific channel."""
        channel = self.get_or_create_channel(channel_id)
        return channel.get_recordings()

# Singleton instance
_audio_handler = None
_audio_handler_lock = threading.Lock()


def get_audio_handler():
    """Get the singleton audio handler instance, initializing if necessary."""
    global _audio_handler
    if _audio_handler is None:
        with _audio_handler_lock:
            if _audio_handler is None:
                try:
                    _audio_handler = _create_audio_handler()
                except Exception as e:
                    error_logger.error(f"Failed to initialize audio handler in get_audio_handler: {str(e)}")
                    raise
    return _audio_handler


def _create_audio_handler():
    """Internal factory — must be called while holding _audio_handler_lock."""
    settings = load_settings()
    model_name = settings.get("model", "small")
    transcribe_method = settings.get("method", "local")
    if transcribe_method not in {"local", "openai"}:
        transcribe_method = "local"

    handler = MultiChannelAudioHandler(
        model_name=model_name,
        transcribe_method=transcribe_method,
    )
    handler._load_current_queue()
    db_logger.info("Audio handler initialized (queue engine can now be started)")
    return handler


def reload_transcription_settings():
    """
    Reload the transcription method and API key from settings
    without restarting the Python application.

    This allows switching between Boondock API and local transcription
    from the UI at runtime.
    """
    global _audio_handler
    try:
        if _audio_handler is None:
            # Handler not initialized yet; nothing to reload
            return

        settings = load_settings()
        method = settings.get("method", "local")
        _audio_handler.transcribe_method = method if method in {"local", "openai"} else "local"

        db_logger.info(
            f"Reloaded transcription settings at runtime: "
            f"method={_audio_handler.transcribe_method}"
        )
    except Exception as e:
        error_logger.error(f"Failed to reload transcription settings: {str(e)}")
