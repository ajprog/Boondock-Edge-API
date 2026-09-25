"""
Recording management routes.
Handles audio file uploads, recordings retrieval, serving, and deletion.
"""
import os
import sqlite3
import logging
import wave
import re
from config import DATA_ROOT
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request, send_from_directory, send_file, abort
from flasgger import swag_from
from tempfile import NamedTemporaryFile

from app.services.audio_handler import get_audio_handler
from app.utils.crc_utils import check_and_update_duplicate_cache
from ..utils.logging_setup import error_logger
from ..middleware.auth_middleware import require_admin, require_permission
from ..routes.route_utils import (
    RECORDINGS_DIR,
    DB_PATH,
    get_channel_details,
    db_lock,
    calculate_wav_duration,
)
from ..services.settings_manager import get_settings_manager
from ..services.channel_state import load_owned_recordings, load_request_recording
from ..services.device_health_monitor import (
    track_device_created,
    track_file_upload,
    track_error
)

_settings_manager = get_settings_manager()

recordings_bp = Blueprint('recordings', __name__)


def _resolve_recording_path(filename):
    """Resolve a stored recording path without allowing it to escape recordings/."""
    path = (DATA_ROOT / filename).resolve()
    return path if path.is_relative_to(RECORDINGS_DIR) else None

@recordings_bp.route('/uploads/queue', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Audio'],
    'summary': 'Queue audio file for processing',
    'parameters': [
        {
            'name': 'relative_path',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Relative path to the audio file'
        },
        {
            'name': 'channel_id',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Channel ID'
        },
        {
            'name': 'mac',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'MAC address'
        }
    ],
    'responses': {
        '200': {'description': 'File queued successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Internal server error'}
    }
})
def upload_audio_queue():
    """Handle audio file uploads with real-time channel MAC address fetching."""
    try:
        mac = request.args.get('mac')  # Fetch MAC from query parameters
        relative_path = request.args.get('relative_path')  # Fetch from query parameters
        channel_id = request.args.get('channel_id')  # Fetch from query parameters

        # Validate required parameters
        if not relative_path or not channel_id:
            return jsonify({'error': 'Missing required parameters'}), 400

        # Get audio handler instance and queue the file for processing
        audio_handler = get_audio_handler()
        success, result = audio_handler.queue_upload_for_processing(relative_path, channel_id)

        if success:
            return jsonify({'message': 'OK'}), 200
        
        return jsonify({'error': f'Error queueing file: {result}'}), 500

    except Exception as e:
        error_logger.error(f"Error in upload_audio_queue: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@recordings_bp.route('/uploads/<filename>/status', methods=['GET'])
@require_admin()
@swag_from({
    'tags': ['Audio'],
    'summary': 'Get upload processing status',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Filename to check status for'
        }
    ],
    'responses': {
        '200': {'description': 'Status retrieved successfully'},
        '404': {'description': 'File not found'},
        '500': {'description': 'Internal server error'}
    }
})
def get_upload_status(filename):
    """Get the processing status of an uploaded file."""
    try:
        audio_handler = get_audio_handler()
        status = audio_handler.get_upload_status(filename)
        
        if status is None:
            return jsonify({'error': 'File not found'}), 404
            
        return jsonify(status), 200
        
    except Exception as e:
        error_logger.error(f"Error getting upload status: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


# Queue management routes - must be defined before dynamic routes
@recordings_bp.route('/queue/status', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Get transcription queue status summary',
    'responses': {
        '200': {'description': 'Queue status retrieved successfully'},
        '500': {'description': 'Internal server error'}
    }
})
def get_queue_status():
    """Get current transcription queue status summary."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            return jsonify({
                'queue_size': 0,
                'total_tasks': 0,
                'pending': 0,
                'processing': 0,
                'completed': 0,
                'failed': 0,
                'is_running': False
            }), 200
        
        queue_status = audio_handler.get_queue_status()
        return jsonify(queue_status), 200
        
    except Exception as e:
        error_logger.error(f"Error getting queue status: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500


@recordings_bp.route('/queue/start', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Start the transcription queue processor',
    'responses': {
        '200': {'description': 'Queue started successfully'},
        '500': {'description': 'Internal server error'}
    }
})
def start_queue():
    """Start the transcription queue so it processes pending tasks."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            return jsonify({'error': 'Audio handler not initialized'}), 500
        # Avoid starting multiple processor threads; if already running, just report status
        if getattr(audio_handler, "running", False):
            response = jsonify({'message': 'Transcription queue already running', 'is_running': True})
            response.headers['Content-Type'] = 'application/json'
            return response, 200
        audio_handler.start()
        _settings_manager.set_setting('global_transcription_queue_enabled', True)
        response = jsonify({'message': 'Transcription queue started', 'is_running': True})
        response.headers['Content-Type'] = 'application/json'
        return response, 200
    except Exception as e:
        error_logger.error(f"Error starting queue: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/queue/stop', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Stop the transcription queue processor',
    'responses': {
        '200': {'description': 'Queue stopped successfully'},
        '500': {'description': 'Internal server error'}
    }
})
def stop_queue():
    """Stop the transcription queue so it no longer processes tasks."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            return jsonify({'error': 'Audio handler not initialized'}), 500
        audio_handler.stop_queue()
        _settings_manager.set_setting('global_transcription_queue_enabled', False)
        response = jsonify({'message': 'Transcription queue stopped', 'is_running': False})
        response.headers['Content-Type'] = 'application/json'
        return response, 200
    except Exception as e:
        error_logger.error(f"Error stopping queue: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/queue/logs', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Get transcription queue logs',
    'parameters': [
        {
            'name': 'status',
            'in': 'query',
            'type': 'string',
            'required': False,
            'enum': ['pending', 'processing', 'completed', 'failed'],
            'description': 'Filter logs by status'
        },
        {
            'name': 'limit',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Limit number of results per page (default: 50)'
        },
        {
            'name': 'page',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Page number (default: 1)'
        },
        {
            'name': 'date_filter',
            'in': 'query',
            'type': 'string',
            'required': False,
            'enum': ['today', 'week', 'month'],
            'description': 'Filter by date range'
        }
    ],
    'responses': {
        '200': {'description': 'Queue logs retrieved successfully'},
        '500': {'description': 'Internal server error'}
    }
})
def get_queue_logs():
    """Get transcription queue logs with optional filtering, pagination, and date filtering."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            return jsonify({
                'error': 'Audio handler not initialized',
                'queue_size': 0,
                'total_tasks': 0,
                'pending': 0,
                'processing': 0,
                'completed': 0,
                'failed': 0,
                'is_running': False,
                'tasks': []
            }), 200
        
        # Get query parameters
        status_filter = request.args.get('status', None)
        limit = request.args.get('limit', None, type=int)
        page = request.args.get('page', 1, type=int)
        date_filter = request.args.get('date_filter', None)
        
        # Validate status filter
        if status_filter and status_filter not in ['pending', 'processing', 'completed', 'failed']:
            return jsonify({'error': f'Invalid status filter: {status_filter}'}), 400
        
        # Validate date filter
        if date_filter and date_filter not in ['today', 'week', 'month']:
            return jsonify({'error': f'Invalid date filter: {date_filter}'}), 400
        
        queue_logs = audio_handler.get_queue_logs(
            status_filter=status_filter, 
            limit=limit, 
            page=page,
            date_filter=date_filter
        )
        response = jsonify(queue_logs)
        response.headers['Content-Type'] = 'application/json'
        return response, 200
        
    except Exception as e:
        error_logger.error(f"Error getting queue logs: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        response = jsonify({'error': f'Internal server error: {str(e)}'})
        response.headers['Content-Type'] = 'application/json'
        return response, 500


@recordings_bp.route('/queue/kill/<filename>', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Kill a processing task and mark it as failed',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Filename of the task to kill'
        }
    ],
    'responses': {
        '200': {'description': 'Task killed successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Task not found'},
        '500': {'description': 'Internal server error'}
    }
})
def kill_task(filename):
    """Kill a processing transcription task and mark it as failed."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            response = jsonify({'error': 'Audio handler not initialized'})
            response.headers['Content-Type'] = 'application/json'
            return response, 500
        
        success, message = audio_handler.kill_task(filename)
        
        if success:
            response = jsonify({'message': message})
            response.headers['Content-Type'] = 'application/json'
            return response, 200
        else:
            status_code = 404 if 'not found' in message.lower() else 400
            response = jsonify({'error': message})
            response.headers['Content-Type'] = 'application/json'
            return response, status_code
            
    except Exception as e:
        error_logger.error(f"Error killing task: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        response = jsonify({'error': f'Internal server error: {str(e)}'})
        response.headers['Content-Type'] = 'application/json'
        return response, 500


@recordings_bp.route('/queue/requeue/<filename>', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Requeue a failed or stuck task',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Filename of the task to requeue'
        }
    ],
    'responses': {
        '200': {'description': 'Task requeued successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Task not found'},
        '500': {'description': 'Internal server error'}
    }
})
def requeue_task(filename):
    """Requeue a failed or stuck transcription task."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            response = jsonify({'error': 'Audio handler not initialized'})
            response.headers['Content-Type'] = 'application/json'
            return response, 500
        
        success, message = audio_handler.requeue_task(filename)
        
        if success:
            response = jsonify({'message': message})
            response.headers['Content-Type'] = 'application/json'
            return response, 200
        else:
            status_code = 404 if 'not found' in message.lower() else 400
            response = jsonify({'error': message})
            response.headers['Content-Type'] = 'application/json'
            return response, status_code
            
    except Exception as e:
        error_logger.error(f"Error requeueing task: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        response = jsonify({'error': f'Internal server error: {str(e)}'})
        response.headers['Content-Type'] = 'application/json'
        return response, 500


@recordings_bp.route('/queue/purge', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Queue'],
    'summary': 'Purge queue logs',
    'parameters': [
        {
            'name': 'status',
            'in': 'query',
            'type': 'string',
            'required': False,
            'enum': ['completed', 'failed'],
            'description': 'Only purge tasks with this status'
        },
        {
            'name': 'date_filter',
            'in': 'query',
            'type': 'string',
            'required': False,
            'enum': ['today', 'week', 'month'],
            'description': 'Filter by date range'
        },
        {
            'name': 'older_than_days',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Purge tasks older than N days'
        }
    ],
    'responses': {
        '200': {'description': 'Logs purged successfully'},
        '500': {'description': 'Internal server error'}
    }
})
def purge_queue_logs():
    """Purge queue logs based on filters."""
    try:
        audio_handler = get_audio_handler()
        if not audio_handler:
            response = jsonify({'error': 'Audio handler not initialized'})
            response.headers['Content-Type'] = 'application/json'
            return response, 500
        
        status_filter = request.args.get('status', None)
        date_filter = request.args.get('date_filter', None)
        older_than_days = request.args.get('older_than_days', None, type=int)
        
        result = audio_handler.purge_queue_logs(
            status_filter=status_filter,
            date_filter=date_filter,
            older_than_days=older_than_days
        )
        
        response = jsonify(result)
        response.headers['Content-Type'] = 'application/json'
        return response, 200
        
    except Exception as e:
        error_logger.error(f"Error purging queue logs: {str(e)}")
        import traceback
        error_logger.error(traceback.format_exc())
        response = jsonify({'error': f'Internal server error: {str(e)}'})
        response.headers['Content-Type'] = 'application/json'
        return response, 500


@recordings_bp.route('/recordings')
@require_permission(['recording.read'], loader=load_owned_recordings, inject_as='recordings')
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get all recordings',
    'responses': {
        '200': {'description': 'List of all recordings'}
    }
})
def get_recordings(recordings):
    return jsonify(recordings)


@recordings_bp.route('/recordings/inbox', methods=['GET'])
@recordings_bp.route('/recordings/inbox/range', methods=['GET'])
@require_permission(['recording.read'], loader=load_owned_recordings, inject_as='recordings')
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Retrieve inbox recordings',
    'description': 'Get a bounded inbox window of recordings',
    'parameters': [
        {
            'name': 'limit',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Maximum number of recordings to return (default 1000, max 5000)'
        },
        {
            'name': 'since_timestamp',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Lower bound timestamp (inclusive), format YYYYMMDD_HHMMSS'
        },
        {
            'name': 'before_timestamp',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Keyset upper bound timestamp (exclusive), format YYYYMMDD_HHMMSS'
        },
        {
            'name': 'before_id',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Tie-breaker ID for before_timestamp keyset paging'
        }
    ],
    'responses': {
        '200': {'description': 'Inbox recordings window'},
        '500': {'description': 'Server error'}
    }
})
def get_recordings_inbox(recordings):
    try:
        limit = request.args.get('limit', default=1000, type=int) or 1000
        limit = max(1, min(limit, 5000))

        has_more = len(recordings) > limit
        recordings = recordings[:limit]
        last = recordings[-1] if recordings else None

        return jsonify({
            'recordings': recordings,
            'meta': {
                'limit': limit,
                'returned': len(recordings),
                'has_more': has_more,
                'next_before_timestamp': last.get('timestamp') if has_more and last else None,
                'next_before_id': last.get('id') if has_more and last else None,
            }
        }), 200
    except Exception as e:
        error_logger.error(f"Error getting inbox recordings window: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500



@recordings_bp.route('/recordings/inbox/count', methods=['GET'])
@require_permission(['recording.read'], loader=load_owned_recordings, inject_as='recordings')
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Total inbox count for a time window',
    'description': (
        'Returns the total number of recordings that match the given time window/filters. '
        'Used by the dashboard footer so it can show "Showing X-Y of <real total>" without '
        'having to load every row first.'
    ),
    'parameters': [
        {
            'name': 'since_timestamp',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Lower bound timestamp (inclusive), format YYYYMMDD_HHMMSS'
        },
        {
            'name': 'before_timestamp',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Upper bound timestamp (exclusive), format YYYYMMDD_HHMMSS'
        },
        {
            'name': 'before_id',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Tie-breaker ID for before_timestamp'
        }
    ],
    'responses': {
        '200': {'description': 'Total count for the window'},
        '500': {'description': 'Server error'}
    }
})
def get_recordings_inbox_count(recordings):
    """Return total recordings count matching the inbox window filters."""
    return jsonify({'total': len(recordings)}), 200


@recordings_bp.route('/recordings/<path:filename>')
@require_permission(
    ['recording.read'], loader=load_request_recording,
    id_argument='filename', inject_as='recording'
)
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Serve audio file',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Path to audio file (supports both old channel_X/audio_*.wav and new MAC/YYYY/MM/DD/*.wav structures)'
        }
    ],
    'responses': {
        '200': {'description': 'Audio file'},
        '404': {'description': 'File not found'}
    }
})
def serve_audio(filename, recording):
    file_path = _resolve_recording_path(recording.get('filename'))
    if file_path is None or not file_path.is_file():
        return abort(404, description="Audio file not found")

    return send_from_directory(file_path.parent, file_path.name)


@recordings_bp.route('/recordings/<int:recording_id>', methods=['DELETE'])
@require_permission(
    ['recording.delete'], loader=load_request_recording,
    id_argument='recording_id', inject_as='recording'
)
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Delete a recording',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'Recording deleted successfully'},
        '404': {'description': 'Recording not found'},
        '500': {'description': 'Database error'}
    }
})
def delete_recording(recording_id, recording):
    """Delete a specific recording by ID."""
    conn = None
    try:
        filename = recording.get('filename')
        file_path = _resolve_recording_path(filename) if filename else None

        if filename and file_path is None:
            return jsonify({"error": "Invalid recording file path"}), 400

        # Commit the database deletion before touching the filesystem. This avoids
        # leaving a live database row pointing at a file that was already removed
        # if the database operation fails.
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        conn.commit()

        file_deleted = False
        if file_path and file_path.is_file():
            try:
                file_path.unlink()
                file_deleted = True
            except OSError as e:
                logging.error(
                    "Deleted recording %s from the database but failed to delete %s: %s",
                    recording_id,
                    file_path,
                    e,
                )
                return jsonify({
                    "error": "Recording deleted from database, but audio file deletion failed",
                    "recording_id": recording_id,
                }), 500

        return jsonify({
            "message": "Recording deleted successfully",
            "file_deleted": file_deleted,
        }), 200
    except sqlite3.Error as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()


@recordings_bp.route('/audio_url/<int:recording_id>', methods=['GET'])
@require_permission(
    ['recording.read'], loader=load_request_recording,
    id_argument='recording_id', inject_as='recording'
)
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get audio URL for a recording',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'Audio URL information'},
        '404': {'description': 'Recording not found'},
        '500': {'description': 'Server error'}
    }
})
def get_audio_url(recording_id, recording):
    """
    Return audio URL information for a recording.
    Responds with the audio file as an attachment if found.
    """
    try:
        filename = recording.get('filename')
        full_path = _resolve_recording_path(filename)

        if full_path is None or not full_path.is_file():
            return jsonify({'error': 'Audio file not found'}), 404

        channel_name = recording.get('channel_name') or 'Audio'

        # Return both the file (as attachment) and the OS full path in JSON
        # If you want to send the file, use send_file; if you want to send JSON, just return the path.
        # Here, let's return JSON with the path and a download URL.
        download_url = f"/{filename.replace(os.sep, '/')}"
        return jsonify({
            'message_id': recording_id,
            'filename': filename,
            'download_url': download_url,
            'channel_name': channel_name
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/audio_url_file/<int:recording_id>', methods=['GET'])
@require_permission(
    ['recording.read'], loader=load_request_recording,
    id_argument='recording_id', inject_as='recording'
)
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Download audio file for a recording',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'Audio file as attachment'},
        '404': {'description': 'Recording not found'},
        '500': {'description': 'Server error'}
    }
})
def get_audio_url_file(recording_id, recording):
    """
    Return the audio file for a recording.
    Responds with the audio file as an attachment if found.
    The downloaded filename uses the recording's UTC timestamp.
    """
    try:
        filename = recording.get('filename')
        full_path = _resolve_recording_path(filename)

        if full_path is None or not full_path.is_file():
            return jsonify({'error': 'Audio file not found'}), 404

        channel_name = recording.get('channel_name') or 'Audio'
        
        # Sanitize channel name to be filename-safe
        safe_channel_name = "".join(c for c in channel_name if c.isalnum() or c in (' ', '-', '_')).rstrip()

        # Extract the UTC timestamp from the filename
        download_filename = None
        try:
            # Get the base filename (e.g., "2026-01-13-07-03-00.wav")
            base_filename = full_path.name
            
            # Try to parse UTC timestamp from filename (format: YYYY-MM-DD-HH-MM-SS.wav)
            # This matches the new file structure: recordings/MAC/YYYY/MM/DD/YYYY-MM-DD-HH-MM-SS.wav
            match = re.match(r'(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.wav$', base_filename)
            if match:
                # Parse UTC timestamp
                utc_timestamp_str = match.group(1)
                parts = utc_timestamp_str.split('-')
                if len(parts) == 6:
                    year, month, day, hour, minute, second = map(int, parts)
                    # Create UTC datetime object
                    utc_dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                    
                    download_filename = f'{safe_channel_name}_{utc_dt.strftime("%Y-%m-%d-%H-%M-%S")}.wav'
                else:
                    download_filename = f'{safe_channel_name}_{base_filename}'
            else:
                download_filename = f'{safe_channel_name}_{base_filename}'
        except Exception as e:
            error_logger.warning(f"Error parsing timestamp from filename {filename}: {e}, using original filename")
            download_filename = f'{safe_channel_name}_{os.path.basename(filename)}'

        # Send the file as an attachment with converted filename
        response = send_file(full_path, as_attachment=True, download_name=download_filename)

        # Add extra info in headers for client use
        response.headers['X-Message-Id'] = str(recording_id)
        response.headers['X-Filename'] = filename
        response.headers['X-Download-Filename'] = download_filename
        response.headers['X-Download-Url'] = f"/api/recordings/{filename.replace(os.sep, '/')}"

        return response

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/recording_duration_calculate/<int:recording_id>', methods=['GET'])
@require_permission(
    ['recording.read'], loader=load_request_recording,
    id_argument='recording_id', inject_as='recording'
)
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Calculate recording duration',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'Duration information'},
        '400': {'description': 'Invalid filename format'},
        '404': {'description': 'Recording not found'},
        '500': {'description': 'Server error'}
    }
})
def get_recording_duration(recording_id, recording):
    conn = None
    try:
        stored_duration = recording.get('duration')
        filename = recording.get('filename')
        filesize = recording.get('filesize')

        duration_seconds = None
        duration_milliseconds = None

        # If duration is already in database, use it
        if stored_duration is not None and stored_duration > 0:
            duration_seconds = float(stored_duration)
            duration_milliseconds = int(duration_seconds * 1000)
        else:
            # Duration not in database, try to calculate from filesize
            if filesize and filesize > 0:
                duration_seconds = calculate_wav_duration(filesize)
                duration_milliseconds = int(duration_seconds * 1000)

                # Store duration in database for future use
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("UPDATE recordings SET duration = ? WHERE id = ?", (duration_seconds, recording_id))
                    conn.commit()
                except Exception as e:
                    error_logger.error(f"Failed to store duration in database: {e}")
            else:
                # Fallback: Calculate from audio file if filesize not available
                full_path = os.path.abspath(os.path.normpath((DATA_ROOT / filename).resolve()))

                if not os.path.exists(full_path):
                    return jsonify({'error': 'Audio file not found'}), 404

                try:
                    with wave.open(full_path, 'rb') as audio_file:
                        frames = audio_file.getnframes()
                        rate = audio_file.getframerate()
                        duration_seconds = frames / float(rate)
                        duration_milliseconds = int(duration_seconds * 1000)

                    # Store duration and filesize in database for future use
                    file_size = os.path.getsize(full_path)
                    try:
                        if conn is None:
                            conn = sqlite3.connect(DB_PATH)
                        conn.execute(
                            "UPDATE recordings SET duration = ?, filesize = ? WHERE id = ?",
                            (duration_seconds, file_size, recording_id),
                        )
                        conn.commit()
                    except Exception as e:
                        error_logger.error(f"Failed to store duration/filesize in database: {e}")
                except Exception as e:
                    error_logger.error(f"Failed to read audio file: {e}")
                    return jsonify({'error': f'Failed to calculate duration: {str(e)}'}), 500

        # At this point we have duration_seconds/milliseconds. Now also compute
        # a human-readable recording start/end time based on the UTC filename.
        start_time_str = None
        end_time_str = None

        try:
            # filename is a relative path like 'recordings/<MAC>/YYYY/MM/DD/YYYY-MM-DD-HH-MM-SS.wav'
            base_filename = os.path.basename(filename)

            # Parse UTC timestamp from filename (format: YYYY-MM-DD-HH-MM-SS.wav)
            match = re.match(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.wav$', base_filename)
            if match and duration_seconds is not None:
                year, month, day, hour, minute, second = map(int, match.groups())
                utc_dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

                utc_end = utc_dt + timedelta(seconds=duration_seconds)
                start_time_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                end_time_str = utc_end.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception as e:
            # Don't fail the endpoint if time parsing fails – just log and return duration only
            error_logger.warning(f"Failed to derive recording start/end time from filename {filename}: {e}")

        response_data = {
            'duration_seconds': round(duration_seconds, 3) if duration_seconds is not None else None,
            'duration_milliseconds': duration_milliseconds,
        }

        # Include extra fields only when we could compute them
        if start_time_str:
            response_data['start_time'] = start_time_str
        if end_time_str:
            response_data['end_time'] = end_time_str
        return jsonify(response_data), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@recordings_bp.route('/truncate_recordings', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Truncate recordings table',
    'responses': {
        '200': {'description': 'Recordings table truncated successfully'},
        '401': {'description': 'Authentication required'},
        '403': {'description': 'Admin access required'},
        '500': {'description': 'Database error'}
    }
})
def truncate_recordings():
    """API route to truncate the recordings table and delete all audio files."""
    import shutil
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            
            # Get all filenames before deleting from database
            cursor.execute('SELECT filename FROM recordings')
            filenames = [row[0] for row in cursor.fetchall()]
            
            # Delete all rows from the database
            cursor.execute('DELETE FROM recordings')
            conn.commit()
            
            # Delete the actual audio files
            deleted_count = 0
            errors = []
            for filename in filenames:
                try:
                    if filename:
                        # filename is relative path like 'recordings/MAC/YYYY/MM/DD/file.wav'
                        file_path = _resolve_recording_path(filename)
                        if file_path is None:
                            errors.append(f"Refused to delete invalid recording path: {filename}")
                        elif file_path.exists():
                            file_path.unlink()
                            deleted_count += 1
                except Exception as e:
                    errors.append(f"Failed to delete {filename}: {str(e)}")
            
            # Clean up empty directories in recordings folder
            try:
                directories = [p for p in RECORDINGS_DIR.rglob("*") if p.is_dir()]

                for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                    try:
                        directory.rmdir()
                        logging.debug("Removed empty directory: %s", directory)
                    except OSError:
                        pass
            except Exception as e:
                errors.append(f"Error cleaning up directories: {str(e)}")
            
            message = f"Recordings table truncated successfully. Deleted {deleted_count} audio file(s)."
            if errors:
                message += f" Warnings: {len(errors)} file(s) had deletion issues."
            
            return jsonify({
                "message": message,
                "deleted_files": deleted_count,
                "warnings": errors if errors else None
            }), 200
            
        except sqlite3.Error as e:
            conn.rollback()
            return jsonify({"error": f"Failed to truncate recordings table: {str(e)}"}), 500
        except Exception as e:
            conn.rollback()
            return jsonify({"error": f"Failed to delete recordings: {str(e)}"}), 500
        finally:
            conn.close()


@recordings_bp.route('/recordings/calendar/days', methods=['GET'])
@require_permission(['recording.read'], loader=load_owned_recordings, inject_as='recordings')
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get days with recordings for a month',
    'parameters': [
        {
            'name': 'year',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Year (e.g., 2025)'
        },
        {
            'name': 'month',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Month (1-12)'
        }
    ],
    'responses': {
        '200': {'description': 'List of days with recordings'},
        '400': {'description': 'Invalid parameters'},
        '500': {'description': 'Server error'}
    }
})
def get_calendar_days(recordings):
    """Get days that have recordings for a given month."""
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    if not year or not month or month < 1 or month > 12:
        return jsonify({'error': 'Invalid year or month'}), 400

    days = set()
    for recording in recordings:
        timestamp = recording.get('timestamp')
        if timestamp and len(timestamp) >= 8:
            try:
                days.add(int(timestamp[6:8]))
            except (ValueError, IndexError):
                pass

    return jsonify({'days': sorted(days)}), 200


@recordings_bp.route('/recordings/calendar/hours', methods=['GET'])
@require_permission(['recording.read'], loader=load_owned_recordings, inject_as='recordings')
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get hours with recordings for a day',
    'parameters': [
        {
            'name': 'year',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Year (e.g., 2025)'
        },
        {
            'name': 'month',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Month (1-12)'
        },
        {
            'name': 'day',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Day (1-31)'
        }
    ],
    'responses': {
        '200': {'description': 'List of hours with recordings'},
        '400': {'description': 'Invalid parameters'},
        '500': {'description': 'Server error'}
    }
})
def get_calendar_hours(recordings):
    """Get hours that have recordings for a given day."""
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    day = request.args.get('day', type=int)

    if not year or not month or not day or month < 1 or month > 12 or day < 1 or day > 31:
        return jsonify({'error': 'Invalid year, month, or day'}), 400

    hours = set()
    for recording in recordings:
        timestamp = recording.get('timestamp')
        if timestamp and len(timestamp) >= 11:
            try:
                hours.add(int(timestamp[9:11]))
            except (ValueError, IndexError):
                pass

    return jsonify({'hours': sorted(hours)}), 200


@recordings_bp.route('/recordings/calendar/recordings', methods=['GET'])
@require_permission(['recording.read'], loader=load_owned_recordings, inject_as='recordings')
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get recordings for a specific hour',
    'parameters': [
        {
            'name': 'year',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Year (e.g., 2025)'
        },
        {
            'name': 'month',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Month (1-12)'
        },
        {
            'name': 'day',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Day (1-31)'
        },
        {
            'name': 'hour',
            'in': 'query',
            'type': 'integer',
            'required': True,
            'description': 'Hour (0-23)'
        }
    ],
    'responses': {
        '200': {'description': 'List of recordings for the hour'},
        '400': {'description': 'Invalid parameters'},
        '500': {'description': 'Server error'}
    }
})
def get_calendar_recordings(recordings):
    """Get recordings for a specific hour."""
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    day = request.args.get('day', type=int)
    hour = request.args.get('hour', type=int)

    if year is None or month is None or day is None or hour is None:
        return jsonify({'error': 'Missing required parameters'}), 400
    if month < 1 or month > 12 or day < 1 or day > 31 or hour < 0 or hour > 23:
        return jsonify({'error': 'Invalid parameters'}), 400

    result = []
    for recording in recordings:
        item = dict(recording)
        item['channel_name'] = item.get('channel_name') or f"Channel {item.get('channel_id')}"
        item['transcription'] = item.get('transcription') or ''
        item['is_duplicate'] = bool(item.get('is_duplicate'))
        filename = item.get('filename')
        item['url'] = f"/api/recordings/{filename.replace(os.sep, '/')}" if filename else None
        result.append(item)

    return jsonify({'recordings': result}), 200
