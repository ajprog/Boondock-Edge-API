"""
Device v1 API routes.
Handles device events, audio uploads to S3, log uploads, channel retrieval, and device settings.
"""
import json
import os
import re
import logging
import subprocess
import io
import time
import uuid
from config import DATA_ROOT
from datetime import datetime, timezone
from flask import Blueprint, after_this_request, jsonify, request, send_file
from flasgger import swag_from
from ..middleware.auth_middleware import require_admin, require_permission
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from werkzeug.utils import secure_filename

from app.services.audio_handler import get_audio_handler
from app.utils.crc_utils import check_and_update_duplicate_cache
from app.utils.sqlite_utils import connect_sqlite, log_slow_operation
from app.services.channel_state import (
    set_channel_visual_state,
    get_channel_visual_state,
    get_stored_visual_state,
    get_all_channel_visual_states,
    load_owned_channels,
    load_request_channel,
    touch_device_activity,
    ChannelVisualState,
)
from ..utils.logging_setup import error_logger, event_logger
from ..utils.auth import (
    get_request_token,
    authenticate_token,
    is_mac_registered,
    generate_token,
    get_mac_for_token,
)
from ..utils.s3_utils import (
    ensure_bucket_exists,
    is_s3_enabled,
    get_s3_client,
    get_s3_settings,
)
from ..routes.route_utils import (
    RECORDINGS_DIR,
    DB_PATH,
    get_channel_id_from_mac,
    get_mac_from_channel_id,
    create_channel_for_mac,
    get_recording_path,
    channels_lock,
    calculate_wav_duration,
    allowed_file,
)
from ..services.settings_manager import normalize_mac_address
from ..services.device_health_monitor import (
    track_device_created,
    track_connection,
    track_event,
    track_error,
    track_file_upload
)
from ..services.cloud_device_events import (
    persist_cloud_device_event_async,
    cloud_event_type_id,
    list_cloud_events_for_mac,
)

device_bp = Blueprint('device', __name__)

# Directory for device settings storage
DEVICE_SETTINGS_DIR = str(DATA_ROOT / 'device_settings')
os.makedirs(DEVICE_SETTINGS_DIR, exist_ok=True)

def _device_logs_root(mac_key: str) -> str:
    return DATA_ROOT / "logs" / mac_key.lower()

def _mac_hex_only(mac_str):
    if not mac_str:
        return ""
    return re.sub(r"[^0-9A-Fa-f]", "", str(mac_str).strip()).upper()

def _normalize_cloud_mac_address(mac_str):
    """Return AA:BB:CC:DD:EE:FF or None if invalid."""
    hx = _mac_hex_only(mac_str)
    if len(hx) != 12:
        return None
    return ":".join(hx[i : i + 2] for i in range(0, 12, 2))

@device_bp.route('/v1/events', methods=['POST'])
@swag_from({
    'tags': ['Events'],
    'summary': 'Handle a device lifecycle event',
    'description': (
        'Accepts a JSON device event with mac_address, event_type, and optional '
        'event_data.'
    ),
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['mac_address', 'event_type'],
                'properties': {
                    'mac_address': {'type': 'string'},
                    'event_type': {'type': 'string'},
                    'event_data': {'type': 'object'},
                },
            },
        },
    ],
    'responses': {
        '200': {'description': 'Event accepted'},
        '400': {'description': 'Missing or invalid fields'},
        '500': {'description': 'Server error'},
    },
})
def post_v1_events():
    """Accept a JSON device lifecycle event."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'JSON body is required'}), 400
    if not (data.get('mac_address') or '').strip() or not (
        data.get('event_type') or ''
    ).strip():
        return jsonify({'error': 'Missing required fields'}), 400
    try:
        """Cloud API: JSON body mac_address, event_type, optional event_data (DEVICE_API.md)."""
        mac_colon = _normalize_cloud_mac_address(data.get("mac_address") or "")
        if not mac_colon:
            return jsonify({"error": "Missing required fields"}), 400

        event_type = (data.get("event_type") or "").strip()
        if not event_type:
            return jsonify({"error": "Missing required fields"}), 400

        event_type_id = cloud_event_type_id(event_type)
        event_data = data.get("event_data")
        if event_data is not None and not isinstance(event_data, dict):
            event_data = {"value": event_data}

        mac_key = normalize_mac_address(mac_colon)
        if len(mac_key) != 12:
            return jsonify({"error": "Invalid MAC address"}), 400
        if get_channel_id_from_mac(mac_key, refresh=False) is None:
            new_id = create_channel_for_mac(mac_key)
            if new_id is None:
                return jsonify({"error": "Failed to create channel"}), 500
            track_device_created(mac_key)

        # Use the unified token transport parser here as well.  The previous local
        # parser accepted only an exact ``Authorization: Bearer `` prefix, so a
        # valid device credential sent through X-API-Key (or a case-variant bearer
        # scheme) was treated as missing and a replacement credential was issued.
        token = get_request_token()
        warning = None
        new_token = None
        expires_at = None

        if not is_mac_registered(mac_colon):
            new_token, expires_at = generate_token(mac_colon)
            warning = "MAC address registered"

        # Always perform the diagnostic lookup.  Besides validating a presented
        # credential, this records an explicit ``missing`` result before bootstrap
        # issues a credential when the device did not send one.
        token_mac = get_mac_for_token(token, expected_mac=mac_colon)
        token_hex = _mac_hex_only(token_mac) if token_mac else ""
        token_valid = bool(
            token_hex
            and token_hex == _mac_hex_only(mac_colon)
            and authenticate_token(token)
        )
        if not token_valid:
            if not new_token:
                new_token, expires_at = generate_token(mac_colon)
            warning = "Invalid token" if not warning else warning + "; Invalid token"

        persist_cloud_device_event_async(
            mac_key, event_type_id, event_type, event_data
        )

        touch_device_activity(mac_key)

        et = event_type.strip().lower()
        if et == "online":
            track_connection(mac_key)
            track_event(mac_key)
            set_channel_visual_state(mac_key, "online")
        elif et == "ping":
            track_connection(mac_key)
            track_event(mac_key)
            if get_stored_visual_state(mac_key) != ChannelVisualState.RECORDING:
                set_channel_visual_state(mac_key, "online")
        elif et == "record_begin":
            track_event(mac_key)
            set_channel_visual_state(mac_key, ChannelVisualState.RECORDING)
        elif et == "record_end":
            track_event(mac_key)
            set_channel_visual_state(mac_key, ChannelVisualState.IDLE)
        elif et == "warning":
            track_event(mac_key)
            set_channel_visual_state(mac_key, ChannelVisualState.WARNING)
        elif et in ("error", "fatal_error"):
            track_event(mac_key)
            track_error(mac_key)
            set_channel_visual_state(mac_key, ChannelVisualState.ERROR)
        else:
            track_event(mac_key)

        ts = datetime.now(timezone.utc).isoformat()
        body = {"message": "Event received", "timestamp": ts}
        if warning:
            body["warning"] = warning
        if new_token:
            body["token"] = new_token
            body["expires_at"] = expires_at
        return jsonify(body), 200
    except Exception:
        logging.exception('device lifecycle event')
        return jsonify({'error': 'An error occurred processing your request'}), 500

@device_bp.route('/v1/channel-visual-states', methods=['GET'])
@require_permission(
    ['channel.read'], loader=load_owned_channels, inject_as='channels'
)
@swag_from({
    'tags': ['Events'],
    'summary': 'Get channel visual states',
    'description': 'Returns in-memory visual states for all channels (recording, idle, error, warning)',
    'responses': {
        '200': {
            'description': 'Visual states retrieved successfully',
            'schema': {
                'type': 'object',
                'additionalProperties': {
                    'type': 'object',
                    'properties': {
                        'state': {
                            'type': 'string',
                            'enum': ['idle', 'recording', 'error', 'warning']
                        },
                        'timestamp': {'type': 'string'}
                    }
                },
                'example': {
                    'B8D61A5AC6C0': {
                        'state': 'recording',
                        'timestamp': '2025-12-26T14:30:45.123456'
                    }
                }
            }
        }
    }
})
def get_channel_visual_states(channels):
    """Get visual states for all channels in memory."""
    try:
        return jsonify(get_all_channel_visual_states(channels)), 200
    except Exception as e:
        error_logger.error(f"Error fetching channel visual states: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@device_bp.route('/v1/channel-visual-states/<mac>', methods=['GET'])
@require_permission(
    ['channel.read'],
    loader=load_request_channel,
    id_argument='mac',
    inject_as='channel',
)
@swag_from({
    'tags': ['Events'],
    'summary': 'Get visual state for a specific channel',
    'parameters': [
        {
            'name': 'mac',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'MAC address of the channel'
        }
    ],
    'responses': {
        '200': {'description': 'Visual state retrieved successfully'},
        '404': {'description': 'Channel has no visual state'}
    }
})
def get_channel_visual_state_by_mac(mac, channel):
    """Get visual state for a specific channel by MAC address."""
    try:
        state = get_channel_visual_state(channel['mac'])
        if state is None:
            return jsonify({"state": None, "message": "No visual state set for this channel"}), 200
        
        return jsonify({"mac": channel['mac'], "state": state}), 200
    except Exception as e:
        error_logger.error(f"Error fetching visual state for {mac}: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@device_bp.route('/v1/devices/<mac>/logs/files', methods=['GET'])
@require_admin
def device_logs_list_files(mac):
    """List uploaded device log files under logs/<mac>/."""
    mac_key = normalize_mac_address(mac)
    if len(mac_key) != 12:
        return jsonify({"error": "Invalid MAC address"}), 400
    root = _device_logs_root(mac_key)
    if not os.path.isdir(root):
        return jsonify({"files": [], "mac": mac_key}), 200
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            low = fn.lower()
            if not low.endswith((".log", ".txt", ".json")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                rel = os.path.relpath(full, root).replace("\\", "/")
            except ValueError:
                continue
            if ".." in rel:
                continue
            try:
                st = os.stat(full)
                files.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "modified": datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
            except OSError:
                continue
    files.sort(key=lambda x: x["path"], reverse=True)
    return jsonify({"files": files, "mac": mac_key}), 200

@device_bp.route('/v1/devices/<mac>/logs/content', methods=['GET'])
@require_admin
def device_logs_content(mac):
    """Return log file text; use ?path= relative path or ?date=YYYY-MM-DD."""
    mac_key = normalize_mac_address(mac)
    if len(mac_key) != 12:
        return jsonify({"error": "Invalid MAC address"}), 400
    root = _device_logs_root(mac_key)
    abs_path = None
    rel_path = request.args.get("path", "").strip().replace("\\", "/")
    date_str = request.args.get("date", "").strip()

    if rel_path:
        if ".." in rel_path or rel_path.startswith("/"):
            return jsonify({"error": "Invalid path"}), 400
        cand = os.path.abspath(os.path.join(root, rel_path))
        if not cand.startswith(root + os.sep) and cand != root:
            return jsonify({"error": "Invalid path"}), 400
        if os.path.isfile(cand):
            abs_path = cand
    elif date_str and re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        y, m, _ = date_str.split("-")
        for ext in (".log", ".txt", ".json"):
            cand = os.path.join(root, y, m, f"{date_str}{ext}")
            if os.path.isfile(cand):
                abs_path = os.path.abspath(cand)
                break
    else:
        return jsonify({"error": "Provide path= or date=YYYY-MM-DD"}), 400

    if not abs_path or not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404

    max_bytes = 512 * 1024
    try:
        sz = os.path.getsize(abs_path)
        with open(abs_path, "rb") as f:
            if sz > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
                raw = f.read()
                truncated = True
            else:
                raw = f.read()
                truncated = False
        text = raw.decode("utf-8", errors="replace")
        return jsonify(
            {
                "content": text,
                "truncated": truncated,
                "path": os.path.relpath(abs_path, root).replace("\\", "/"),
                "mac": mac_key,
            }
        ), 200
    except OSError as e:
        return jsonify({"error": str(e)}), 500

@device_bp.route('/v1/devices/<mac>/events', methods=['GET'])
@require_permission(
    ['channel.read'],
    loader=load_request_channel,
    id_argument='mac',
    inject_as='channel',
)
def device_cloud_events_list(mac, channel):
    """Recent cloud_device_events for this MAC."""
    mac_key = channel['mac']
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    types_param = request.args.get("types", "").strip()
    type_list = (
        [x.strip().lower() for x in types_param.split(",") if x.strip()]
        if types_param
        else None
    )
    events = list_cloud_events_for_mac(mac_key, limit=limit, event_types=type_list)
    return jsonify({"events": events, "mac": mac_key}), 200

@device_bp.route('/v1/audio/s3', methods=['POST'])
@device_bp.route('/v2/audio/s3', methods=['POST'])
@device_bp.route('/upload/audio', methods=['POST'])
@require_permission(
    ['device', 'recording.create'], loader=load_request_channel, inject_as='channel'
)
@swag_from({
    'tags': ['Audio'],
    'summary': 'Upload an audio file to S3 (v1: default WAV; v2: default MP3)',
    'security': [{'BearerAuth': []}],
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'formData',
            'type': 'string',
            'required': False,
            'description': 'Optional MAC consistency check; credential selects the channel'
        },
        {
            'name': 'audio_file',
            'in': 'formData',
            'type': 'file',
            'required': True,
            'description': 'WAV file to upload'
        },
        {
            'name': 'convert_to_mp3',
            'in': 'formData',
            'type': 'boolean',
            'required': False,
            'description': 'Convert WAV → MP3 before upload',
            'default': False
        },
        {
            'name': 'tags',
            'in': 'formData',
            'type': 'string',
            'required': False,
            'description': 'JSON containing "recorder", "dock", "user" objects that will be stored as S3 tags',
            'example': '{"recorder":{"id":"rec-01"},"dock":{"id":"dock-A"},"user":{"name":"Alice"}}'
        },
        {
            'name': 'timestamp',
            'in': 'formData',
            'type': 'string',
            'format': 'date-time',
            'required': False,
            'description': 'ISO-8601 time the clip was recorded'
        }
    ],
    'responses': {
        '200': {
            'description': 'Audio uploaded successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'timestamp': {'type': 'string', 'format': 'date-time'}
                },
                'required': ['message', 'timestamp']
            }
        },
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error during upload or bucket creation'}
    }
})
def upload_audio_s3(channel):
    """Upload audio files from Boondock devices to iDrive storage"""
    # Registering a response callback here ensures early validation returns are
    # timed as well as the complete upload path for both routes handled by this
    # function.
    is_v2_audio = request.path.rstrip("/").endswith("/api/v2/audio/s3")
    request_started_at = time.perf_counter()
    step_started_at = request_started_at
    previous_log_duration_ms = 0.0
    request_size = request.content_length
    request_id = uuid.uuid4().hex[:12]
    audio_filename = None

    @after_this_request
    def log_audio_performance(response):
        duration_ms = (time.perf_counter() - request_started_at) * 1000
        logging.info(
            "audio_s3_performance request_id=%s file=%s status_code=%s "
            "duration_ms=%.2f request_bytes=%s response_bytes=%s",
            request_id,
            audio_filename or "Error",
            response.status_code,
            duration_ms,
            request_size if request_size is not None else "unknown",
            response.content_length
            if response.content_length is not None
            else "unknown",
        )
        return response

    def log_audio_step(step):
        """Log both the current step and cumulative request duration."""
        nonlocal previous_log_duration_ms, step_started_at
        now = time.perf_counter()

        if(previous_log_duration_ms > 1000 or (now - step_started_at) > 1000):
            logging.info(
                "audio_s3_step_performance request_id=%s step=%s "
                "step_duration_ms=%.2f total_duration_ms=%.2f "
                "previous_log_duration_ms=%.2f",
                request_id,
                step,
                (now - step_started_at) * 1000,
                (now - request_started_at) * 1000,
                previous_log_duration_ms,
            )
        # Start the next step after the log record has been emitted. Logging can
        # block on the configured handler (for example journald), and charging
        # that delay to the next application step produces misleading timings.
        log_completed_at = time.perf_counter()
        previous_log_duration_ms = (log_completed_at - now) * 1000
        step_started_at = log_completed_at

    def log_audio_request():
        """Log request metadata without exposing credentials or file contents."""
        redacted_headers = {"authorization", "cookie", "proxy-authorization"}
        headers = {
            key: "[REDACTED]" if key.lower() in redacted_headers else value
            for key, value in request.headers.items()
        }
        sensitive_form_fields = {"access_token", "api_key", "password", "secret", "token"}
        form = {
            key: ["[REDACTED]"] if key.lower() in sensitive_form_fields else values
            for key, values in request.form.to_dict(flat=False).items()
        }
        files = {
            key: [
                {
                    "filename": uploaded_file.filename,
                    "content_type": uploaded_file.content_type,
                    "content_length": uploaded_file.content_length,
                }
                for uploaded_file in uploaded_files
            ]
            for key, uploaded_files in request.files.lists()
        }
        request_details = {
            "method": request.method,
            "path": request.path,
            "query": request.args.to_dict(flat=False),
            "headers": headers,
            "content_type": request.content_type,
            "content_length": request.content_length,
            "form": form,
            "files": files,
            "remote_addr": request.remote_addr,
        }
        logging.info(
            "audio_s3_request request_id=%s request=%s",
            request_id,
            json.dumps(request_details, sort_keys=True, default=str),
        )

    # 1. ---- Validate form data -------------------------------------------------
    # Wrap form data access in try-except to handle connection errors gracefully
    try:
        uploaded_files = request.files
        uploaded_audio = uploaded_files.get("audio_file")
        audio_filename = uploaded_audio.filename if uploaded_audio is not None else "Error"
        if uploaded_audio is None:
            logging.warning("Missing audio_file in request")
            log_audio_request()
            log_audio_step("multipart_parsing")
            return (
                jsonify({"error": "Missing required field (audio_file)"}),
                400,
            )

        mac_address = channel['mac']
        audio_file = request.files["audio_file"]
        log_audio_step("form_parsing")
        audio_filename = audio_file.filename if audio_file is not None else "Error"
        log_audio_request()
        log_audio_step("multipart_parsing")
    except (OSError, ConnectionResetError, ConnectionError) as e:
        audio_filename = "Error"
        # Handle cases where client disconnects before full request is received
        error_msg = str(e)
        if "unexpected end of file" in error_msg.lower() or "connection reset" in error_msg.lower():
            logging.warning(f"Client disconnected during upload: {error_msg}")
            log_audio_step("multipart_parsing")
            return (
                jsonify({"error": "Upload interrupted - connection closed by client"}),
                499,  # 499 Client Closed Request (non-standard but appropriate)
            )
        else:
            logging.error(f"Connection error during form data parsing: {error_msg}")
            log_audio_step("multipart_parsing")
            return (
                jsonify({"error": "Connection error during upload"}),
                400,
            )
    except Exception as e:
        audio_filename = "Error"
        # Catch any other unexpected errors during form parsing
        logging.error(f"Unexpected error during form data parsing: {str(e)}")
        log_audio_step("multipart_parsing")
        return (
            jsonify({"error": "Failed to parse request data"}),
            400,
        )

    # v2 path defaults to MP3 when convert_to_mp3 omitted (cloud API); v1 defaults to WAV
    if "convert_to_mp3" in request.form:
        convert_to_mp3 = request.form.get("convert_to_mp3", "false").lower() in [
            "true",
            "1",
            "yes",
        ]
    else:
        convert_to_mp3 = bool(is_v2_audio)

    tags_param = request.form.get("tags")
    timestamp_str = request.form.get("timestamp", "").strip()
    
    # ── Tags (unchanged) --------------------------------------------------------
    tagging_header = None

    if tags_param:
        try:
            tags_dict = json.loads(tags_param)
            if not isinstance(tags_dict, dict):
                raise ValueError("tags must be a JSON object")

            tag_groups = {}
            for group in ["recorder", "dock", "user"]:
                if group in tags_dict and isinstance(tags_dict[group], dict):
                    tag_groups[group] = json.dumps(tags_dict[group])
            if tag_groups:
                tagging_header = urlencode(tag_groups)
        except (json.JSONDecodeError, ValueError):
            logging.warning("Invalid tags JSON")
            log_audio_step("upload_metadata")
            return jsonify({"error": "Invalid tags JSON"}), 400

    # ── Timestamp (unchanged) ---------------------------------------------------
    if timestamp_str:
        try:
            utc_now = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if utc_now.tzinfo is None:
                utc_now = utc_now.replace(tzinfo=timezone.utc)
        except Exception:
            logging.warning("Invalid timestamp format: %s", timestamp_str)
            log_audio_step("upload_metadata")
            return jsonify({"error": "Invalid timestamp format; use ISO 8601"}), 400
    else:
        utc_now = datetime.now(timezone.utc)
    log_audio_step("upload_metadata")

    if audio_file.filename == "":
        logging.warning("Empty filename in upload")
        log_audio_step("file_validation")
        return jsonify({"error": "No file selected"}), 400

    uploaded_filename = secure_filename(audio_file.filename)
    if not uploaded_filename:
        logging.warning("Invalid filename in upload")
        log_audio_step("file_validation")
        return jsonify({"error": "Invalid filename"}), 400
    if not allowed_file(uploaded_filename, {"wav"}):
        logging.warning("Unsupported audio file extension: %s", uploaded_filename)
        log_audio_step("file_validation")
        return jsonify({"error": "Unsupported audio file type; expected a WAV file"}), 400
    log_audio_step("file_validation")

    channel_id = channel['id']
    log_audio_step("channel_lookup")

    logging.info("Received file for Channel ID: %s for MAC: %s with timestamp: %s", channel_id, mac_address, utc_now.isoformat())

    # 4. ---- Build S3 key path --------------------------------------------------
    # Get bucket name from settings (may be blank when S3 is intentionally disabled)
    s3_settings = get_s3_settings()
    bucket = s3_settings.get('bucket_name', '')

    # Use MAC address as folder prefix in the bucket. The final object name is
    # assigned after local collision handling so local and S3 names stay aligned.
    mac_folder = mac_address.lower()
    ext = "mp3" if convert_to_mp3 else "wav"

    # Prepare for local save and S3 upload
    local_file_saved = False
    relative_path = None
    absolute_path = None
    recording_id = None
    is_duplicate = False
    crc_value = None
    log_audio_step("s3_configuration")

    # 5. ---- Save locally and upload to S3 --------------------------------------
    try:
        # Save file locally if channel_id is available
        if channel_id is not None:
            try:
                # Read bytes once for filename/size duplicate check
                audio_file.seek(0)
                file_bytes = audio_file.read()
                audio_file.seek(0)
                crc_name = (audio_file.filename or "").strip() or "upload.wav"
                crc_result = check_and_update_duplicate_cache(file_bytes, channel_id, crc_name)
                is_duplicate = bool(crc_result.get("is_duplicate", False))
                crc_value = crc_result.get("crc")
                log_audio_step("duplicate_check")
                if is_duplicate:
                    logging.info(
                        "Duplicate file detected (S3 device upload, within 30 min window) for channel %s: "
                        "filesize=%s, previous=%s",
                        channel_id,
                        len(file_bytes),
                        crc_result.get("previous_timestamp"),
                    )
                # Get MAC address for file path structure
                mac_address_for_path = mac_address.lower() if mac_address else None
                if not mac_address_for_path:
                    # Try to get MAC from channel_id
                    mac_address_for_path = get_mac_from_channel_id(channel_id)
                
                if not mac_address_for_path:
                    logging.warning("MAC address not found for channel_id %s, using fallback path", channel_id)
                    # Fallback to old structure if MAC not found
                    absolute_path = DATA_ROOT / 'recordings' / f'channel_{channel_id}' / f"audio_{utc_now.strftime('%Y%m%d_%H%M%S')}.wav"
                else:
                    # Preserve the upload name; get_recording_path adds a
                    # microsecond suffix if that name already exists.
                    absolute_path = get_recording_path(
                        mac_address_for_path, utc_now, uploaded_filename
                    )
                
                # Create directory for the file if it doesn't exist
                directory_path = absolute_path.parent
                try:
                    directory_path.mkdir(parents=True, exist_ok=True)
                    if not directory_path.exists():
                        raise OSError(f"Failed to create directory: {directory_path}")
                    logging.debug("Directory ensured: %s", directory_path)
                except OSError as e:
                    error_logger.error("Failed to create directory %s: %s", directory_path, str(e))
                    # Continue with S3 upload even if local save fails
                log_audio_step("local_path_setup")
                
                # Save the file locally (always save as WAV for local storage)
                if convert_to_mp3:
                    # If converting to MP3, we need to save the original WAV first
                    # The MP3 conversion happens later for S3
                    audio_file.seek(0)  # Reset file pointer
                    audio_file.save(absolute_path)
                else:
                    audio_file.seek(0)  # Reset file pointer
                    audio_file.save(absolute_path)
                
                local_file_saved = True
                logging.info("File saved locally: %s", absolute_path)
                log_audio_step("local_file_save")
                
                # Calculate file size
                file_size = absolute_path.stat().st_size
                
                # Calculate duration from actual WAV file (more accurate than file size estimation)
                duration = None
                try:
                    import wave
                    with wave.open(str(absolute_path), 'rb') as wav_f:
                        frames = wav_f.getnframes()
                        rate = wav_f.getframerate()
                        duration = frames / float(rate)
                except Exception as e:
                    # Fallback to file size estimation if WAV file can't be read
                    logging.warning("Could not read WAV file for duration calculation, using file size estimation: %s", e)
                    duration = calculate_wav_duration(file_size)
                log_audio_step("audio_duration")
                
                # Create recording entry in database
                database_started_at = time.perf_counter()
                conn = connect_sqlite(DB_PATH)
                try:
                    cursor = conn.cursor()

                    db_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
                    relative_path = absolute_path.relative_to(DATA_ROOT).as_posix()
                    cursor.execute('''
                        INSERT INTO recordings (channel_id, filename, timestamp, transcription, status, is_duplicate, crc, filesize, duration)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (channel_id, relative_path, db_timestamp, 'No transcription available', 'queued', 1 if is_duplicate else 0, crc_value, file_size, duration))

                    recording_id = cursor.lastrowid
                    conn.commit()
                finally:
                    conn.close()
                    log_slow_operation(
                        database="recordings",
                        operation="insert_recording",
                        started_at=database_started_at,
                        rows=1,
                    )
                logging.info("Database entry created: recording_id=%s", recording_id)
                log_audio_step("database_insert")
                
                # Track file upload - file is saved and database entry created
                track_file_upload(mac_address.upper())
                
            except Exception as local_error:
                error_logger.error(f"Failed to save file locally: {str(local_error)}")
                # Continue with S3 upload even if local save fails
                local_file_saved = False
        log_audio_step("local_persistence")

         # Queue for transcription if file was saved locally (do this BEFORE S3 upload check)
        if local_file_saved and channel_id is not None and relative_path:
            try:
                audio_handler = get_audio_handler()
                success, result = audio_handler.queue_upload_for_processing(
                    relative_path, channel_id, is_duplicate=is_duplicate
                )
                if success:
                    logging.debug("File queued for transcription: %s", relative_path)
                else:
                    logging.warning("Failed to queue file for transcription: %s", result)
            except Exception as queue_error:
                error_logger.error(f"Failed to queue file for transcription: {str(queue_error)}")
                # Don't fail the request if transcription queueing fails
        log_audio_step("transcription_queue")

        # Upload to S3 (only if enabled in settings)
        s3_filename = f"{absolute_path.stem}.{ext}"
        s3_key = f"{mac_folder}/{utc_now:%Y/%m/%d}/{s3_filename}"
        logging.debug("bucket=%s, s3_key=%s", bucket, s3_key)

        if not is_s3_enabled():
            log_audio_step("s3_enabled_check")
            logging.debug("S3 upload is disabled in settings, skipping S3 upload")
            # Use same success message format as when S3 is enabled, so device recognizes it as success
            response = {
                "message": "Audio uploaded successfully",
                "timestamp": utc_now.isoformat(),
            }
            if local_file_saved and recording_id:
                response["recording_id"] = recording_id
                response["channel_id"] = channel_id
                response["local_file"] = relative_path
                response["local_path"] = relative_path  # Device also checks for this key
                response["is_duplicate"] = is_duplicate
                if crc_value is not None:
                    response["crc"] = crc_value
            log_audio_step("response_build")
            return jsonify(response), 200
        log_audio_step("s3_enabled_check")
        
        # Ensure bucket exists (only once, using the configured bucket name)
        # Wrap S3 operations in try-except to prevent errors from reaching the device
        s3_upload_success = False
        try:
            ensure_bucket_exists(bucket)
        except Exception as s3_bucket_error:
            logging.error(f"S3 bucket check/creation failed: {str(s3_bucket_error)}")
            # Continue without S3 upload, but don't fail the request
        log_audio_step("s3_bucket_check")

        # ── If conversion requested, transcode on-the-fly using ffmpeg ─────────
        if convert_to_mp3:
            # Use the locally saved file if available, otherwise save to temp
            if local_file_saved and absolute_path and os.path.exists(absolute_path):
                wav_source = absolute_path
                temp_wav_created = False
            else:
                # Save to temp file if not already saved locally
                tmp_wav = NamedTemporaryFile(suffix=".wav", delete=False)
                audio_file.seek(0)  # Reset file pointer
                audio_file.save(tmp_wav.name)
                tmp_wav.close()
                wav_source = tmp_wav.name
                temp_wav_created = True
            
            tmp_mp3 = wav_source.replace(".wav", ".mp3")
            try:
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    wav_source,
                    "-codec:a", "libmp3lame",
                    "-qscale:a", "0",           # highest quality VBR
                    "-ar", "8000",              # preserve original sample rate
                    "-ac", "1",                 # preserve mono channel
                    tmp_mp3,
                ]
                subprocess.run(ffmpeg_cmd, check=True)
                with open(tmp_mp3, "rb") as mp3_file:
                    upload_source = mp3_file.read()
            finally:
                # Only remove temp file if we created it (not the local saved file)
                if temp_wav_created and os.path.exists(wav_source):
                    os.remove(wav_source)
                if os.path.exists(tmp_mp3):
                    os.remove(tmp_mp3)
            upload_source = io.BytesIO(upload_source)
        else:
            # If file was already saved locally, read from there; otherwise use the file object
            if local_file_saved and absolute_path and os.path.exists(absolute_path):
                upload_source = open(absolute_path, 'rb')
            else:
                audio_file.seek(0)  # Reset file pointer
                upload_source = audio_file
        log_audio_step("audio_conversion")

        extra_args = {}
        if tagging_header:
            extra_args["Tagging"] = tagging_header
        # Optionally set ContentType so the browser knows what it is
        extra_args.setdefault(
            "ContentType", "audio/mpeg" if convert_to_mp3 else "audio/wav"
        )

        # Upload to S3 (catch errors to prevent device from receiving S3-related errors)
        try:
            client = get_s3_client()
            if not client:
                logging.warning("S3 client not available - credentials not configured")
            else:
                client.upload_fileobj(upload_source, bucket, s3_key, ExtraArgs=extra_args)
                s3_upload_success = True
                logging.info("Audio uploaded to S3 - Bucket: %s, S3_Key: %s", bucket, s3_key)
        except Exception as s3_upload_error:
            # Log the error but don't fail the request - device should always get success response
            error_logger.error(f"S3 upload failed (will retry in backup): {str(s3_upload_error)}")
            logging.warning(f"S3 upload temporarily failed for {s3_key}, but request succeeded. Error: {str(s3_upload_error)}")
        finally:
            # Close file handle if we opened a local file
            if not convert_to_mp3 and local_file_saved and absolute_path and os.path.exists(absolute_path):
                if hasattr(upload_source, 'close'):
                    upload_source.close()
        log_audio_step("s3_upload")

        response = {
            "message": "Audio uploaded successfully",
            "timestamp": utc_now.isoformat(),
        }
        if local_file_saved and recording_id:
            response["recording_id"] = recording_id
            response["channel_id"] = channel_id
            response["local_file"] = relative_path
            response["local_path"] = relative_path  # Device also checks for this key
            response["is_duplicate"] = is_duplicate
            if crc_value is not None:
                response["crc"] = crc_value

        log_audio_step("response_build")
        return jsonify(response), 200

    except Exception as exc:
        log_audio_step("error_handling")
        logging.exception("Failed during S3 upload flow: %s", exc)


@device_bp.route('/V1/upload/logs', methods=['POST'])
@device_bp.route('/v1/upload/logs', methods=['POST'])
@require_permission(
    ['device'], loader=load_request_channel, inject_as='channel'
)
@swag_from({
    'tags': ['Logs'],
    'summary': 'Upload log files from ESP32 devices',
    'security': [{'BearerAuth': []}],
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        },
        {
            'name': 'filename',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Original filename from device (e.g., 2025-11-16-LOG)'
        },
        {
            'name': 'file',
            'in': 'formData',
            'type': 'file',
            'required': True,
            'description': 'Log file to upload (.txt, .log, or .json)'
        }
    ],
    'responses': {
        '200': {
            'description': 'Log file uploaded successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'timestamp': {'type': 'string', 'format': 'date-time'},
                    'file_path': {'type': 'string'},
                    'warning': {'type': 'string'}
                },
                'required': ['message', 'timestamp', 'file_path']
            }
        },
        '400': {'description': 'Bad request - Missing required fields or invalid file type'},
        '500': {'description': 'Internal server error'}
    }
})
def upload_logs(channel):
    """Upload log files from ESP32 devices. Files are stored under /logs/<devicemac>/YYYY/MM/YYYY-MM-DD.log or .txt"""
    
    # Validate form data - wrap in try-except to handle connection errors gracefully
    try:
        if 'mac_address' not in request.form or 'filename' not in request.form:
            return jsonify({'error': 'Missing required fields (mac_address and filename)'}), 400
        
        if 'file' not in request.files:
            return jsonify({'error': 'No file part in the request'}), 400
        
        mac_address = channel['mac']
        original_filename = request.form['filename']
        file = request.files['file']
    except (OSError, ConnectionResetError, ConnectionError) as e:
        # Handle cases where client disconnects before full request is received
        error_msg = str(e)
        if "unexpected end of file" in error_msg.lower() or "connection reset" in error_msg.lower():
            logging.warning(f"Client disconnected during log upload: {error_msg}")
            return (
                jsonify({"error": "Upload interrupted - connection closed by client"}),
                499,  # 499 Client Closed Request (non-standard but appropriate)
            )
        else:
            logging.error(f"Connection error during form data parsing: {error_msg}")
            return (
                jsonify({"error": "Connection error during upload"}),
                400,
            )
    except Exception as e:
        # Catch any other unexpected errors during form parsing
        logging.error(f"Unexpected error during form data parsing: {str(e)}")
        return (
            jsonify({"error": "Failed to parse request data"}),
            400,
        )
    
    if file.filename == '':
        return jsonify({'error': 'No file selected for uploading'}), 400
    
    # Validate file extension (cloud API also allows .json)
    allowed_extensions = {'.txt', '.log', '.json'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in allowed_extensions:
        return jsonify({
            'error': f'Invalid file type. Only .txt, .log, and .json files are allowed. Received: {file_ext}'
        }), 400
    
    # Parse date from filename (format: 2025-11-16-LOG)
    # Extract date part (YYYY-MM-DD) from filename
    try:
        # Try to extract date from filename like "2025-11-16-LOG" or "2025-11-16-LOG.txt"
        date_match = re.match(r'(\d{4}-\d{2}-\d{2})', original_filename)
        if not date_match:
            # If no date in filename, use current UTC time
            utc_now = datetime.now(timezone.utc)
            warning = 'Could not parse date from filename, using current UTC time'
        else:
            date_str = date_match.group(1)
            # Parse the date and create a UTC datetime for that date at midnight
            parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
            # Create timezone-aware datetime in UTC
            utc_now = parsed_date.replace(tzinfo=timezone.utc)
            warning = None
    except ValueError as e:
        # If date parsing fails, use current UTC time
        utc_now = datetime.now(timezone.utc)
        warning = f'Error parsing date from filename: {str(e)}, using current UTC time'
    
    # Normalize MAC address to lowercase
    mac = mac_address.lower() if mac_address else None
    if not mac:
        return jsonify({'error': 'Invalid MAC address'}), 400
    
    # Generate file path: logs/<MAC>/YYYY/MM/YYYY-MM-DD.log or .txt
    year = utc_now.strftime('%Y')
    month = utc_now.strftime('%m')
    date_str = utc_now.strftime('%Y-%m-%d')
    
    # Use the file extension from the uploaded file
    final_filename = f'{date_str}{file_ext}'
    
    # Build relative path: logs/<MAC>/YYYY/MM/YYYY-MM-DD.log or .txt
    relative_path = os.path.join('logs', mac, year, month, final_filename)
    
    # Build absolute path
    absolute_path = DATA_ROOT / relative_path
    
    # Create directory for the file if it doesn't exist
    directory_path = os.path.dirname(absolute_path)
    try:
        os.makedirs(directory_path, exist_ok=True)
    except OSError as e:
        error_logger.error(f"Error creating directory {directory_path}: {str(e)}")
        return jsonify({'error': f'Failed to create directory: {str(e)}'}), 500
    
    # Save the file
    try:
        file.save(absolute_path)
        event_logger.info(f"Log file uploaded: MAC={mac}, File={final_filename}, Path={relative_path}")
    except Exception as e:
        error_logger.error(f"Error saving log file: {str(e)}")
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500
    
    timestamp = datetime.now(timezone.utc).isoformat()
    response = {
        'message': 'Log file uploaded successfully',
        'timestamp': timestamp,
        'file_path': relative_path
    }
    
    if warning:
        response['warning'] = warning
    
    return jsonify(response), 200

@device_bp.route('/v1/settings', methods=['POST'])
@require_permission(
    ['device', 'channel.update'], loader=load_request_channel, inject_as='channel'
)
@swag_from({
    'tags': ['Settings'],
    'summary': 'Save device settings for Boondock devices',
    'security': [{'BearerAuth': []}],
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        },
        {
            'name': 'settings',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'JSON string containing device settings'
        }
    ],
    'responses': {
        '200': {
            'description': 'Settings saved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'timestamp': {'type': 'string', 'format': 'date-time'},
                    'mac_address': {'type': 'string'}
                },
                'required': ['message', 'timestamp', 'mac_address']
            }
        },
        '400': {'description': 'Bad request - Missing required fields or invalid JSON'},
        '500': {'description': 'Server error - Failed to save settings'}
    }
})
def save_device_settings(channel):
    """Save device settings for Boondock devices."""
    if 'mac_address' not in request.form or 'settings' not in request.form:
        return jsonify({'error': 'Missing required fields (mac_address and settings)'}), 400

    mac_address = channel['mac']
    settings_str = request.form['settings']

    try:
        settings = json.loads(settings_str)
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON format for settings'}), 400

    filepath = os.path.join(DEVICE_SETTINGS_DIR, f"{mac_address}.json")
    try:
        with open(filepath, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        return jsonify({'error': f'Failed to save settings: {str(e)}'}), 500

    utc_now = datetime.now(timezone.utc)
    log_message = f"Settings saved - Device: {mac_address}, Filepath: {filepath}"
    logging.info(log_message)

    response = {
        'message': 'Settings saved successfully',
        'timestamp': utc_now.isoformat(),
        'mac_address': mac_address
    }
    return jsonify(response), 200


@device_bp.route('/v1/settings/<mac>', methods=['GET'])
@require_permission(
    ['device', 'channel.read'],
    loader=load_request_channel,
    id_argument='mac',
    inject_as='channel',
)
@swag_from({
    'tags': ['Settings'],
    'summary': 'Retrieve device settings for Boondock devices',
    'security': [{'BearerAuth': []}],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        }
    ],
    'responses': {
        '200': {
            'description': 'Settings retrieved successfully',
            'schema': {
                'type': 'object',
                'additionalProperties': True
            }
        },
        '404': {'description': 'Settings not found for this device'},
        '500': {'description': 'Server error - Failed to read settings'}
    }
})
def get_device_settings(mac, channel):
    """Retrieve device settings for Boondock devices."""
    mac_address = channel['mac']
    filepath = os.path.join(DEVICE_SETTINGS_DIR, f"{mac_address}.json")
    if not os.path.exists(filepath):
        return jsonify({'error': 'Settings not found for this device'}), 404

    try:
        with open(filepath, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        return jsonify({'error': f'Failed to read settings: {str(e)}'}), 500

    log_message = f"Settings retrieved - Device: {mac_address}, Filepath: {filepath}"
    logging.info(log_message)

    return jsonify(settings), 200


@device_bp.route('/v1/firmware/check', methods=['GET'])
@require_permission(['device'])
@swag_from({
    "tags": ["Firmware"],
    "summary": "Check for device firmware upgrade (cloud-style API)",
    "parameters": [
        {
            "name": "current_version",
            "in": "query",
            "type": "string",
            "required": True,
            "description": "Installed firmware version e.g. 1.0.0",
        }
    ],
    "responses": {
        "200": {"description": "Upgrade status"},
        "400": {"description": "Missing current_version"},
    },
})
def firmware_check():
    cv = request.args.get("current_version")
    if not cv or not str(cv).strip():
        return jsonify({"error": "Missing current_version parameter"}), 400
    cv = str(cv).strip()
    try:
        from ..services.firmware_device_service import find_upgrade_for_device

        base = request.url_root.rstrip("/")
        up = find_upgrade_for_device(cv)
        if not up:
            return (
                jsonify(
                    {
                        "upgrade_available": False,
                        "message": "Device is up to date",
                        "download_link": None,
                        "target_version": None,
                    }
                ),
                200,
            )
        fid, ver, desc = up
        link = f"{base}/api/v1/firmware/download/{fid}/firmware.bin"
        return (
            jsonify(
                {
                    "upgrade_available": True,
                    "message": f"Upgrade available to version {ver}",
                    "download_link": link,
                    "description": desc,
                    "target_version": ver,
                }
            ),
            200,
        )
    except Exception:
        logging.exception("firmware check")
        return (
            jsonify({"error": "Unexpected error while checking firmware"}),
            500,
        )


@device_bp.route('/v1/firmware/download/<firmware_id>/<filename>', methods=['GET'])
@require_permission(['device'])
def firmware_download(firmware_id, filename):
    """Serve OTA binaries from managed firmware storage."""
    if filename not in ("firmware.bin", "bootloader.bin", "partitions.bin"):
        return jsonify({"error": "Not found"}), 404
    try:
        from ..services.firmware_device_service import get_firmware_file_path

        path = get_firmware_file_path(firmware_id, filename)
        if not path:
            return jsonify({"error": "Not found"}), 404
        return send_file(
            path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/octet-stream",
        )
    except Exception:
        logging.exception("firmware download")
        return jsonify({"error": "Unexpected error"}), 500
