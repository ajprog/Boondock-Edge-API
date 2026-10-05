"""Administrator-only category settings and system utility routes."""
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from ..middleware.auth_middleware import require_admin
import pytz

from ..services.settings_manager import get_settings_manager
from ..services.db_logging_manager import LOGS_DB_PATH
import subprocess
import sys
from config import Config, CODE_ROOT

_settings_manager = get_settings_manager()

settings_bp = Blueprint('settings', __name__)

SUMMARY_METRICS_CACHE_TTL_SECONDS = 600  # 10 minutes
_summary_metrics_cache_lock = threading.Lock()
_summary_metrics_cache = {}


def _resolve_timezone_name(explicit_timezone=None):
        return 'UTC'


def _get_local_day_bounds(timezone_name):
    """Return local-day UTC bounds and local date string for the supplied timezone."""
    tz = pytz.timezone(timezone_name)
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return {
        'local_date': local_now.strftime('%Y-%m-%d'),
        'recordings_start_utc': int(local_start.astimezone(timezone.utc).timestamp() * 1000),
        'recordings_end_utc': int(local_end.astimezone(timezone.utc).timestamp() * 1000),
        'logs_start': local_start.strftime('%Y-%m-%d %H:%M:%S'),
        'logs_end': local_end.strftime('%Y-%m-%d %H:%M:%S'),
        'timezone': timezone_name,
    }


def _summary_cache_get(cache_key):
    """Return cached summary payload when still fresh."""
    now_ts = datetime.now(timezone.utc).timestamp()
    with _summary_metrics_cache_lock:
        entry = _summary_metrics_cache.get(cache_key)
        if not entry:
            return None
        cached_at = entry.get('cached_at_ts', 0)
        if now_ts - cached_at > SUMMARY_METRICS_CACHE_TTL_SECONDS:
            return None
        return entry.get('payload')


def _summary_cache_set(cache_key, payload):
    """Store summary payload in memory cache."""
    with _summary_metrics_cache_lock:
        _summary_metrics_cache[cache_key] = {
            'cached_at_ts': datetime.now(timezone.utc).timestamp(),
            'payload': payload,
        }


@settings_bp.route('/settings/summary/metrics', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Settings'],
    'summary': 'Get lightweight dashboard summary metrics',
    'parameters': [
        {
            'name': 'timezone',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'IANA timezone (defaults to global setting)'
        }
    ],
    'responses': {
        '200': {'description': 'Summary metrics returned successfully'},
        '500': {'description': 'Server error'}
    }
})
def get_summary_metrics():
    """
    Return summary metrics optimized for scale.
    Uses aggregate SQL counts instead of fetching full recordings/log datasets.
    """
    try:
        requested_tz = request.args.get('timezone')
        timezone_name = _resolve_timezone_name(requested_tz)
        force_refresh = str(request.args.get('force_refresh', 'false')).lower() == 'true'
        cache_key = timezone_name

        if not force_refresh:
            cached_payload = _summary_cache_get(cache_key)
            if cached_payload is not None:
                payload = dict(cached_payload)
                payload['is_cached'] = True
                return jsonify(payload), 200

        bounds = _get_local_day_bounds(timezone_name)

        total_recordings = 0
        today_recordings = 0
        try:
            recordings_db = Config.get_recordings_db_path()
            conn = sqlite3.connect(recordings_db, timeout=5.0)
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM recordings')
            total_recordings = int(cur.fetchone()[0] or 0)
            cur.execute(
                '''
                SELECT COUNT(*)
                FROM recordings
                WHERE timestamp >= ? AND timestamp < ?
                ''',
                (bounds['recordings_start_utc'], bounds['recordings_end_utc'])
            )
            today_recordings = int(cur.fetchone()[0] or 0)
            conn.close()
        except Exception as recordings_error:
            logging.warning(f"Summary recordings query failed: {recordings_error}")

        errors = 0
        warnings = 0
        try:
            conn = sqlite3.connect(LOGS_DB_PATH, timeout=5.0)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM errors WHERE timestamp >= ? AND timestamp < ?",
                (bounds['logs_start'], bounds['logs_end'])
            )
            errors = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM warnings WHERE timestamp >= ? AND timestamp < ?",
                (bounds['logs_start'], bounds['logs_end'])
            )
            warnings = int(cur.fetchone()[0] or 0)
            conn.close()
        except Exception as logs_error:
            logging.warning(f"Summary logs query failed: {logs_error}")

        payload = {
            'total_recordings': total_recordings,
            'today_recordings': today_recordings,
            'errors': errors,
            'warnings': warnings,
            'total_users': _settings_manager.count_users(),
            'timezone': bounds['timezone'],
            'local_date': bounds['local_date'],
            'cached_for_seconds': SUMMARY_METRICS_CACHE_TTL_SECONDS,
            'is_cached': False,
        }
        _summary_cache_set(cache_key, payload)
        return jsonify(payload), 200
    except Exception as e:
        logging.error(f"Error building summary metrics: {e}")
        return jsonify({'error': 'Failed to get summary metrics'}), 500

def _request_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Request body must be a JSON object')
    return data


def _reject_unknown_fields(data, allowed):
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(unknown)}")


def _require_type(data, field, expected_type):
    if field in data and not isinstance(data[field], expected_type):
        raise ValueError(f'{field} has an invalid type')


def _secret_value(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    return value if value.strip() else ''


def _save_settings(changes):
    if changes and not _settings_manager.set_all_settings(changes):
        raise RuntimeError('Failed to save settings')


def _settings_error(error):
    status = 400 if isinstance(error, ValueError) else 500
    return jsonify({'error': str(error)}), status


@settings_bp.route('/settings/transcription', methods=['GET'])
@require_admin
def get_transcription_settings():
    settings = _settings_manager.get_all_settings()
    return jsonify({
        'target_language': settings.get('target_language', 'english'),
        'model': settings.get('model', 'tiny.en'),
        'method': settings.get('method', 'local'),
        'queue_enabled': bool(settings.get('transcription_queue_enabled', True)),
        'api_key_configured': bool(settings.get('transcription_api_key')),
    })


@settings_bp.route('/settings/transcription', methods=['PATCH'])
@require_admin
def update_transcription_settings():
    try:
        data = _request_object()
        _reject_unknown_fields(
            data,
            {'target_language', 'model', 'method', 'queue_enabled', 'api_key'},
        )
        for field in ('target_language', 'model', 'method'):
            _require_type(data, field, str)
        if data.get('method', 'local') not in {'local', 'openai'}:
            raise ValueError("method must be 'local' or 'openai'")
        _require_type(data, 'queue_enabled', bool)
        changes = {key: data[key] for key in ('target_language', 'model', 'method') if key in data}
        if 'queue_enabled' in data:
            changes['transcription_queue_enabled'] = data['queue_enabled']
        if 'api_key' in data:
            changes['transcription_api_key'] = _secret_value(data['api_key'], 'api_key')
        _save_settings(changes)
        if 'queue_enabled' in data:
            from ..services.audio_handler import get_audio_handler
            audio_handler = get_audio_handler()
            if data['queue_enabled'] and not audio_handler.running:
                audio_handler.start()
            elif not data['queue_enabled'] and audio_handler.running:
                audio_handler.stop_queue()
        if changes:
            try:
                from ..services.audio_handler import reload_transcription_settings
                reload_transcription_settings()
            except Exception as error:
                logging.warning('Failed to reload transcription settings: %s', error)
        return get_transcription_settings()
    except Exception as error:
        return _settings_error(error)


@settings_bp.route('/settings/audio-processing', methods=['GET'])
@require_admin
def get_audio_processing_settings():
    return jsonify({'hallucination_patterns': _settings_manager.get_all_hallucinations()})


@settings_bp.route('/settings/audio-processing', methods=['PATCH'])
@require_admin
def update_audio_processing_settings():
    try:
        data = _request_object()
        _reject_unknown_fields(data, {'hallucination_patterns'})
        if 'hallucination_patterns' in data:
            _settings_manager.replace_hallucinations(data['hallucination_patterns'])
        return get_audio_processing_settings()
    except Exception as error:
        return _settings_error(error)


@settings_bp.route('/settings/recorders', methods=['GET'])
@require_admin
def get_recorder_settings():
    settings = _settings_manager.get_all_settings()
    return jsonify({
        'uniden_scanners_enabled': bool(settings.get('uniden_scanners_enabled', False)),
        'edge_recorders_enabled': bool(settings.get('edge_recorders_enabled', True)),
    })


@settings_bp.route('/settings/recorders', methods=['PATCH'])
@require_admin
def update_recorder_settings():
    try:
        data = _request_object()
        fields = {'uniden_scanners_enabled', 'edge_recorders_enabled'}
        _reject_unknown_fields(data, fields)
        for field in fields:
            _require_type(data, field, bool)
        _save_settings({key: data[key] for key in fields if key in data})
        return get_recorder_settings()
    except Exception as error:
        return _settings_error(error)


@settings_bp.route('/settings/wifi', methods=['GET'])
@require_admin
def get_wifi_settings():
    settings = _settings_manager.get_all_settings()
    try:
        host_port = int(settings.get('host_port', 4000))
    except (TypeError, ValueError):
        host_port = 4000
    return jsonify({
        'ssid': settings.get('ssid', ''),
        'password_configured': bool(settings.get('host_password')),
        'host_ip': settings.get('host_ip', ''),
        'host_port': host_port,
    })


@settings_bp.route('/settings/wifi', methods=['PATCH'])
@require_admin
def update_wifi_settings():
    try:
        data = _request_object()
        _reject_unknown_fields(data, {'ssid', 'password', 'host_ip', 'host_port'})
        for field in ('ssid', 'host_ip'):
            _require_type(data, field, str)
        if 'host_port' in data and (isinstance(data['host_port'], bool) or
                                    not isinstance(data['host_port'], int) or
                                    not 1 <= data['host_port'] <= 65535):
            raise ValueError('host_port must be an integer between 1 and 65535')
        changes = {key: data[key] for key in ('ssid', 'host_ip', 'host_port') if key in data}
        if 'password' in data:
            changes['host_password'] = _secret_value(data['password'], 'password')
        _save_settings(changes)
        return get_wifi_settings()
    except Exception as error:
        return _settings_error(error)


@settings_bp.route('/settings/backup', methods=['GET'])
@require_admin
def get_backup_settings():
    settings = _settings_manager.get_all_settings()
    return jsonify({
        's3_enabled': bool(settings.get('s3_enabled', False)),
        's3_endpoint_url': settings.get('s3_endpoint_url', ''),
        's3_access_key_configured': bool(settings.get('s3_access_key')),
        's3_secret_key_configured': bool(settings.get('s3_secret_key')),
        's3_region': settings.get('s3_region', 'us-east-1'),
        's3_bucket_name': settings.get('s3_bucket_name', ''),
        'samba_enabled': bool(settings.get('samba_enabled', False)),
        'samba_share_path': settings.get('samba_share_path', ''),
        'samba_username': settings.get('samba_username', ''),
        'samba_password_configured': bool(settings.get('samba_password')),
    })


@settings_bp.route('/settings/backup', methods=['PATCH'])
@require_admin
def update_backup_settings():
    try:
        data = _request_object()
        plain_fields = {
            's3_enabled', 's3_endpoint_url', 's3_region', 's3_bucket_name',
            'samba_enabled', 'samba_share_path', 'samba_username',
        }
        secret_fields = {'s3_access_key', 's3_secret_key', 'samba_password'}
        _reject_unknown_fields(data, plain_fields | secret_fields)
        for field in ('s3_enabled', 'samba_enabled'):
            _require_type(data, field, bool)
        for field in plain_fields - {'s3_enabled', 'samba_enabled'}:
            _require_type(data, field, str)
        changes = {key: data[key] for key in plain_fields if key in data}
        for field in secret_fields:
            if field in data:
                changes[field] = _secret_value(data[field], field)
        _save_settings(changes)
        return get_backup_settings()
    except Exception as error:
        return _settings_error(error)


@settings_bp.route('/settings/maintenance', methods=['GET'])
@require_admin
def get_maintenance_settings():
    settings = _settings_manager.get_all_settings()
    return jsonify({
        'scheduled_time': settings.get('scheduled_time', '03:00'),
        'enabled_tasks': settings.get(
            'enabled_tasks', ['data_backup', 'logs_cleanup', 'health_checks']
        ),
    })


@settings_bp.route('/settings/maintenance', methods=['PATCH'])
@require_admin
def update_maintenance_settings():
    try:
        data = _request_object()
        _reject_unknown_fields(data, {'scheduled_time', 'enabled_tasks'})
        if 'scheduled_time' in data:
            if not isinstance(data['scheduled_time'], str):
                raise ValueError('scheduled_time must be a string')
            try:
                parsed = datetime.strptime(data['scheduled_time'], '%H:%M')
            except ValueError as error:
                raise ValueError('scheduled_time must use HH:MM') from error
            if parsed.strftime('%H:%M') != data['scheduled_time']:
                raise ValueError('scheduled_time must use HH:MM')
        valid_tasks = {'data_backup', 'logs_cleanup', 'health_checks'}
        if 'enabled_tasks' in data:
            tasks = data['enabled_tasks']
            if (not isinstance(tasks, list) or
                    any(not isinstance(task, str) or task not in valid_tasks for task in tasks)):
                raise ValueError('enabled_tasks contains an invalid task')
            if len(tasks) != len(set(tasks)):
                raise ValueError('enabled_tasks cannot contain duplicates')
        _save_settings({key: data[key] for key in ('scheduled_time', 'enabled_tasks') if key in data})
        if data:
            try:
                from ..services.maintenance_scheduler import restart_scheduler
                restart_scheduler()
            except Exception as error:
                logging.warning('Failed to restart maintenance scheduler: %s', error)
        return get_maintenance_settings()
    except Exception as error:
        return _settings_error(error)


@settings_bp.route('/settings/restart-service', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Settings'],
    'summary': 'Restart the Boondock Edge system service',
    'responses': {
        '200': {'description': 'Service restart triggered successfully'},
        '500': {'description': 'Server error'}
    }
})
def restart_system_service():
    """
    Restart the underlying Boondock Edge systemd service.

    This endpoint is intended to be called from the Settings UI via a restart button.
    """
    try:
        # Only attempt restart on Linux environments
        if sys.platform != "linux":
            return jsonify({'error': 'Service restart is only supported on Linux targets'}), 500

        # Determine restart script path:
        restart_script = CODE_ROOT / 'restart.sh'

        if not restart_script.exists():
            return jsonify({'error': f'restart.sh not found at {restart_script}'}), 500

        try:
            # Use bash to execute the restart script
            subprocess.run(
                ["/bin/bash", restart_script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError:
            return jsonify({'error': 'Failed to execute restart.sh'}), 500

        return jsonify({'message': 'restart.sh executed successfully'}), 200
    except Exception as e:
        logging.error(f"Error restarting system service: {str(e)}")
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/reboot', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Settings'],
    'summary': 'Reboot the Boondock Edge application service',
    'responses': {
        '200': {'description': 'Reboot (service restart) triggered successfully'},
        '500': {'description': 'Server error'}
    }
})
def reboot_application():
    """
    Alias endpoint for restarting the Boondock Edge systemd service.
    Exposed as /api/reboot.
    """
    # Reuse the same logic as restart_system_service (includes OPTIONS handling and Linux guard)
    return restart_system_service()


@settings_bp.route('/time', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Get current GMT time',
    'responses': {
        '200': {'description': 'Current GMT time in ISO 8601 format'}
    }
})
def get_gmt_time():
    """
    API endpoint to return the current GMT time in ISO 8601 format.
    """
    from datetime import datetime, timezone
    current_gmt_time = datetime.now(timezone.utc).isoformat()
    return jsonify({"gmt_time": current_gmt_time})


@settings_bp.route('/ping', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Health check endpoint',
    'responses': {
        '200': {'description': 'Server is healthy'}
    }
})
def ping():
    """
    Simple health check endpoint to verify server is running.
    """
    return jsonify({"status": "ok", "message": "Server is running"}), 200
