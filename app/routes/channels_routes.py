"""
Channel management routes.
Handles all channel CRUD operations and channel-related queries.
"""
import re
import logging
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from ..middleware.auth_middleware import require_permission

from ..utils.logging_setup import error_logger, event_logger
from ..services.settings_manager import get_settings_manager
from ..services.channel_state import load_owned_channels, load_request_channel, load_owned_recordings

_settings_manager = get_settings_manager()

channels_bp = Blueprint('channels', __name__)

@channels_bp.route('/channels')
@require_permission(['channel.read'], loader=load_owned_channels, inject_as='channels_data')
@swag_from({
    'tags': ['Channels'],
    'summary': 'Get all channels',
    'responses': {
        '200': {'description': 'List of all active (non-deleted) channels'}
    }
})
def get_channels(channels_data):
    """Fetch and return all active channels (excludes soft-deleted) with default values for missing fields."""

    # Filter out soft-deleted channels
    active_channels = [ch for ch in channels_data if not ch.get('deleted')]

    # Add default values for model, language, and auto_transcribe if not present
    for channel in active_channels:
        channel.setdefault('model', 'medium.en')  # Default model
        channel.setdefault('src_language', 'english')  # Default language
        channel.setdefault('auto_transcribe', True)  # Default to enabled for auto-transcription

    return jsonify(active_channels)


@channels_bp.route('/channel/<int:channel_id>/recordings')
@require_permission(
    ['recording.read'], loader=load_owned_recordings,
    id_argument='channel_id', inject_as='recordings'
)
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get recordings for a specific channel',
    'parameters': [
        {
            'name': 'channel_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Channel ID'
        }
    ],
    'responses': {
        '200': {'description': 'List of channel recordings'}
    }
})
def get_channel_recordings(channel_id, recordings):
    return jsonify(recordings)


@channels_bp.route('/channel/<int:channel_id>', methods=['GET'])
@require_permission(
    ['channel.read'], loader=load_request_channel,
    id_argument='channel_id', inject_as='channel'
)
@swag_from({
    'tags': ['Channels'],
    'summary': 'Get a specific channel',
    'parameters': [
        {
            'name': 'channel_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Channel ID'
        }
    ],
    'responses': {
        '200': {'description': 'Channel details'},
        '404': {'description': 'Channel not found'}
    }
})
def get_channel(channel_id, channel):
    """Get details for a specific channel."""
    return jsonify(channel)

@channels_bp.route('/channel/<int:channel_id>', methods=['PUT'])
@require_permission(
    ['channel.update'], loader=load_request_channel,
    id_argument='channel_id', inject_as='channel'
)
@swag_from({
    'tags': ['Channels'],
    'summary': 'Update channel configuration',
    'parameters': [
        {
            'name': 'channel_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Channel ID'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'status': {'type': 'string'},
                    'model': {'type': 'string'},
                    'color': {'type': 'string'},
                    'background_color': {'type': 'string'},
                    'team_color': {'type': 'string'},
                    'threshold': {'type': 'string'},
                    'silence': {'type': 'string'},
                    'min_rec': {'type': 'string'},
                    'max_rec': {'type': 'string'},
                    'audio_gain': {'type': 'string'},
                    'speaker_enabled': {'type': 'boolean'},
                    'speaker_volume': {'type': 'integer'},
                    'audio_stream_enabled': {'type': 'boolean'},
                    'audio_stream_port': {'type': 'integer'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Channel updated successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Channel not found'},
        '500': {'description': 'Server error'}
    }
})
def update_channel(channel_id, channel):
    """Update a channel's data."""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body is missing or invalid'}), 400

    # Validate required fields if they're provided
    if 'name' in data and not data['name']:
        return jsonify({'error': 'Name cannot be empty'}), 400
    if 'status' in data and not data['status']:
        return jsonify({'error': 'Status cannot be empty'}), 400

    # Validate color formats if they're provided
    hex_color_pattern = r'^#[0-9A-Fa-f]{6}$'
    colors_to_validate = [
        ('color', data.get('color')),
        ('background_color', data.get('background_color')),
        ('team_color', data.get('team_color'))
    ]
    for color_field, color_value in colors_to_validate:
        if color_value and not re.match(hex_color_pattern, color_value):
            return jsonify({'error': f'Invalid {color_field} format. Use a hex color code (e.g., #RRGGBB).'}), 400

    # Validate audio settings if they're provided
    audio_validations = [
        ('threshold', data.get('threshold'), lambda x: 0 <= float(x) <= 100, "Threshold must be between 0 and 100."),
        ('silence', data.get('silence'), lambda x: 500 <= float(x) <= 5000, "Silence must be between 500 and 5000 milliseconds."),
        ('min_rec', data.get('min_rec'), lambda x: 1000 <= float(x) <= 5000, "Min recording time must be between 1000 and 5000 milliseconds."),
        ('max_rec', data.get('max_rec'), lambda x: 10000 <= float(x) <= 30000, "Max recording time must be between 10000 and 30000 milliseconds."),
        ('audio_gain', data.get('audio_gain'), lambda x: 1 <= float(x) <= 10, "Audio gain must be between 1 and 10.")
    ]

    for field, value, validation_func, error_msg in audio_validations:
        if value is not None:  # Only validate if the field is provided
            try:
                float_value = float(value)
                if not validation_func(float_value):
                    return jsonify({'error': error_msg}), 400
            except ValueError:
                return jsonify({'error': f'Invalid {field} value. Must be a number.'}), 400

    # Validate audio stream port if audio stream is enabled
    if data.get('audio_stream_enabled'):
        port = data.get('audio_stream_port')
        if port is None:
            return jsonify({'error': 'audio_stream_port is required when audio_stream_enabled is true'}), 400
        
        try:
            port_int = int(port)
            if not (6001 <= port_int <= 6010):
                return jsonify({'error': 'audio_stream_port must be between 6001 and 6010'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': 'audio_stream_port must be a valid integer'}), 400
        
        # Check if port is already in use by another channel
        channels_data = _settings_manager.get_all_channels()
        for ch in channels_data:
            if ch['id'] != channel_id and ch.get('audio_stream_enabled') and ch.get('audio_stream_port') == port_int:
                return jsonify({'error': f'Port {port_int} is already in use by another channel'}), 400
    elif data.get('audio_stream_enabled') is False:
        # If disabling audio stream, remove the port assignment
        data['audio_stream_port'] = None

    # Update only the fields that were provided in the request
    fields_to_update = [
        'name', 'status', 'model', 'color', 'background_color', 'team_color',
        'src_language', 'target_language', 'threshold', 'silence', 'min_rec',
        'max_rec', 'audio_gain', 'driver', 'mac', 'person', 'tag', 'car',
        'frequency', 'tone', 'type', 'audio_stream_enabled', 'audio_stream_port', 
        'auto_transcribe', 'speaker_enabled', 'speaker_volume'
    ]

    for field in fields_to_update:
        if field in data:
            channel[field] = data[field]

    # Save updated channel using SettingsManager
    _settings_manager.save_channel(channel)

    return jsonify({'message': 'Channel updated successfully'}), 200


@channels_bp.route('/channel/<int:channel_id>', methods=['DELETE'])
@require_permission(
    ['channel.delete'], loader=load_request_channel,
    id_argument='channel_id', inject_as='channel'
)
@swag_from({
    'tags': ['Channels'],
    'summary': 'Delete a channel (soft delete)',
    'description': 'Soft deletes a channel configuration by marking it as deleted. The channel ID is preserved and audio files remain linked. If the same MAC address reconnects, the channel will be restored.',
    'parameters': [
        {
            'name': 'channel_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Channel ID to delete'
        }
    ],
    'responses': {
        '200': {'description': 'Channel deleted successfully'},
        '404': {'description': 'Channel not found'},
        '500': {'description': 'Server error'}
    }
})
def delete_channel(channel_id, channel):
    """Soft delete a channel configuration (marks as deleted, preserves ID and audio file links)."""
    try:
        # Soft delete: mark as deleted instead of removing
        channel['deleted'] = True
        channel['status'] = 'deleted'
        
        # Save updated channel using SettingsManager
        _settings_manager.save_channel(channel)

        logging.info(f"Channel {channel_id} soft-deleted (MAC: {channel.get('mac', 'N/A')})")
        event_logger.info(f"Channel {channel_id} soft-deleted by user (MAC: {channel.get('mac', 'N/A')})")
        
        return jsonify({'message': 'Channel deleted successfully', 'deleted_channel_id': channel_id}), 200

    except Exception as e:
        error_logger.error(f"Error deleting channel {channel_id}: {str(e)}")
        return jsonify({'error': f'Failed to delete channel: {str(e)}'}), 500


@channels_bp.route('/channel', methods=['POST'])
@require_permission(['channel.create'])
@swag_from({
    'tags': ['Channels'],
    'summary': 'Create a new channel',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['name'],
                'properties': {
                    'name': {'type': 'string'},
                    'status': {'type': 'string', 'default': 'enabled'},
                    'model': {'type': 'string', 'default': 'medium.en'},
                    'src_language': {'type': 'string', 'default': 'english'},
                    'target_language': {'type': 'string', 'default': 'english'},
                    'color': {'type': 'string', 'default': '#000000'},
                    'background_color': {'type': 'string', 'default': '#FFFFFF'},
                    'team_color': {'type': 'string', 'default': '#FFFFFF'},
                    'threshold': {'type': 'string', 'default': '50'},
                    'silence': {'type': 'string', 'default': '1000'},
                    'min_rec': {'type': 'string', 'default': '1000'},
                    'max_rec': {'type': 'string', 'default': '30000'},
                    'audio_gain': {'type': 'string', 'default': '3'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Channel created successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def create_channel():
    """
    Create a new channel with the provided data.
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body is missing or invalid'}), 400

    # Extract data from request, using defaults for optional fields
    name = data.get('name')
    status = data.get('status', "enabled")
    model = data.get('model', 'medium.en')
    src_language = data.get('src_language', 'english')
    target_language = data.get('target_language', 'english')
    color = data.get('color', '#000000')
    background_color = data.get('background_color', '#FFFFFF')
    team_color = data.get('team_color', '#FFFFFF')
    car = data.get('car', 'car name')
    driver = data.get('driver', 'driver name')
    person = data.get('person', 'person name')
    tag = data.get('tag', 'tag')
    mac = data.get('mac', 'mac address')
    audio_stream_enabled = data.get('audio_stream_enabled', False)
    
    # New audio settings with default values
    threshold = data.get('threshold', '50')
    silence = data.get('silence', '1000')
    min_rec = data.get('min_rec', '1000')
    max_rec = data.get('max_rec', '30000')
    audio_gain = data.get('audio_gain', '3')

    # Validate required fields
    if not name:
        return jsonify({'error': 'Missing required field: name'}), 400

    # Validate color formats
    hex_color_pattern = r'^#[0-9A-Fa-f]{6}$'
    if not all(re.match(hex_color_pattern, c) for c in [color, background_color, team_color]):
        return jsonify({'error': 'Invalid color format. Use a hex color code (e.g., #RRGGBB).'}), 400

    # Validate audio settings
    try:
        float_threshold = float(threshold)
        float_silence = float(silence)
        float_min_rec = float(min_rec)
        float_max_rec = float(max_rec)
        float_audio_gain = float(audio_gain)

        if not (0 <= float_threshold <= 100):
            raise ValueError("Audio Threshold must be between 0 and 100.")
        if not (500 <= float_silence <= 5000):
            raise ValueError("Silence must be between 500 and 5000 milliseconds.")
        if not (1000 <= float_min_rec <= 5000):
            raise ValueError("Min recording time must be between 1000 and 5000 milliseconds.")
        if not (10000 <= float_max_rec <= 30000):
            raise ValueError("Max recording time must be between 10000 and 30000 milliseconds.")
        if not (1 <= float_audio_gain <= 10):
            raise ValueError("Audio gain must be between 1 and 10.")
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Get auto_transcribe setting (default to True)
    auto_transcribe = data.get('auto_transcribe', True)
    
    # Create new channel entry
    new_channel = {
        'name': name,
        'status': status,
        'model': model,
        'src_language': src_language,
        'target_language': target_language,
        'color': color,
        'background_color': background_color,
        'team_color': team_color,
        'car': car,
        'driver': driver,
        'person': person,
        'tag': tag,
        'mac': mac,
        'audio_stream_enabled': audio_stream_enabled,
        'threshold': threshold,
        'silence': silence,
        'min_rec': min_rec,
        'max_rec': max_rec,
        'audio_gain': audio_gain,
        'auto_transcribe': auto_transcribe
    }

    # Don't set ID - let save_channel auto-generate it for new records
    # Remove ID if present to ensure INSERT instead of UPDATE
    if 'id' in new_channel:
        del new_channel['id']

    # Save the new channel using SettingsManager - this will return the new ID
    new_id = _settings_manager.save_channel(new_channel)
    
    if new_id == -1:
        return jsonify({'error': 'Failed to save channel'}), 500

    return jsonify({'message': 'Channel created successfully', 'channel_id': new_id}), 201

@channels_bp.route('/v1/channels/<mac>', methods=['GET'])
@require_permission(
    ['device', 'channel.read'], loader=load_request_channel,
    id_argument='mac', inject_as='channel'
)
@swag_from({
    'tags': ['Channels'],
    'summary': 'Get channel data by MAC address',
    'description': 'Retrieves channel configuration data from channels.json for the specified MAC address',
    'parameters': [
        {
            'name': 'mac',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address (e.g., 10061CD8B8E8)'
        }
    ],
    'responses': {
        '200': {
            'description': 'Channel data retrieved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'mac': {'type': 'string'},
                    'name': {'type': 'string'},
                    'status': {'type': 'string'},
                    'model': {'type': 'string'},
                    'src_language': {'type': 'string'},
                    'threshold': {'type': 'string'},
                    'silence': {'type': 'string'},
                    'min_rec': {'type': 'string'},
                    'max_rec': {'type': 'string'},
                    'audio_gain': {'type': 'string'},
                    'color': {'type': 'string'},
                    'background_color': {'type': 'string'},
                    'team_color': {'type': 'string'},
                    'target_language': {'type': 'string'},
                    'driver': {'type': 'string'},
                    'person': {'type': 'string'},
                    'tag': {'type': 'string'},
                    'car': {'type': 'string'},
                    'state': {'type': 'string'},
                    'frequency': {'type': 'number'},
                    'tone': {'type': 'string'},
                    'type': {'type': 'string'},
                    'firmware': {'type': 'string'},
                    'ip': {'type': 'string'},
                    'audio_stream_enabled': {'type': 'boolean'}
                },
                'additionalProperties': True
            }
        },
        '404': {
            'description': 'Channel not found for the specified MAC address'
        },
        '500': {
            'description': 'Server error - Failed to read channels.json'
        }
    }
})
def get_channel_by_mac(mac, channel):
    """Get channel data by MAC address from database."""
    try:
        # Normalize MAC address to uppercase for comparison
        mac_address = mac.upper().strip()

        # Return channel data as JSON
        logging.info(f"Channel data retrieved for MAC: {mac_address}")
        return jsonify(channel), 200
    except Exception as e:
        logging.exception(f"Error retrieving channel data for MAC {mac_address}: {e}")
        return jsonify({'error': f'Failed to retrieve channel data: {str(e)}'}), 500
