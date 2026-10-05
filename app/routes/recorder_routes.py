import json
import logging
import re
import sys
import threading
import time
from config import DATA_ROOT
from collections import deque
from datetime import datetime, timezone
import subprocess
import shutil
from pathlib import Path
from werkzeug.utils import secure_filename

from flask import Blueprint, jsonify, request
from ..middleware.auth_middleware import require_admin
from serial import Serial, SerialException
import serial.tools.list_ports
from app.services.recorder_monitor import (
    start_monitoring_for_device,
    stop_monitoring_for_device,
    is_monitoring_active,
    get_messages,
    initialize_monitoring,
    send_command_to_ports,
    run_autoconfig_sequence,
    reconnect_device,
    reconnect_all_monitored_devices,
    get_error_logs,
    get_all_parsed_data,
    set_port_flashing,
    get_port_mac_map,
    load_reboot_history,
    sync_reboot_counts_from_logs,
)
from app.services.recorder_config_transport import (
    read_recorder_config as read_recorder_config_from_device,
    write_recorder_config as write_recorder_config_to_device,
)

recorders_bp = Blueprint('recorders', __name__)

_logger = logging.getLogger(__name__)

# Project root is one level up from backend
from ..services.settings_manager import get_settings_manager
from ..services.db_logging_manager import get_db_logging_manager

_settings_manager = get_settings_manager()
RECORDER_CONFIG_DIR = DATA_ROOT / 'db' / 'recorder_configs'
FIRMWARE_STORAGE_DIR = DATA_ROOT / 'firmware'

_inventory_lock = threading.Lock()
_terminal_sessions = {}
_terminal_sessions_lock = threading.Lock()
_TERMINAL_IDLE_TIMEOUT = 300
_TERMINAL_MAX_CHUNKS = 1000

# Flash progress tracking
_flash_progress = {}
_flash_progress_lock = threading.Lock()

# Vendor/Product IDs and descriptor keywords commonly exposed by CP210x bridges used on ESP32 boards
_ESP32_VENDOR_IDS = {0x10C4}
_ESP32_PRODUCT_IDS = {0xEA60, 0xEA63, 0xEA70}
_ESP32_KEYWORDS = (
    'cp210',
    'silicon labs',
    'usb to uart',
    'esp32'
)


def _load_inventory():
    """Load recorder inventory from database."""
    try:
        records = _settings_manager.get_all_recorders()
        # Extract the 'data' field from each record (which contains the actual device data)
        devices = []
        for record in records:
            if 'data' in record:
                # If 'data' is already a dict, use it directly
                if isinstance(record['data'], dict):
                    devices.append(record['data'])
                else:
                    devices.append(record['data'])
            else:
                # If no 'data' field, use the record itself (for backward compatibility)
                devices.append(record)
        return devices
    except Exception as exc:
        _logger.error("Failed to load recorder inventory: %s", exc)
        return []


def _save_inventory(devices):
    """Save recorder inventory to database."""
    try:
        # Save each device to the database (save_recorder handles updates by port)
        for device in devices:
            _settings_manager.save_recorder(device)
    except Exception as exc:
        _logger.error("Failed to save recorder inventory: %s", exc)


def _ensure_config_storage():
    RECORDER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_port_key(port):
    return ''.join(char if char.isalnum() else '_' for char in port)


def _config_path_for_port(port):
    safe_name = _sanitize_port_key(port) + '_config'
    return RECORDER_CONFIG_DIR / f'{safe_name}.json'


def _load_stored_config(port):
    _ensure_config_storage()
    path = _config_path_for_port(port)
    if not path.exists():
        return None, None

    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        _logger.warning("Recorder config file for %s is unreadable. Ignoring.", port)
        return None, None

    if isinstance(data, dict) and 'config' in data:
        config = data.get('config')
        updated_at = data.get('updated_at')
    else:
        config = data
        updated_at = None

    return config, updated_at


def _store_config(port, config):
    _ensure_config_storage()
    path = _config_path_for_port(port)
    updated_at = datetime.utcnow().isoformat() + 'Z'
    payload = {
        'port': port,
        'config': config,
        'updated_at': updated_at
    }
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2)
    return updated_at


def _settings_allow_discovery():
    try:
        settings = _settings_manager.get_all_settings()
        return settings.get('edge_recorders_enabled', False)
    except Exception as e:
        _logger.error(f"Unable to get settings while checking recorder discovery flag: {e}")
        return False


def _matches_esp32_bridge(port_info):
    descriptor_parts = filter(None, [
        port_info.description,
        port_info.manufacturer,
        port_info.product,
        port_info.device
    ])
    descriptor = ' '.join(descriptor_parts).lower()

    if port_info.vid in _ESP32_VENDOR_IDS and port_info.pid in _ESP32_PRODUCT_IDS:
        return True

    return any(keyword in descriptor for keyword in _ESP32_KEYWORDS)


def _probe_port(port_name):
    try:
        with Serial(port=port_name, baudrate=115200, timeout=1) as connection:
            return connection.is_open
    except SerialException as exc:
        _logger.debug("Serial probe failed for %s: %s", port_name, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - capture unexpected issues
        _logger.debug("Unexpected error probing %s: %s", port_name, exc)
        return False


def _enumerate_recorders():
    devices = []
    # Load existing inventory to preserve monitor flags and reboot counts (but default to True for new devices)
    existing_inventory = _load_inventory()
    monitor_flags = {device.get('port'): device.get('monitor', True) for device in existing_inventory if 'port' in device}
    reboot_counts = {device.get('port'): device.get('reboot_count', 0) for device in existing_inventory if 'port' in device}
    last_reboots = {device.get('port'): device.get('last_reboot') for device in existing_inventory if 'port' in device}

    for port in serial.tools.list_ports.comports():
        if not _matches_esp32_bridge(port):
            continue

        status = 'available' if _probe_port(port.device) else 'busy'
        device_entry = {
            'port': port.device,
            'description': port.description,
            'manufacturer': port.manufacturer,
            'product': port.product,
            'serial_number': port.serial_number,
            'vid': f"0x{port.vid:04X}" if port.vid is not None else None,
            'pid': f"0x{port.pid:04X}" if port.pid is not None else None,
            'status': status,
            'last_seen': datetime.utcnow().isoformat() + 'Z',
            'monitor': monitor_flags.get(port.device, True),  # Default to True for all devices
            'reboot_count': reboot_counts.get(port.device, 0)  # Preserve reboot count
        }
        # Preserve last_reboot if it exists
        if port.device in last_reboots and last_reboots[port.device]:
            device_entry['last_reboot'] = last_reboots[port.device]
        devices.append(device_entry)

    return devices


def refresh_recorder_inventory():
    """Refresh the recorder inventory and persist it to disk."""
    with _inventory_lock:
        devices = _enumerate_recorders()
        _save_inventory(devices)
    _logger.info("Recorder inventory refresh complete. Discovered %d device(s).", len(devices))
    return devices


def initialize_recorder_inventory():
    if not _settings_allow_discovery():
        _logger.info("Boondock Edge device discovery disabled via global settings.")
        return {'enabled': False, 'devices': []}

    devices = refresh_recorder_inventory()
    
    # Initialize monitoring for devices flagged for monitoring
    try:
        initialize_monitoring()
    except Exception as exc:
        _logger.error("Failed to initialize recorder monitoring: %s", exc)
        # Don't fail the whole initialization if monitoring fails
    
    return {'enabled': True, 'devices': devices}


def _terminal_session_active(port):
    with _terminal_sessions_lock:
        session = _terminal_sessions.get(port)
        return bool(session and session._active)


def _send_simple_command(port, command, wait_after=0.0):
    with Serial(port=port, baudrate=115200, timeout=1, write_timeout=1) as connection:
        try:
            connection.reset_input_buffer()
            connection.reset_output_buffer()
        except (SerialException, AttributeError):
            pass

        command_payload = f'{command}\n'.encode('utf-8')
        connection.write(command_payload)
        connection.flush()
        if wait_after > 0:
            time.sleep(wait_after)


class SerialTerminalSession:
    """Maintain a live serial terminal session for a recorder device."""

    def __init__(self, port):
        self.port = port
        # Configure serial port with settings that handle device resets better
        self.serial = Serial(
            port=port,
            baudrate=115200,
            timeout=0.1,
            write_timeout=1.0,
            inter_byte_timeout=None,  # Don't timeout between bytes
            dsrdtr=False,  # Don't use DSR/DTR flow control
            rtscts=False   # Don't use RTS/CTS flow control
        )
        # Reset input/output buffers to start fresh
        try:
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
        except (SerialException, AttributeError):
            pass
        self._active = True
        self._chunks = deque(maxlen=_TERMINAL_MAX_CHUNKS)
        self._seq = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"RecorderTerminal-{port}",
            daemon=True
        )
        self.last_activity = time.time()
        self._reader_thread.start()

    def _append_chunk(self, chunk):
        if not chunk:
            return
        with self._lock:
            self._seq += 1
            self._chunks.append((self._seq, chunk))
            self.last_activity = time.time()

    def _reader_loop(self):
        consecutive_errors = 0
        max_consecutive_errors = 10
        error_backoff = 0.1
        
        while self._active:
            try:
                # Check if serial port is still open before reading
                if not self.serial or not self.serial.is_open:
                    _logger.warning("Serial port %s is not open, closing reader loop", self.port)
                    break
                
                data = self.serial.read(1024)
                if data:
                    text = data.decode('utf-8', errors='replace')
                    self._append_chunk(text)
                    consecutive_errors = 0  # Reset error count on successful read
                else:
                    time.sleep(0.05)
            except SerialException as exc:
                consecutive_errors += 1
                error_msg = str(exc).lower()
                
                # Check if this is a fatal error (port not found, permission denied, etc.)
                is_fatal = any(keyword in error_msg for keyword in [
                    'could not open port',
                    'access is denied',
                    'permission denied',
                    'no such file',
                    'device not found'
                ])
                
                if is_fatal:
                    _logger.error("Fatal serial error on %s: %s", self.port, exc)
                    self._append_chunk(f"\n[Fatal serial error: {exc}]\n")
                    break
                
                # For non-fatal errors, log and continue with backoff
                if consecutive_errors <= max_consecutive_errors:
                    _logger.warning("Serial error on %s (attempt %d/%d): %s", 
                                  self.port, consecutive_errors, max_consecutive_errors, exc)
                    self._append_chunk(f"\n[Serial warning: {exc}]\n")
                    time.sleep(error_backoff * consecutive_errors)  # Exponential backoff
                else:
                    _logger.error("Too many consecutive serial errors on %s, closing connection", self.port)
                    self._append_chunk(f"\n[Too many serial errors, connection closed]\n")
                    break
            except Exception as exc:  # noqa: BLE001 - unexpected issues logged for debugging
                consecutive_errors += 1
                _logger.exception("Unexpected error reading serial port %s", self.port)
                self._append_chunk(f"\n[Unexpected error: {exc}]\n")
                
                if consecutive_errors > max_consecutive_errors:
                    _logger.error("Too many consecutive errors on %s, closing connection", self.port)
                    break
                time.sleep(error_backoff * consecutive_errors)

        self._active = False
        try:
            if self.serial and self.serial.is_open:
                self.serial.close()
        except SerialException:
            _logger.debug("Failed to close serial connection for %s during shutdown", self.port)

    def read_since(self, last_seq):
        with self._lock:
            if not self._chunks:
                oldest_seq = last_seq + 1
            else:
                oldest_seq = self._chunks[0][0]
            truncated = last_seq < oldest_seq - 1
            messages = [(seq, chunk) for seq, chunk in self._chunks if seq > last_seq]
            latest_seq = self._seq
            self.last_activity = time.time()
        return messages, latest_seq, truncated

    def write(self, payload):
        if not payload:
            return
        if not self._active:
            raise SerialException("Session is closed")
        encoded = payload.encode('utf-8', errors='replace')
        with self._write_lock:
            self.serial.write(encoded)
            self.serial.flush()
        self.last_activity = time.time()

    def close(self):
        self._active = False
        try:
            self.serial.close()
        except SerialException:
            _logger.debug("Serial connection already closed for %s", self.port)


def _cleanup_terminal_sessions():
    now = time.time()
    with _terminal_sessions_lock:
        expired_ports = [
            port for port, session in _terminal_sessions.items()
            if (not session._active) or (now - session.last_activity > _TERMINAL_IDLE_TIMEOUT)
        ]
        for port in expired_ports:
            session = _terminal_sessions.pop(port, None)
            if session:
                session.close()
                _logger.info("Closed idle recorder terminal session for %s", port)


@recorders_bp.route('/devices', methods=['GET'])
@require_admin
def list_recorders():
    """Return the cached list of ESP32 recorder devices."""
    enabled = _settings_allow_discovery()
    devices = _load_inventory() if enabled else []
    return jsonify({
        'enabled': enabled,
        'devices': devices,
        'count': len(devices)
    })


@recorders_bp.route('/refresh', methods=['POST'])
@require_admin
def refresh_recorders():
    """Trigger a discovery run and return the updated device list."""
    if not _settings_allow_discovery():
        return jsonify({
            'enabled': False,
            'devices': [],
            'count': 0,
            'message': 'Enable Boondock Edge devices in Global settings to scan for recorders.'
        }), 400

    devices = refresh_recorder_inventory()
    return jsonify({
        'enabled': True,
        'devices': devices,
        'count': len(devices)
    })


@recorders_bp.route('/devices/<path:port>', methods=['DELETE'])
@require_admin
def delete_recorder(port):
    """Delete a recorder from inventory by port."""
    try:
        # Stop monitoring for this port first
        from ..services.recorder_monitor import stop_monitoring_for_device
        stop_monitoring_for_device(port)
        
        # Delete from database
        deleted = _settings_manager.delete_recorder(port)
        
        if deleted:
            return jsonify({
                'success': True,
                'message': f'Recorder on port {port} has been deleted.'
            })
        else:
            return jsonify({
                'success': False,
                'message': f'No recorder found on port {port}.'
            }), 404
    except Exception as exc:
        _logger.error("Failed to delete recorder: %s", exc)
        return jsonify({
            'success': False,
            'message': f'Error deleting recorder: {str(exc)}'
        }), 500


@recorders_bp.route('/config', methods=['GET'])
@require_admin
def get_recorder_config():
    port = request.args.get('port')
    if not port:
        return jsonify({'message': 'Port is required.'}), 400

    config, updated_at = _load_stored_config(port)
    if config is None:
        return jsonify({'message': f'No saved configuration for {port}.'}), 404

    return jsonify({'port': port, 'config': config, 'updated_at': updated_at})


@recorders_bp.route('/config', methods=['PUT'])
@require_admin
def update_recorder_config():
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    config = payload.get('config')

    if not port:
        return jsonify({'message': 'Port is required.'}), 400
    if config is None:
        return jsonify({'message': 'Config payload is required.'}), 400

    if not isinstance(config, (dict, list)):
        return jsonify({'message': 'Config must be a JSON object or array.'}), 400

    updated_at = _store_config(port, config)
    return jsonify({'message': 'Configuration saved.', 'port': port, 'updated_at': updated_at})


@recorders_bp.route('/config/read', methods=['POST'])
@require_admin
def read_recorder_config():
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')

    if not port:
        return jsonify({'message': 'Port is required.'}), 400

    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403

    if _terminal_session_active(port):
        return jsonify({'message': f'Close the open terminal session for {port} before reading configuration.'}), 409

    try:
        config = read_recorder_config_from_device(port)
    except FileNotFoundError:
        return jsonify({'message': f'Port {port} not found.'}), 404
    except TimeoutError as exc:
        return jsonify({'message': str(exc)}), 504
    except SerialException as exc:
        return jsonify({'message': f'Unable to read config from {port}: {exc}'}), 500
    except Exception as exc:  # noqa: BLE001
        _logger.exception('Unexpected error while reading config from %s', port)
        return jsonify({'message': f'Unexpected error while reading config from {port}: {exc}'}), 500

    updated_at = _store_config(port, config)
    return jsonify({'message': 'Configuration read from device.', 'port': port, 'config': config, 'updated_at': updated_at})


@recorders_bp.route('/config/write', methods=['POST'])
@require_admin
def write_recorder_config():
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    config_override = payload.get('config')

    if not port:
        return jsonify({'message': 'Port is required.'}), 400

    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403

    if _terminal_session_active(port):
        return jsonify({'message': f'Close the open terminal session for {port} before writing configuration.'}), 409

    if config_override is not None and not isinstance(config_override, (dict, list)):
        return jsonify({'message': 'Config override must be a JSON object or array.'}), 400

    if config_override is None:
        config, _updated_at = _load_stored_config(port)
        if config is None:
            return jsonify({'message': f'No saved configuration for {port}. Read or upload a config before writing.'}), 404
    else:
        config = config_override
        _store_config(port, config)  # Keep disk copy in sync with override

    try:
        write_recorder_config_to_device(port, config)
    except FileNotFoundError:
        return jsonify({'message': f'Port {port} not found.'}), 404
    except TimeoutError as exc:
        return jsonify({'message': str(exc)}), 504
    except SerialException as exc:
        return jsonify({'message': f'Unable to write config to {port}: {exc}'}), 500
    except Exception as exc:  # noqa: BLE001
        _logger.exception('Unexpected error while writing config to %s', port)
        return jsonify({'message': f'Unexpected error while writing config to {port}: {exc}'}), 500

    return jsonify({'message': 'Configuration written to device and reboot command issued.', 'port': port})


@recorders_bp.route('/config/reboot', methods=['POST'])
@require_admin
def reboot_recorder():
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')

    if not port:
        return jsonify({'message': 'Port is required.'}), 400

    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403

    if _terminal_session_active(port):
        return jsonify({'message': f'Close the open terminal session for {port} before rebooting.'}), 409

    try:
        _send_simple_command(port, 'REBOOT', wait_after=0.1)
    except FileNotFoundError:
        return jsonify({'message': f'Port {port} not found.'}), 404
    except SerialException as exc:
        return jsonify({'message': f'Unable to reboot {port}: {exc}'}), 500
    except Exception as exc:  # noqa: BLE001
        _logger.exception('Unexpected error while rebooting %s', port)
        return jsonify({'message': f'Unexpected error while rebooting {port}: {exc}'}), 500

    return jsonify({'message': 'Reboot command sent.', 'port': port})


# Terminal endpoints removed - using serial monitoring instead


@recorders_bp.route('/terminal/poll', methods=['GET'])
@require_admin
def poll_terminal():
    port = request.args.get('port')
    cursor = request.args.get('cursor', type=int)
    if port is None:
        return jsonify({'message': 'Port is required.'}), 400
    if cursor is None or cursor < 0:
        cursor = 0

    _cleanup_terminal_sessions()

    with _terminal_sessions_lock:
        session = _terminal_sessions.get(port)

    if not session or not session._active:
        return jsonify({
            'messages': [],
            'cursor': cursor,
            'connected': False,
            'truncated': False
        }), 410

    # Check if serial port is still open
    try:
        if not session.serial or not session.serial.is_open:
            _logger.warning("Serial port %s is closed during poll, marking session inactive", port)
            session._active = False
            return jsonify({
                'messages': [],
                'cursor': cursor,
                'connected': False,
                'truncated': False
            }), 410
    except (AttributeError, SerialException):
        _logger.warning("Error checking serial port status for %s, marking session inactive", port)
        session._active = False
        return jsonify({
            'messages': [],
            'cursor': cursor,
            'connected': False,
            'truncated': False
        }), 410

    messages, latest_cursor, truncated = session.read_since(cursor)
    formatted = [{'seq': seq, 'text': chunk} for seq, chunk in messages]
    return jsonify({
        'messages': formatted,
        'cursor': latest_cursor,
        'connected': True,
        'truncated': truncated
    })


@recorders_bp.route('/terminal/send', methods=['POST'])
@require_admin
def send_terminal():
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    data = payload.get('data', '')
    append_newline = bool(payload.get('append_newline', False))

    if not port:
        return jsonify({'message': 'Port is required.'}), 400

    _cleanup_terminal_sessions()

    with _terminal_sessions_lock:
        session = _terminal_sessions.get(port)

    if not session or not session._active:
        return jsonify({'message': f'No active terminal session for {port}.'}), 409

    try:
        payload_to_send = data + ('\n' if append_newline else '')
        session.write(payload_to_send)
    except SerialException as exc:
        _logger.error("Failed to write to recorder terminal %s: %s", port, exc)
        return jsonify({'message': f'Failed to send to {port}: {exc}'}), 500

    return jsonify({'message': 'sent'})


# Terminal endpoints removed - using serial monitoring instead


@recorders_bp.route('/terminal/reboot', methods=['POST'])
@require_admin
def reboot_from_terminal():
    """Reboot ESP32 device using DTR/RTS hardware signals (similar to ESPhome)."""
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    if not port:
        return jsonify({'message': 'Port is required.'}), 400

    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403

    _cleanup_terminal_sessions()

    with _terminal_sessions_lock:
        session = _terminal_sessions.get(port)

    if not session or not session._active:
        return jsonify({'message': f'No active terminal session for {port}.'}), 409

    try:
        # Use DTR/RTS hardware signals to reboot (similar to ESPhome)
        # This method works even when terminal is connected
        serial_conn = session.serial
        if not serial_conn or not serial_conn.is_open:
            return jsonify({'message': f'Serial connection for {port} is not open.'}), 409

        _logger.info("Rebooting ESP32 on %s using DTR/RTS hardware signals", port)
        
        # Set DTR and RTS to low (False) to enter boot mode
        serial_conn.dtr = False
        serial_conn.rts = False
        time.sleep(0.1)
        
        # Set DTR and RTS to high (True) to reset
        serial_conn.dtr = True
        serial_conn.rts = True
        time.sleep(0.1)
        
        # Set DTR and RTS back to low to complete reset
        serial_conn.dtr = False
        serial_conn.rts = False
        
        _logger.info("Hardware reboot signal sent to %s", port)
    except SerialException as exc:
        _logger.error("Failed to reboot %s via hardware signals: %s", port, exc)
        return jsonify({'message': f'Unable to reboot {port}: {exc}'}), 500
    except Exception as exc:  # noqa: BLE001
        _logger.exception('Unexpected error while rebooting %s', port)
        return jsonify({'message': f'Unexpected error while rebooting {port}: {exc}'}), 500

    return jsonify({'message': 'Reboot signal sent via hardware (DTR/RTS).', 'port': port})


# Serial Port Listing (All Devices)
@recorders_bp.route('/serial-ports', methods=['GET'])
@require_admin
def list_serial_ports():
    """Return all available serial ports, not just ESP32 devices."""
    ports = []
    for port in serial.tools.list_ports.comports():
        port_entry = {
            'port': port.device,
            'description': port.description,
            'manufacturer': port.manufacturer,
            'product': port.product,
            'serial_number': port.serial_number,
            'vid': f"0x{port.vid:04X}" if port.vid is not None else None,
            'pid': f"0x{port.pid:04X}" if port.pid is not None else None,
            'hwid': port.hwid
        }
        ports.append(port_entry)
    return jsonify({'ports': ports, 'count': len(ports)})


# Firmware Management Functions
def _ensure_firmware_storage():
    """Ensure firmware storage directory exists."""
    FIRMWARE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _get_firmware_json_path():
    """Get the path to the firmware.json file."""
    return FIRMWARE_STORAGE_DIR / 'firmware.json'


def _load_firmware_metadata():
    """Load firmware metadata from JSON file."""
    _ensure_firmware_storage()
    json_path = _get_firmware_json_path()
    _logger.info("Loading firmware metadata from: %s", json_path)
    _logger.info("Firmware storage directory: %s", FIRMWARE_STORAGE_DIR)
    _logger.info("JSON file exists: %s", json_path.exists())
    try:
        if json_path.exists():
            with open(json_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content or not content.strip():
                    _logger.warning("Firmware JSON file is empty: %s", json_path)
                    return {}
                metadata = json.loads(content)
                if not isinstance(metadata, dict):
                    _logger.error("Firmware JSON file does not contain a dictionary: %s", json_path)
                    return {}
                _logger.info("Loaded firmware metadata: %d entries - %s", len(metadata), list(metadata.keys()))
                return metadata if metadata else {}
        else:
            _logger.warning("Firmware JSON file not found at: %s", json_path)
            _logger.info("Firmware directory(%s) exists: %s", FIRMWARE_STORAGE_DIR, FIRMWARE_STORAGE_DIR.exists())
            if FIRMWARE_STORAGE_DIR.exists():
                try:
                    _logger.info("Contents of firmware directory: %s", [p.name for p in FIRMWARE_STORAGE_DIR.iterdir()])
                except Exception as list_exc:
                    _logger.warning("Could not list firmware directory contents: %s", list_exc)
        
        # Migration: If JSON doesn't exist but database has data, migrate it (one-time migration)
        # Only do this if JSON file truly doesn't exist
        if not json_path.exists():
            try:
                db_metadata = _settings_manager.get_firmware_metadata()
                if db_metadata:
                    _logger.info("Migrating firmware metadata from database to JSON file (one-time migration)")
                    _save_firmware_metadata(db_metadata)
                    return db_metadata
            except Exception as mig_exc:
                _logger.debug("Database migration check failed (this is okay): %s", mig_exc)
                pass  # Database methods may not work, that's okay
        
        return {}
    except json.JSONDecodeError as exc:
        _logger.error("Invalid JSON in firmware.json at %s: %s", json_path, exc, exc_info=True)
        return {}
    except Exception as exc:
        _logger.error("Failed to load firmware metadata from %s: %s", json_path, exc, exc_info=True)
        return {}


def _save_firmware_metadata(metadata):
    """Save firmware metadata to JSON file."""
    _ensure_firmware_storage()
    json_path = _get_firmware_json_path()
    _logger.info("Saving firmware metadata to: %s", json_path)
    _logger.info("Metadata to save: %d entries - %s", len(metadata), list(metadata.keys()))
    try:
        # Create directory if it doesn't exist
        json_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write to a temporary file first, then rename (atomic write)
        temp_path = json_path.with_name(json_path.name + ".tmp")
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        # Atomic rename
        if temp_path.exists():
            temp_path.replace(json_path)
        
        # Verify the file was written and can be read back
        if json_path.exists():
            file_size = json_path.stat().st_size
            _logger.info("Firmware metadata saved successfully to %s (size: %d bytes)", json_path, file_size)
            
            # Verify by reading it back
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    verify_metadata = json.load(f)
                if verify_metadata == metadata:
                    _logger.info("Verified: JSON file contains correct data")
                else:
                    _logger.warning("Warning: JSON file content doesn't match what was written")
            except Exception as verify_exc:
                _logger.error("Failed to verify written JSON file: %s", verify_exc)
        else:
            _logger.error("Firmware metadata file was not created at %s", json_path)
            raise IOError(f"Failed to create firmware.json file at {json_path}")
            
    except Exception as exc:
        _logger.error("Failed to save firmware metadata to %s: %s", json_path, exc, exc_info=True)
        # Clean up temp file if it exists
        (json_path.with_name(json_path.name + ".tmp")).unlink(missing_ok=True)
        raise


def _get_firmware_dir(firmware_id, metadata=None):
    """Get the directory path for a specific firmware using the folder field from metadata."""
    if metadata is None:
        metadata = _load_firmware_metadata()
    
    # Get folder from metadata, fallback to firmware_id if not found
    firmware_info = metadata.get(firmware_id, {})
    folder = firmware_info.get('folder', firmware_id)
    return FIRMWARE_STORAGE_DIR / folder


@recorders_bp.route('/firmware', methods=['GET'])
@require_admin
def list_firmwares():
    """List all stored firmware groups."""
    metadata = _load_firmware_metadata()
    _logger.debug("Firmware metadata loaded: %d entries", len(metadata))
    firmwares = []
    for firmware_id, info in metadata.items():
        firmware_dir = _get_firmware_dir(firmware_id, metadata)
        _logger.debug("Checking firmware %s in directory: %s", firmware_id, firmware_dir)
        files = {
            'bootloader.bin': (firmware_dir / 'bootloader.bin').exists(),
            'partitions.bin': (firmware_dir / 'partitions.bin').exists(),
            'firmware.bin': (firmware_dir / 'firmware.bin').exists()
        }
        firmwares.append({
            'id': firmware_id,
            'name': info.get('name', firmware_id),
            'description': info.get('description', ''),
            'folder': info.get('folder', firmware_id),
            'created_at': info.get('created_at', ''),
            'files': files
        })
    _logger.info("Returning %d firmware entries", len(firmwares))
    return jsonify({'firmwares': firmwares, 'count': len(firmwares)})


@recorders_bp.route('/firmware', methods=['POST'])
@require_admin
def upload_firmware():
    """Upload firmware files (bootloader.bin, partitions.bin, firmware.bin) and create a named firmware group."""
    if 'name' not in request.form:
        return jsonify({'message': 'Firmware name is required.'}), 400
    
    firmware_name = request.form.get('name', '').strip()
    if not firmware_name:
        return jsonify({'message': 'Firmware name cannot be empty.'}), 400
    
    # Check for required files
    required_files = ['bootloader.bin', 'partitions.bin', 'firmware.bin']
    uploaded_files = {}
    
    for file_key in required_files:
        if file_key not in request.files:
            return jsonify({'message': f'Missing required file: {file_key}'}), 400
        
        file = request.files[file_key]
        if file.filename == '':
            return jsonify({'message': f'No file selected for {file_key}'}), 400
        
        if not file.filename.endswith('.bin'):
            return jsonify({'message': f'File {file_key} must be a .bin file'}), 400
        
        uploaded_files[file_key] = file
    
    # Generate firmware ID
    firmware_id = secure_filename(firmware_name).replace(' ', '_').lower()
    timestamp = datetime.utcnow().isoformat() + 'Z'
    
    # Ensure unique ID
    metadata = _load_firmware_metadata()
    original_id = firmware_id
    counter = 1
    while firmware_id in metadata:
        firmware_id = f"{original_id}_{counter}"
        counter += 1
    
    # Create firmware directory (folder will be same as firmware_id for new uploads)
    firmware_dir = _get_firmware_dir(firmware_id)
    firmware_dir.mkdir(parents=True, exist_ok=True)
    
    # Save files
    try:
        for file_key, file in uploaded_files.items():
            file_path = firmware_dir / file_key
            file.save(file_path)
            _logger.info("Saved firmware file: %s", file_path)
        
        # Update metadata
        metadata[firmware_id] = {
            'name': firmware_name,
            'description': '',
            'folder': firmware_id,  # Folder is same as firmware_id for uploaded firmware
            'created_at': timestamp
        }
        
        # Save to JSON file
        try:
            _save_firmware_metadata(metadata)
            _logger.info("Firmware metadata saved to JSON file successfully")
        except Exception as save_exc:
            _logger.error("Failed to save firmware metadata to JSON: %s", save_exc, exc_info=True)
            # Don't fail the upload if JSON save fails, but log the error
            # The files are already saved, so we can continue
        
        return jsonify({
            'message': 'Firmware uploaded successfully.',
            'firmware_id': firmware_id,
            'name': firmware_name
        })
    except Exception as exc:
        _logger.exception("Error saving firmware files")
        # Clean up on error
        if firmware_dir.exists():
            shutil.rmtree(firmware_dir)
        return jsonify({'message': f'Error saving firmware: {exc}'}), 500


@recorders_bp.route('/firmware/<firmware_id>', methods=['PUT'])
@require_admin
def update_firmware(firmware_id):
    """Update firmware metadata (name and description)."""
    metadata = _load_firmware_metadata()
    
    if firmware_id not in metadata:
        return jsonify({'message': 'Firmware not found.'}), 404
    
    payload = request.get_json(silent=True) or {}
    name = payload.get('name', '').strip()
    description = payload.get('description', '').strip()
    
    if not name:
        return jsonify({'message': 'Firmware name is required.'}), 400
    
    try:
        metadata[firmware_id]['name'] = name
        metadata[firmware_id]['description'] = description
        _save_firmware_metadata(metadata)
        
        return jsonify({
            'message': 'Firmware updated successfully.',
            'firmware_id': firmware_id,
            'name': name,
            'description': description
        })
    except Exception as exc:
        _logger.exception("Error updating firmware")
        return jsonify({'message': f'Error updating firmware: {exc}'}), 500


@recorders_bp.route('/firmware/<firmware_id>', methods=['DELETE'])
@require_admin
def delete_firmware(firmware_id):
    """Delete a firmware group."""
    metadata = _load_firmware_metadata()
    
    if firmware_id not in metadata:
        return jsonify({'message': 'Firmware not found.'}), 404
    
    firmware_dir = _get_firmware_dir(firmware_id, metadata)
    
    try:
        # Delete directory
        if firmware_dir.exists():
            shutil.rmtree(firmware_dir)
        
        # Remove from metadata
        del metadata[firmware_id]
        _save_firmware_metadata(metadata)
        
        return jsonify({'message': 'Firmware deleted successfully.'})
    except Exception as exc:
        _logger.exception("Error deleting firmware")
        return jsonify({'message': f'Error deleting firmware: {exc}'}), 500


def _update_flash_progress(port, status, progress=0, message='', output='', error=''):
    """Update flash progress for a port."""
    with _flash_progress_lock:
        _flash_progress[port] = {
            'status': status,  # 'running', 'completed', 'failed'
            'progress': progress,  # 0-100
            'message': message,
            'output': output,
            'error': error,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }


def _get_flash_progress(port):
    """Get flash progress for a port."""
    with _flash_progress_lock:
        return _flash_progress.get(port)


def _clear_flash_progress(port):
    """Clear flash progress for a port."""
    with _flash_progress_lock:
        _flash_progress.pop(port, None)


def _parse_progress_from_line(line):
    """Parse progress percentage from esptool output line."""
    # Look for patterns like "Writing at 0x00010000... (100 %)"
    match = re.search(r'\((\d+)\s*%\)', line)
    if match:
        return int(match.group(1))
    # Look for patterns like "100%" or "50%"
    match = re.search(r'(\d+)%', line)
    if match:
        return int(match.group(1))
    return None


def _flash_firmware_thread(port, firmware_id, bootloader_path, partitions_path, firmware_path):
    """Flash firmware in a background thread with progress tracking."""
    try:
        _update_flash_progress(port, 'running', 0, 'Initializing flash operation...')
        
        # Ensure port is marked as flashing
        set_port_flashing(port, True)
        
        cmd = [
            sys.executable, '-m', 'esptool',
            '--chip', 'esp32',
            '--port', port,
            '--baud', '460800',
            '--before', 'default-reset',
            '--after', 'hard-reset',
            'write-flash',
            '-z',
            '--flash-mode', 'dio',
            '--flash-freq', '40m',
            '--flash-size', 'detect',
            '0x1000', bootloader_path,
            '0x8000', partitions_path,
            '0x10000', firmware_path
        ]
        
        _logger.info("Flashing firmware %s to port %s", firmware_id, port)
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        output_lines = []
        last_progress = 0
        
        while True:
            line = process.stdout.readline()
            if not line:
                break
            
            line = line.strip()
            if line:
                output_lines.append(line)
                _logger.debug("esptool output: %s", line)
                
                # Try to parse progress
                progress = _parse_progress_from_line(line)
                if progress is not None:
                    last_progress = progress
                    _update_flash_progress(port, 'running', progress, line, '\n'.join(output_lines))
                else:
                    # Update with current line but keep last progress
                    _update_flash_progress(port, 'running', last_progress, line, '\n'.join(output_lines))
        
        process.wait()
        output = '\n'.join(output_lines)
        
        if process.returncode == 0:
            _logger.info("Successfully flashed firmware %s to %s", firmware_id, port)
            _update_flash_progress(port, 'completed', 100, 'Firmware flashed successfully!', output)
        else:
            _logger.error("Failed to flash firmware %s to %s: %s", firmware_id, port, output)
            _update_flash_progress(port, 'failed', last_progress, 'Flash operation failed', output, output)
        
        # Unmark port as flashing
        set_port_flashing(port, False)
        
        # Restart monitoring if device is flagged for monitoring
        # Wait a moment after flash completes before restarting monitoring
        time.sleep(3.0)
        
        devices = _load_inventory()
        for device in devices:
            if device.get('port') == port and device.get('monitor', False):
                # Check if monitoring is already active (might have been restarted by port scanner)
                if not is_monitoring_active(port):
                    start_monitoring_for_device(port)
                    _logger.info("Restarted monitoring for port %s after flash operation", port)
                else:
                    _logger.info("Monitoring already active for port %s after flash operation", port)
                break
            
    except subprocess.TimeoutExpired:
        _logger.error("Timeout while flashing firmware %s to %s", firmware_id, port)
        _update_flash_progress(port, 'failed', 0, 'Flash operation timed out', '', 'Operation timed out after 2 minutes')
        set_port_flashing(port, False)
    except FileNotFoundError:
        _logger.error("esptool not found. Make sure esptool is installed.")
        _update_flash_progress(port, 'failed', 0, 'esptool not found', '', 'esptool not found. Please install esptool (pip install esptool).')
        set_port_flashing(port, False)
    except Exception as exc:
        _logger.exception("Unexpected error while flashing firmware %s to %s", firmware_id, port)
        _update_flash_progress(port, 'failed', 0, f'Unexpected error: {exc}', '', str(exc))
        set_port_flashing(port, False)


@recorders_bp.route('/flash', methods=['POST'])
@require_admin
def flash_firmware():
    """Start flashing firmware to a device using esptool (runs in background)."""
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    firmware_id = payload.get('firmware_id')
    
    if not port:
        return jsonify({'message': 'Port is required.'}), 400
    
    if not firmware_id:
        return jsonify({'message': 'Firmware ID is required.'}), 400
    
    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403
    
    # Check if flash is already running
    progress = _get_flash_progress(port)
    if progress and progress.get('status') == 'running':
        return jsonify({'message': f'Flash operation already in progress for {port}.'}), 409
    
    # Mark port as flashing to prevent monitoring restart
    set_port_flashing(port, True)
    
    try:
        # Stop monitoring for this port before flashing
        stop_monitoring_for_device(port)
        
        # Wait a moment to ensure the serial port is fully released
        # This is critical on Linux/Raspberry Pi where port release can take time
        time.sleep(2.0)
        
        # Verify firmware exists
        metadata = _load_firmware_metadata()
        if firmware_id not in metadata:
            set_port_flashing(port, False)
            return jsonify({'message': 'Firmware not found.'}), 404
        
        firmware_dir = _get_firmware_dir(firmware_id, metadata)
        bootloader_path = firmware_dir / 'bootloader.bin'
        partitions_path = firmware_dir / 'partitions.bin'
        firmware_path = firmware_dir / 'firmware.bin'
        
        # Verify all files exist
        for file_path in [bootloader_path, partitions_path, firmware_path]:
            if not file_path.exists():
                set_port_flashing(port, False)
                return jsonify({'message': f'Firmware file {file_path.name} not found.'}), 404
        
        # Start flash in background thread
        flash_thread = threading.Thread(
            target=_flash_firmware_thread,
            args=(port, firmware_id, bootloader_path, partitions_path, firmware_path),
            daemon=True,
            name=f"FlashFirmware-{port}"
        )
        flash_thread.start()
        
        return jsonify({
            'message': f'Flash operation started for {port}.',
            'port': port,
            'firmware_id': firmware_id
        })
    except Exception as exc:
        # Ensure port is unmarked on any error
        set_port_flashing(port, False)
        _logger.exception("Error starting flash operation for %s", port)
        return jsonify({'message': f'Error starting flash operation: {exc}'}), 500


@recorders_bp.route('/flash/progress', methods=['GET'])
@require_admin
def get_flash_progress():
    """Get flash progress for a port."""
    port = request.args.get('port')
    if not port:
        return jsonify({'message': 'Port is required.'}), 400
    
    progress = _get_flash_progress(port)
    if not progress:
        return jsonify({
            'status': 'not_started',
            'progress': 0,
            'message': 'No flash operation found for this port.'
        })
    
    return jsonify(progress)


@recorders_bp.route('/flash/clear', methods=['POST'])
@require_admin
def clear_flash_progress():
    """Clear flash progress for a port."""
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    if not port:
        return jsonify({'message': 'Port is required.'}), 400
    
    _clear_flash_progress(port)
    return jsonify({'message': 'Flash progress cleared.'})


@recorders_bp.route('/monitor', methods=['PUT'])
@require_admin
def update_monitor_flag():
    """Update the monitor flag for a device."""
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    monitor = payload.get('monitor')
    
    if not port:
        return jsonify({'message': 'Port is required.'}), 400
    
    if monitor is None:
        return jsonify({'message': 'Monitor flag is required.'}), 400
    
    # Convert to boolean if needed
    monitor = bool(monitor)
    
    with _inventory_lock:
        devices = _load_inventory()
        device_found = False
        
        for device in devices:
            if device.get('port') == port:
                device['monitor'] = monitor
                device_found = True
                break
        
        if not device_found:
            return jsonify({'message': f'Device with port {port} not found in inventory.'}), 404
        
        _save_inventory(devices)
    
    # Start or stop monitoring based on flag
    if monitor:
        start_monitoring_for_device(port)
    else:
        stop_monitoring_for_device(port)
    
    return jsonify({
        'message': 'Monitor flag updated successfully.',
        'port': port,
        'monitor': monitor
    })


@recorders_bp.route('/monitor/messages', methods=['GET'])
@require_admin
def get_monitor_messages():
    """Get messages from monitored devices."""
    port = request.args.get('port')  # Optional: filter by port
    limit = request.args.get('limit', type=int, default=100)
    
    messages = get_messages(port=port, limit=limit)
    return jsonify({
        'messages': messages,
        'count': len(messages)
    })


@recorders_bp.route('/logs', methods=['GET'])
@require_admin
def get_recorder_logs():
    """
    DEVICES tab: serial/com_port lines + API event lines for that device's MAC.

    Query parameters:
        port: Optional. COM port identifier (e.g., COM3). If omitted, returns logs for all ports.
        date: Optional. Date in YYYY-MM-DD. Defaults to today.
    """
    port_filter = request.args.get('port')
    date_str = request.args.get('date')

    try:
        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            target_date = datetime.now(timezone.utc).date()
            date_str = target_date.strftime('%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    year = target_date.strftime('%Y')
    month = target_date.strftime('%m')
    base_dir = DATA_ROOT / 'logs' / year / month

    logs_by_port = {}
    dedupe = {}

    def _dedupe_key(ts, msg):
        return (ts or '', (msg or '')[:600])

    def _add_line(port_name, ts, msg):
        if port_filter and port_name != port_filter:
            return
        k = _dedupe_key(ts, msg)
        d = dedupe.setdefault(port_name, set())
        if k in d:
            return
        d.add(k)
        logs_by_port.setdefault(port_name, []).append({'timestamp': ts or '', 'message': msg})

    def _parse_line(line):
        line = line.rstrip('\n')
        if not line:
            return None
        if line.startswith('[') and '] ' in line:
            ts_part, msg = line.split('] ', 1)
            ts_str = ts_part.strip('[]')
        else:
            ts_str = ''
            msg = line
        return ts_str, msg

        if base_dir.is_dir():
            for file_path in base_dir.glob(f"{date_str}_*.log"):
                port_name = file_path.stem.split("_", 1)[1]

                if port_filter and port_name != port_filter:
                    continue

                try:
                    with file_path.open("r", encoding="utf-8") as f:
                        for line in f:
                            if parsed := _parse_line(line):
                                _add_line(port_name, parsed[0], parsed[1])
                except Exception as exc:
                    _logger.error(
                        "Failed to read recorder log file %s: %s",
                        file_path,
                        exc,
                    )
    try:
        dbm = get_db_logging_manager()
        for row in dbm.get_logs('com_port', date=date_str, limit=25000):
            p = row.get('port')
            if not p:
                continue
            _add_line(p, row.get('timestamp') or '', row.get('message') or '')
    except Exception as exc:
        _logger.debug("Recorder logs: com_port DB merge failed: %s", exc)

    def _norm_mac_fragment(s):
        return re.sub(r'[^0-9A-Fa-f]', '', s or '').upper()

    mac_to_port = {}
    for com_port, mac in get_port_mac_map().items():
        nm = _norm_mac_fragment(mac)
        if len(nm) >= 6:
            mac_to_port[nm] = com_port

    mac_in_msg = re.compile(r'MAC[:\s]+([0-9A-Fa-f:]+)', re.I)
    try:
        dbm = get_db_logging_manager()
        for row in dbm.get_logs('event', date=date_str, limit=8000):
            msg = row.get('message') or ''
            m = mac_in_msg.search(msg)
            if not m:
                continue
            target = mac_to_port.get(_norm_mac_fragment(m.group(1)))
            if target:
                ts = row.get('timestamp') or ''
                _add_line(target, ts, f'[API/event] {msg}')
    except Exception as exc:
        _logger.debug("Recorder logs: event DB merge failed: %s", exc)

    for p in list(logs_by_port.keys()):
        logs_by_port[p].sort(key=lambda e: e.get('timestamp') or '')

    return jsonify({
        'logs': logs_by_port,
        'date': date_str,
    })


@recorders_bp.route('/monitor/status', methods=['GET'])
@require_admin
def get_monitor_status():
    """Get monitoring status for all devices or a specific port."""
    port = request.args.get('port')  # Optional
    
    devices = _load_inventory()
    status_list = []
    
    for device in devices:
        device_port = device.get('port')
        if port and device_port != port:
            continue
        
        status_list.append({
            'port': device_port,
            'monitor_flag': device.get('monitor', False),
            'monitoring_active': is_monitoring_active(device_port) if device_port else False
        })
    
    return jsonify({
        'devices': status_list,
        'count': len(status_list)
    })


@recorders_bp.route('/monitor/send', methods=['POST'])
@require_admin
def send_monitor_command():
    """Send a command to one or more monitored serial ports."""
    payload = request.get_json(silent=True) or {}
    command = payload.get('command', '').strip()
    ports = payload.get('ports', [])
    
    if not command:
        return jsonify({'message': 'Command is required.'}), 400
    
    if not ports or not isinstance(ports, list):
        return jsonify({'message': 'Ports list is required.'}), 400
    
    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403
    
    # Send command to all specified ports
    results = send_command_to_ports(ports, command)
    
    success_count = sum(1 for success in results.values() if success)
    
    return jsonify({
        'message': f'Command sent to {success_count}/{len(ports)} port(s).',
        'results': results,
        'success_count': success_count,
        'total_count': len(ports)
    })


@recorders_bp.route('/monitor/send-by-mac', methods=['POST'])
@require_admin
def send_monitor_command_by_mac():
    """Resolve a device MAC to its serial port and send one or more commands to it.

    Used by the channel settings modal so that saving audio settings pushes the
    matching CLI SET/SAVE commands to the specific recorder over the serial monitor.
    Body: { "mac": "AABBCC...", "commands": ["SET audio.codecgain 3", "SAVE"] }
    (a single "command" string is also accepted).
    """
    payload = request.get_json(silent=True) or {}
    mac = (payload.get('mac') or '').strip()
    commands = payload.get('commands')
    single = payload.get('command')
    if single and not commands:
        commands = [single]

    if not mac:
        return jsonify({'message': 'mac is required.'}), 400

    if not commands or not isinstance(commands, list):
        return jsonify({'message': 'commands list is required.'}), 400

    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403

    def _norm_mac(value):
        return re.sub(r'[^0-9A-Fa-f]', '', value or '').upper()

    target_mac = _norm_mac(mac)
    port = None
    for com_port, dev_mac in get_port_mac_map().items():
        if _norm_mac(dev_mac) == target_mac:
            port = com_port
            break

    if not port:
        return jsonify({
            'message': f'No connected/monitored recorder found for MAC {mac}.',
            'mac': mac
        }), 404

    results = []
    for raw_cmd in commands:
        cmd = (raw_cmd or '').strip()
        if not cmd:
            continue
        ok = bool(send_command_to_ports([port], cmd).get(port, False))
        results.append({'command': cmd, 'success': ok})
        # Small gap so the firmware can process each SET before the trailing SAVE.
        time.sleep(0.15)

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'message': f'Sent {success_count}/{len(results)} command(s) to {port}.',
        'mac': mac,
        'port': port,
        'results': results,
        'success_count': success_count,
        'total_count': len(results)
    })


@recorders_bp.route('/monitor/autoconfig', methods=['POST'])
@require_admin
def send_autoconfig():
    """Run the 7-step autoconfig sequence (set SSID, password, custom host/port, save, reboot) on selected ports."""
    payload = request.get_json(silent=True) or {}
    ports = payload.get('ports', [])
    # host_ssid = (payload.get('host_ssid') or '').strip()
    # host_password = payload.get('host_password') or ''
    # host_ip = (payload.get('host_ip') or '').strip()
    # configured_host_port = _settings_manager.get_setting('host_port', '4000')
    # host_port_raw = payload.get('host_port') or configured_host_port

    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403

    if not ports or not isinstance(ports, list):
        return jsonify({'message': 'Ports list is required.'}), 400

    # try:
    #     host_port = int(host_port_raw)
    #     expected_host_port = int(configured_host_port)
    # except (TypeError, ValueError):
    #     host_port = 0
    #     expected_host_port = 0
    # if host_port != expected_host_port:
    #     return jsonify({'message': 'The host port cannot be changed.'}), 400
    # if not host_ssid or not host_password or not host_ip or host_port < 1 or host_port > 65535:
    #     return jsonify({
    #         'message': 'host_ssid, host_password, host_ip, and host_port (1-65535) are required.'
    #     }), 400
    settings = _settings_manager.get_all_settings() or {}
    host_ssid = settings.get("ssid")
    host_password = settings.get("host_password")
    host_ip = settings.get("host_ip")
    host_port = settings.get("host_port", "4000")
    if not all((host_ssid, host_password, host_ip, host_port)):
        _logger.warning("Skipping USB device autoconfiguration: host settings are incomplete")
        return {}


    command_interval = float(payload.get('command_interval', 2.5))
    if command_interval < 0 or command_interval > 10:
        command_interval = 2.5

    results = {}
    for port in ports:
        results[port] = run_autoconfig_sequence(
            port,
            host_ssid,
            host_password,
            host_ip,
            host_port,
            command_interval=command_interval,
        )

    success_count = sum(1 for r in results.values() if r.get('success'))
    return jsonify({
        'message': f'Autoconfig completed for {success_count}/{len(ports)} port(s).',
        'results': results,
        'success_count': success_count,
        'total_count': len(ports)
    })


@recorders_bp.route('/monitor/reset', methods=['POST'])
@require_admin
def reset_monitor_connection():
    """Reset monitoring connection for a device."""
    payload = request.get_json(silent=True) or {}
    port = payload.get('port')
    
    if not port:
        return jsonify({'message': 'Port is required.'}), 400
    
    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403
    
    try:
        success = reconnect_device(port)
        
        if success:
            return jsonify({
                'message': 'Monitoring connection reset successfully.',
                'port': port
            })
        else:
            return jsonify({
                'message': 'Failed to reset monitoring connection.',
                'port': port
            }), 500
    except Exception as exc:
        _logger.exception("Error resetting monitor connection for %s", port)
        return jsonify({'message': f'Error: {exc}'}), 500


@recorders_bp.route('/reboot-counts', methods=['GET'])
@require_admin
def get_reboot_counts():
    """Get reboot counts for all recorders or a specific port."""
    port = request.args.get('port')  # Optional
    
    devices = _load_inventory()
    reboot_counts = []
    
    for device in devices:
        device_port = device.get('port')
        if port and device_port != port:
            continue
        
        reboot_counts.append({
            'port': device_port,
            'reboot_count': device.get('reboot_count', 0),
            'last_reboot': device.get('last_reboot')
        })
    
    return jsonify({
        'reboot_counts': reboot_counts,
        'count': len(reboot_counts)
    })


@recorders_bp.route('/reboot-counts/sync', methods=['POST'])
@require_admin
def sync_reboot_counts():
    """Sync reboot counts from log files to inventory."""
    try:
        updated = sync_reboot_counts_from_logs()
        
        return jsonify({
            'message': 'Reboot counts synced from log files.',
            'updated': updated
        })
    except Exception as exc:
        _logger.exception("Error syncing reboot counts")
        return jsonify({'message': f'Error: {exc}'}), 500


@recorders_bp.route('/reboot-history', methods=['GET'])
@require_admin
def get_reboot_history():
    """Get reboot history for a device by MAC address or port."""
    try:        
        mac_address = request.args.get('mac')
        port = request.args.get('port')
        limit = int(request.args.get('limit', 5))  # Default to last 5 reboots
        
        if not mac_address and not port:
            return jsonify({'error': 'Either mac or port parameter is required'}), 400
        
        history = load_reboot_history()
        
        # If MAC address provided, get history by MAC
        if mac_address:
            mac_address = mac_address.upper()
            if mac_address in history:
                reboots = history[mac_address][-limit:]  # Get last N reboots
                return jsonify({
                    'mac_address': mac_address,
                    'reboots': reboots,
                    'count': len(reboots),
                    'total_count': len(history[mac_address])
                }), 200
            else:
                return jsonify({
                    'mac_address': mac_address,
                    'reboots': [],
                    'count': 0,
                    'total_count': 0
                }), 200
        
        # If port provided, find MAC address from inventory and get history
        if port:
            devices = _load_inventory()
            device = next((d for d in devices if d.get('port') == port), None)
            
            if not device:
                return jsonify({'error': f'Device not found for port {port}'}), 404
            
            # Try to find MAC address from device status or recent messages
            # For now, we'll need to search history by port
            # Find all MAC addresses that have reboots on this port
            matching_reboots = []
            for mac, reboots in history.items():
                for reboot in reboots:
                    if reboot.get('port') == port:
                        matching_reboots.append({
                            'mac_address': mac,
                            'timestamp': reboot.get('timestamp'),
                            'port': reboot.get('port')
                        })
            
            # Sort by timestamp (newest first) and limit
            matching_reboots.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            matching_reboots = matching_reboots[:limit]
            
            return jsonify({
                'port': port,
                'reboots': matching_reboots,
                'count': len(matching_reboots)
            }), 200
        
    except Exception as exc:
        _logger.exception("Error getting reboot history")
        return jsonify({'error': f'Error: {exc}'}), 500


@recorders_bp.route('/serial-data', methods=['GET'])
@require_admin
def get_serial_data():
    """Get parsed serial data (short, health, config, error logs) for a device."""
    try:
        port = request.args.get('port')
        if not port:
            return jsonify({'error': 'Port parameter is required'}), 400
        
        data = get_all_parsed_data(port)
        return jsonify({
            'port': port,
            'short': data.get('short'),
            'health': data.get('health'),
            'config': data.get('config'),
            'error_logs': data.get('error_logs', [])
        }), 200
    except Exception as exc:
        _logger.exception("Error retrieving serial data")
        return jsonify({'error': f'Error: {exc}'}), 500


@recorders_bp.route('/serial-data/error-logs', methods=['GET'])
@require_admin
def get_serial_error_logs():
    """Get error/warning/fatal logs for a device."""
    try:
        port = request.args.get('port')
        if not port:
            return jsonify({'error': 'Port parameter is required'}), 400
        
        logs = get_error_logs(port)
        return jsonify({
            'port': port,
            'logs': logs,
            'count': len(logs)
        }), 200
    except Exception as exc:
        _logger.exception("Error retrieving error logs")
        return jsonify({'error': f'Error: {exc}'}), 500


def reset_monitoring():
    """Disconnect and reconnect all monitored devices."""
    if not _settings_allow_discovery():
        return jsonify({'message': 'Recorder discovery is disabled.'}), 403
    
    results = reconnect_all_monitored_devices()
    success_count = sum(1 for success in results.values() if success)
    
    return jsonify({
        'message': f'Reset {success_count}/{len(results)} monitored device(s).',
        'results': results,
        'success_count': success_count,
        'total_count': len(results)
    })
