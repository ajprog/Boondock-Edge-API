"""
Transcription management routes.
Handles transcription operations including saving, retrieving, and proxy transcription.
"""
import json
import sqlite3
import logging
from config import DATA_ROOT
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from ..middleware.auth_middleware import require_permission

from ..utils.logging_setup import error_logger
from ..services.transcription_service import request_openai_transcription
from ..services.recording_state import update_transcription as persist_transcription
from ..routes.route_utils import (
    DB_PATH,
    create_history_entry,
)

transcription_bp = Blueprint('transcription', __name__)


@transcription_bp.route('/transcribe_save/<int:message_id>', methods=['POST'])
@require_permission(['transcription.update'])
@swag_from({
    'tags': ['Transcription'],
    'summary': 'Update transcription and optionally replace audio',
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'message_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'transcription',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Transcription text'
        },
        {
            'name': 'croppedAudio',
            'in': 'formData',
            'type': 'file',
            'required': False,
            'description': 'Optional cropped audio file (.wav, .ogg, .raw)'
        }
    ],
    'responses': {
        '200': {'description': 'Transcription updated successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Recording not found'}
    }
})
def update_transcription(message_id):
    """Update transcription, move original audio file to folder, and replace original with new audio.
    Supports .wav, .ogg, and .raw formats for croppedAudio.
    If croppedAudio does not exist, only update the transcription.
    """

    if not request.form and not request.files:
        return jsonify({'error': 'Missing form data or files'}), 400

    transcription = request.form.get('transcription', '').strip()
    if not transcription:
        return jsonify({'error': 'Missing transcription'}), 400

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    try:
        # Get existing filename and current transcription from DB
        cur.execute("SELECT filename, transcription FROM recordings WHERE id = ?", (message_id,))
        result = cur.fetchone()
        if not result:
            return jsonify({'error': 'Recording not found'}), 404

        filename, current_transcription = result
        # Always resolve path relative to application root
        file_path = (DATA_ROOT / filename).resolve()
        if not file_path.is_relative_to(DATA_ROOT / 'recordings'):
            return jsonify({'error': 'Invalid recording path'}), 400

        # Create history entry before making changes
        history_description = "Manual transcription update"
        
        # If croppedAudio is provided, handle audio replacement
        if 'croppedAudio' in request.files:
            if not file_path.is_file():
                return jsonify({'error': f'Original audio file not found on disk: {file_path}', 'filepath': str(file_path)}), 404

            # Save new cropped audio file to original path (replace original location)
            cropped_audio = request.files['croppedAudio']

            # Validate file type by extension (accept .wav, .ogg, .raw)
            filename = cropped_audio.filename or ''
            allowed_exts = ('.wav', '.ogg', '.raw')
            if not filename.lower().endswith(allowed_exts):
                return jsonify({'error': 'Invalid file type for croppedAudio. Must be .wav, .ogg, or .raw'}), 400

            # Create folder path: same as file but with "_FOLDER" suffix
            folder_name = f"{file_path.stem}_FOLDER"
            folder_path = file_path.parent / folder_name

            # Make the new folder if it doesn't exist
            folder_path.mkdir(parents=True, exist_ok=True)

            # Move the original file into this folder
            backup_file_path = folder_path / file_path.name
            if not backup_file_path.is_file():  # Prevent overwriting if already moved
                file_path.rename(backup_file_path)

            cropped_audio.save(file_path)
            history_description = "Audio edit and transcription update"

        # Create history entry with current state before updating
        # Always create history entry (even for first save to preserve original)
        try:
            # Pass the current audio filename to history
            current_audio_filename = filename if filename else None
            create_history_entry(message_id, current_transcription, current_audio_filename, history_description)
        except Exception as e:
            print(f"Warning: Failed to create history entry: {e}")

        # Update transcription in DB (always)
        persist_transcription(conn, message_id, transcription)
        conn.commit()

        return jsonify({
            'recording_id': message_id,
            'transcription': transcription,
            'message': 'Transcription updated' + (' and audio replaced successfully' if 'croppedAudio' in request.files else '')
        }), 200

    except sqlite3.IntegrityError:
        return jsonify({'error': 'Database integrity error'}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@transcription_bp.route('/transcribe_save/<int:message_id>', methods=['GET'])
@require_permission(['transcription.read'])
@swag_from({
    'tags': ['Transcription'],
    'summary': 'Get transcription for a recording',
    'parameters': [
        {
            'name': 'message_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'Transcription retrieved successfully'},
        '404': {'description': 'Recording not found'},
        '500': {'description': 'Database error'}
    }
})
def get_transcription(message_id):
    """Fetch the transcription field for a given recording."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT transcription FROM recordings WHERE id = ?",
            (message_id,)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Recording not found'}), 404
        transcription = row[0] if row[0] is not None else ''
        return jsonify({'recording_id': message_id, 'transcription': transcription}), 200
    except sqlite3.Error as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    finally:
        conn.close()


@transcription_bp.route('/transcribe', methods=['POST'])
@require_permission(['transcription.create'])
@swag_from({
    'tags': ['Transcription'],
    'summary': 'Transcribe audio file (proxy to external API)',
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'file',
            'in': 'formData',
            'type': 'file',
            'required': True,
            'description': 'Audio file to transcribe'
        }
    ],
    'responses': {
        '200': {'description': 'Transcription successful'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'API request failed'},
        '504': {'description': 'Request timeout'}
    }
})
def transcribe_proxy():
    # Get the audio file from the request
    audio_file = request.files['file']

    try:
        # Forward the request to the Boondock API
        response = request_openai_transcription(
            audio_file.stream, audio_file.filename, timeout=30
        )
        
        # Log the API response for debugging
        print(f"Target API status: {response.status_code}")
        print(f"Target API response: {response.text}")

        # Check for successful response
        response.raise_for_status()
        
        # Parse and transform the API response
        try:
            api_response = response.json()
            # Check if response is a dictionary and has 'transcription' key
            if not isinstance(api_response, dict) or 'transcription' not in api_response:
                raise ValueError("Invalid API response format: missing 'transcription' key")
            # Transform the response to include 'status' key
            formatted_response = {
                'status': 'success',
                'transcription': api_response['transcription']
            }
        except ValueError as e:
            print(f"JSON decode error: {str(e)}")
            error_resp = jsonify({'error': 'Invalid response format from API'})
            error_resp.headers['Access-Control-Allow-Origin'] = '*'
            return error_resp, 500

        # Return the formatted response
        resp = jsonify(formatted_response)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, response.status_code

    except TimeoutError:
        print("Request timed out")
        error_resp = jsonify({'error': 'API request timed out'})
        error_resp.headers['Access-Control-Allow-Origin'] = '*'
        return error_resp, 504
    except Exception as e:
        print(f"Request error: {str(e)}")
        error_resp = jsonify({'error': f'API request failed: {str(e)}'})
        error_resp.headers['Access-Control-Allow-Origin'] = '*'
        return error_resp, 500


@transcription_bp.route('/transcribe/<int:record_id>', methods=['POST'], endpoint='transcribe_proxy_audio')
@require_permission(['transcription.create'])
@swag_from({
    'tags': ['Transcription'],
    'summary': 'Transcribe a recording by ID',
    'parameters': [
        {
            'name': 'record_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'Transcription successful'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Recording not found'},
        '500': {'description': 'Server error'},
        '504': {'description': 'Request timeout'}
    }
})
def transcribe_proxy_by_id(record_id):
    # Validate record_id
    if record_id <= 0:
        return jsonify({'error': 'Invalid record ID', 'status': 'error'}), 400

    try:
        # Get filename from DB
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT filename FROM recordings WHERE id = ?", (record_id,))
            result = cur.fetchone()

        if not result:
            return jsonify({'error': 'Recording not found', 'status': 'error'}), 404

        file_path = (DATA_ROOT / result['filename']).resolve()

        # Validate file path security
        if not file_path.is_relative_to(DATA_ROOT / "recordings"):
            return jsonify({'error': 'Invalid file path', 'status': 'error'}), 400

        if not file_path.is_file():
            return jsonify({'error': 'Audio file not found', 'file_path': str(file_path), 'status': 'error'}), 404

        # Send file to API
        with open(file_path, 'rb') as audio_file:
            response = request_openai_transcription(audio_file, file_path.name, timeout=30)

        print(f"Target API status: {response.status_code}")
        print(f"Target API response: {response.text}")

        response.raise_for_status()

        # Process API response
        try:
            api_response = response.json()
            if not isinstance(api_response, dict) or 'transcription' not in api_response:
                raise ValueError("Invalid API response format: missing 'transcription' key")

            formatted_response = {
                'status': 'success',
                'transcription': api_response['transcription'],
                'record_id': record_id
            }

            # Save transcription to DB
            with sqlite3.connect(DB_PATH) as conn:
                persist_transcription(conn, record_id, api_response['transcription'])
                conn.commit()

            resp = jsonify(formatted_response)
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp, 200

        except ValueError as e:
            print(f"JSON decode error: {str(e)}")
            return jsonify({'error': 'Invalid response format from API', 'status': 'error'}), 500

    except TimeoutError:
        print("Request timed out")
        return jsonify({'error': 'API request timed out', 'status': 'error'}), 504

    except Exception as e:
        print(f"Request error: {str(e)}")
        return jsonify({'error': f'API request failed: {str(e)}', 'status': 'error'}), 500

    except sqlite3.Error as e:
        print(f"Database error: {str(e)}")
        return jsonify({'error': 'Database error occurred', 'status': 'error'}), 500

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred', 'status': 'error'}), 500
