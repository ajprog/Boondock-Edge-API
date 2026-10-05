"""
Serial port monitoring service for Boondock Edge recorders.
Monitors serial ports for devices flagged for monitoring and logs messages.

Automated line format: see Device-Docs/DEVICE_SERIAL.md (ty: log, ready, config, short, health, event, info, error).
"""
import os
import json
import logging
import queue
import threading
import time
from config import Config
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from serial import Serial, SerialException
from serial.serialutil import SerialTimeoutException
import serial.tools.list_ports
from ..services.settings_manager import get_settings_manager

_logger = logging.getLogger(__name__)

_settings_manager = get_settings_manager()

# Use the shared logs directory structure: logs/YYYY/MM/....
# Get logs directory from centralized config

_LOGS_DIR = Config.get_logs_dir()

# Global state for monitoring
_monitoring_threads = {}
_monitoring_lock = threading.Lock()
_monitoring_messages = {}  # port -> deque of messages
# port -> {"event": Event(), "result": None}; fulfilled when device sends JSON with status=="ok"
_response_waiters = {}
_max_messages_per_port = 1000
_monitoring_active = {}  # port -> bool
_monitoring_connections = {}  # port -> Serial connection object
_port_scanning_active = False
_port_scanning_thread = None
_port_scan_interval = 5.0  # Scan every 5 seconds
_last_known_mac = {}  # port -> MAC address (to track MAC per port)
_last_channel_check_time = {}  # MAC -> timestamp of last channel check (to avoid spamming)
_flashing_ports = set()  # Set of ports currently being flashed (to prevent monitoring restart)
_flashing_lock = threading.Lock()  # Lock for flashing_ports set
_pending_config_exports = {}  # port -> MAC awaiting one initial EXPORT document
_synchronized_config_mac = {}  # port -> MAC synchronized for this connection
# Last HEALTH CLI poll per port (DEVICE_SERIAL.md: rc/uc/pq only in ty=health)
_last_health_poll = {}  # port -> unix time
_HEALTH_POLL_INTERVAL_SEC = 50.0
REBOOT_HISTORY_JSON_PATH = Config.get_db_dir() / 'reboot_history.json'
_reboot_history_queue = queue.Queue(maxsize=100)  # Queue for non-blocking reboot history writes
_reboot_history_thread = None  # Background thread for processing reboot history
_reboot_history_stop = threading.Event()  # Set to request clean shutdown of the queue processor

# Parsed message data storage
_parsed_short_status = {}  # port -> latest short status message
# port -> { 'system': {data, timestamp}, 'recording': {...}, 'legacy': {...} } (DEVICE_SERIAL.md health is two objects)
_parsed_health_data = {}
_parsed_config_data = {}  # port -> merged config + wifi/audio/other/recorder/general buckets
_error_logs = {}  # port -> deque(maxlen=10) of error/warning/fatal messages


def _get_log_file_path(port):
    """Get the log file path for a device on a specific day.

    New format (aligned with other logs):
        logs/YYYY/MM/YYYY-MM-DD_<PORT>.log
    """
    today = datetime.now(timezone.utc).date()

    year = today.strftime('%Y')
    month = today.strftime('%m')
    date_str = today.strftime('%Y-%m-%d')

    # Sanitize port name for filename
    safe_port = ''.join(c if c.isalnum() else '_' for c in port)

    log_path = _LOGS_DIR / year / month / f"{date_str}_{safe_port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    return log_path


def _is_valid_message(message):
    """
    Check if a message is valid (not junk/corrupted data).
    Returns True if the message should be logged, False if it's junk.
    """
    if not message or not isinstance(message, str):
        return False
    
    # Strip whitespace
    msg = message.strip()
    if not msg:
        return False
    
    # Very short messages (less than 3 chars) that aren't known patterns are likely junk
    if len(msg) < 3:
        return False
    
    # Check if it looks like valid JSON
    if msg.startswith('{') and msg.endswith('}'):
        try:
            json.loads(msg)
            return True  # Valid JSON
        except json.JSONDecodeError:
            # Invalid JSON - check if it's severely corrupted
            # Count quote characters - corrupted JSON often has mismatched quotes
            quote_count = msg.count('"')
            colon_count = msg.count(':')
            # If it has "ty" key pattern, it's likely a corrupted JSON message
            if '"ty"' in msg or '"ty":' in msg:
                return True  # Partially valid JSON structure, keep it for debugging
            # If it has very few proper JSON elements, it's junk
            if quote_count < 4 or colon_count < 1:
                return False
            # Check for obvious corruption patterns
            if '""' in msg and msg.count('""') > 2:
                return False  # Too many empty strings, likely corrupted
            return True  # Keep it but it may be corrupted
    
    # Known valid message prefixes (ESP32 boot/system messages)
    valid_prefixes = (
        'ets ',           # ESP32 boot
        'rst:',           # Reset reason
        'configsip:',     # SPI config
        'clk_drv:',       # Clock driver
        'mode:',          # Boot mode
        'load:',          # Bootloader load
        'entry ',         # Entry point
        'E (',            # ESP-IDF error
        'W (',            # ESP-IDF warning  
        'I (',            # ESP-IDF info
        'D (',            # ESP-IDF debug
        'V (',            # ESP-IDF verbose
        'Backtrace:',     # Stack trace
        'ELF file',       # ELF info
        'Rebooting',      # Reboot message
        'assert ',        # Assert failure
        '[SENT]',         # Our sent command marker
        '[RETRY]',        # Our retry marker
        '[INFO]',         # Our info marker
        '[ERROR]',        # Our error marker
    )
    
    if any(msg.startswith(prefix) for prefix in valid_prefixes):
        return True
    
    # Messages starting with [ are usually our markers or timestamps
    if msg.startswith('['):
        return True
    
    # Check for printable character ratio
    # Junk data often has many non-printable or unusual characters
    printable_count = sum(1 for c in msg if c.isprintable() or c in '\t\n\r')
    if len(msg) > 0 and printable_count / len(msg) < 0.8:
        return False  # Less than 80% printable, likely junk
    
    # Check for reasonable character distribution
    # Corrupted data often has repeated patterns or unusual char sequences
    alpha_count = sum(1 for c in msg if c.isalpha())
    digit_count = sum(1 for c in msg if c.isdigit())
    space_count = msg.count(' ')
    
    # If it's mostly alphabetic or has reasonable structure, keep it
    if alpha_count > len(msg) * 0.3 or (alpha_count + digit_count + space_count) > len(msg) * 0.5:
        return True
    
    # If it contains common ESP32/firmware keywords, keep it
    keywords = ['wifi', 'error', 'warning', 'info', 'init', 'boot', 'flash', 'spi', 'uart']
    msg_lower = msg.lower()
    if any(kw in msg_lower for kw in keywords):
        return True
    
    # Default: if it's short and doesn't match patterns, it's probably junk
    if len(msg) < 20:
        return False
    
    return True


def _log_message(port, message):
    """Log a message to the database (com_port log type)."""
    # Skip junk/corrupted messages
    if not _is_valid_message(message):
        _logger.debug("Skipping junk message from %s: %s", port, message[:50] if message else '')
        return
    
    try:
        from ..services.db_logging_manager import get_db_logging_manager
        db_manager = get_db_logging_manager()
        
        # Determine log level from message content
        message_upper = message.upper()
        if any(keyword in message_upper for keyword in ['ERROR', 'E (', '[ERROR]', 'FAILED', 'FATAL']):
            level = 'ERROR'
        elif any(keyword in message_upper for keyword in ['WARNING', 'W (', '[WARN]', 'WARN']):
            level = 'WARNING'
        elif any(keyword in message_upper for keyword in ['INFO', 'I (', '[INFO]']):
            level = 'INFO'
        else:
            level = 'INFO'  # Default to INFO
        
        # Log to database with port information
        db_manager.log(
            log_type='com_port',
            level=level,
            message=message,
            logger_name='recorder_monitor',
            port=port
        )
        # Mirror to daily port file so /api/recorders/logs (DEVICES tab) stays in sync
        try:
            log_path = _get_log_file_path(port)
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception as file_exc:
            _logger.debug("Could not mirror com_port to file for %s: %s", port, file_exc)
    except Exception as exc:
        # Fallback to file logging if database logging fails
        try:
            log_path = _get_log_file_path(port)
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception as file_exc:
            _logger.error("Failed to write to log (database and file) for %s: %s / %s", port, exc, file_exc)


def _extract_mac_from_message(message):
    """Extract MAC address from a message if present."""
    try:
        if isinstance(message, str):
            start = message.find('{')
            end = message.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = message[start:end + 1]
                json_data = json.loads(json_str)
                if isinstance(json_data, dict) and json_data.get('mc'):
                    return json_data.get('mc').upper()
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    return None


def _ensure_channel_for_mac(mac_address):
    """
    Ensure a channel exists for the given MAC address.
    If no channel exists, create one automatically.
    Includes retry logic for database readiness issues.
    
    Args:
        mac_address (str): The MAC address to check/create channel for
    """
    max_retries = 3
    retry_delay = 1.0
    
    for attempt in range(max_retries):
        try:
            from ..routes.route_utils import get_channel_id_from_mac, create_channel_for_mac
            
            # Check if channel already exists (refresh to get latest from database)
            channel_id = get_channel_id_from_mac(mac_address, refresh=True)
            
            if channel_id is None:
                # No channel exists, create one
                if attempt == 0:
                    _logger.info(f"No channel found for MAC {mac_address} from serial port, creating new channel")
                else:
                    _logger.info(f"Retrying channel creation for MAC {mac_address} (attempt {attempt + 1}/{max_retries})")
                
                try:
                    new_channel_id = create_channel_for_mac(mac_address)
                    _logger.info(f"create_channel_for_mac returned: {new_channel_id} for MAC {mac_address}")
                    if new_channel_id:
                        _logger.info(f"Auto-created channel {new_channel_id} for MAC {mac_address} from serial port")
                        return  # Success, exit retry loop
                    else:
                        if attempt < max_retries - 1:
                            _logger.warning(f"Failed to create channel for MAC {mac_address} (attempt {attempt + 1}/{max_retries}), retrying...")
                            time.sleep(retry_delay)
                            continue
                        else:
                            _logger.warning(f"Failed to create channel for MAC {mac_address} after {max_retries} attempts - create_channel_for_mac returned None or invalid ID")
                except Exception as create_exc:
                    if attempt < max_retries - 1:
                        _logger.warning(f"Exception in create_channel_for_mac for MAC {mac_address} (attempt {attempt + 1}/{max_retries}): {create_exc}, retrying...")
                        time.sleep(retry_delay)
                        continue
                    else:
                        _logger.error(f"Exception in create_channel_for_mac for MAC {mac_address} after {max_retries} attempts: {create_exc}", exc_info=True)
            else:
                # Channel exists - no need to log anything, just ensure it's working
                _logger.debug(f"Channel {channel_id} already exists for MAC {mac_address}")
                return  # Success, exit retry loop
        except Exception as exc:
            if attempt < max_retries - 1:
                _logger.warning(f"Error ensuring channel for MAC {mac_address} (attempt {attempt + 1}/{max_retries}): {exc}, retrying...")
                time.sleep(retry_delay)
                continue
            else:
                _logger.error(f"Error ensuring channel for MAC {mac_address} after {max_retries} attempts: {exc}", exc_info=True)


def _load_reboot_history():
    """Load reboot history from disk."""
    try:
        if REBOOT_HISTORY_JSON_PATH.exists():
            with open(REBOOT_HISTORY_JSON_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as exc:
        _logger.error("Failed to load reboot history: %s", exc)
    return {}


def load_reboot_history():
    """Public function to load reboot history from disk."""
    return _load_reboot_history()


def _save_reboot_history(history):
    """Save reboot history to disk."""
    try:
        REBOOT_HISTORY_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REBOOT_HISTORY_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2)
    except Exception as exc:
        _logger.error("Failed to save reboot history: %s", exc)


def _add_reboot_to_history(mac_address, port, timestamp):
    """Add a reboot event to the history queue (non-blocking)."""
    try:
        if not mac_address:
            _logger.warning("Cannot add reboot to history: no MAC address for port %s", port)
            return
        
        mac_address = mac_address.upper()
        # Queue the operation (non-blocking, will drop if queue is full)
        try:
            _reboot_history_queue.put_nowait(('add', mac_address, port, timestamp))
        except queue.Full:
            _logger.warning("Reboot history queue full, dropping event for MAC=%s", mac_address)
    except Exception as exc:
        _logger.error("Failed to queue reboot history: %s", exc)


def _process_reboot_history_queue():
    """Background thread to process reboot history writes (non-blocking)."""
    while not _reboot_history_stop.is_set():
        try:
            action, mac_address, port, timestamp = _reboot_history_queue.get(timeout=1.0)
            if action == 'add':
                try:
                    history = _load_reboot_history()
                    
                    # Initialize MAC entry if it doesn't exist
                    if mac_address not in history:
                        history[mac_address] = []
                    
                    # Add reboot event (limit to last 100 reboots per MAC)
                    reboot_event = {
                        'timestamp': timestamp.isoformat() + 'Z',
                        'port': port
                    }
                    history[mac_address].append(reboot_event)
                    
                    # Keep only last 100 reboots per MAC address
                    if len(history[mac_address]) > 100:
                        history[mac_address] = history[mac_address][-100:]
                    
                    _save_reboot_history(history)
                    _logger.info("Added reboot to history: MAC=%s, port=%s, time=%s", mac_address, port, timestamp.isoformat())
                except Exception as exc:
                    _logger.error("Failed to process reboot history entry: %s", exc)
        except queue.Empty:
            continue
        except Exception as exc:
            _logger.error("Error in reboot history queue processor: %s", exc)
            time.sleep(1.0)  # Brief pause on error


def _detect_reboot(message):
    """Detect boot/reboot: DEVICE_SERIAL log line with reset reason (rr), or legacy info INIT."""
    try:
        if isinstance(message, str):
            start = message.find('{')
            end = message.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = message[start:end + 1]
                try:
                    json_data = json.loads(json_str)
                    if isinstance(json_data, dict):
                        if json_data.get('ty') == 'log' and json_data.get('rr'):
                            return True
                        if json_data.get('ty') == 'info' and json_data.get('ms') == 'INIT':
                            return True
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return False


def _health_message_bucket(json_data):
    """Classify ty=health line as system vs recording (DEVICE_SERIAL.md). Returns 'system'|'recording'|'legacy'."""
    if not isinstance(json_data, dict) or json_data.get('ty') != 'health':
        return None
    st = json_data.get('st')
    if isinstance(st, list):
        return 'recording'
    if any(
        k in json_data
        for k in ('rc', 'uc', 'pq', 'td', 'am', 'ax', 'aa', 'yr', 'yf', 'ys', 'ym', 'yd')
    ):
        return 'recording'
    if json_data.get('tr') is not None or json_data.get('tu') is not None:
        return 'recording'
    if json_data.get('ht') and json_data.get('hf'):
        return 'system'
    if json_data.get('wi') is not None and isinstance(st, str):
        return 'system'
    return 'legacy'


def _rebuild_merged_config(port, timestamp_iso):
    """Merge all config buckets into port['data']."""
    entry = _parsed_config_data.get(port)
    if not entry:
        return
    merged = {}
    for key in ('wifi', 'audio', 'other', 'recorder', 'general'):
        bucket = entry.get(key)
        if bucket and isinstance(bucket.get('data'), dict):
            merged.update(bucket['data'])
    entry['data'] = merged
    entry['timestamp'] = timestamp_iso


def _increment_reboot_count(port, mac_address=None, timestamp=None):
    """Increment reboot count for a port in the inventory and add to reboot history."""
    try:
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        devices = _load_inventory()
        port_found = False
        device_count = 0
        
        for device in devices:
            if device.get('port') == port:
                # Increment reboot count
                current_count = device.get('reboot_count', 0)
                device['reboot_count'] = current_count + 1
                device['last_reboot'] = timestamp.isoformat() + 'Z'
                device_count = device['reboot_count']
                port_found = True
                break
        
        if not port_found:
            # Port not in inventory, add it
            new_device = {
                'port': port,
                'reboot_count': 1,
                'last_reboot': timestamp.isoformat() + 'Z',
                'monitor': True
            }
            devices.append(new_device)
            device_count = 1
        
        _save_inventory(devices)
        _logger.info("Reboot detected for port %s. Total reboots: %d", port, device_count)
        
        # Add to reboot history if MAC address is available
        if mac_address:
            _add_reboot_to_history(mac_address, port, timestamp)
        else:
            _logger.warning("Reboot detected for port %s but no MAC address available", port)
    except Exception as exc:
        _logger.error("Failed to update reboot count for port %s: %s", port, exc)


def _parse_json_message(message):
    """Extract and parse JSON from a message string."""
    try:
        if isinstance(message, str):
            start = message.find('{')
            end = message.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = message[start:end + 1]
                return json.loads(json_str)
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    return None


def _process_parsed_message(port, json_data, timestamp):
    """Process parsed JSON (DEVICE_SERIAL.md + legacy cm/wifi/audio/other config)."""
    if not isinstance(json_data, dict):
        return

    msg_type = json_data.get('ty')
    ts_iso = timestamp.isoformat()

    with _monitoring_lock:
        if port not in _error_logs:
            _error_logs[port] = deque(maxlen=10)

        if msg_type == 'short':
            _parsed_short_status[port] = {'data': json_data, 'timestamp': ts_iso}

        elif msg_type == 'health':
            bucket = _health_message_bucket(json_data)
            if port not in _parsed_health_data:
                _parsed_health_data[port] = {}
            h = _parsed_health_data[port]
            if bucket == 'system':
                h['system'] = {'data': json_data, 'timestamp': ts_iso}
            elif bucket == 'recording':
                h['recording'] = {'data': json_data, 'timestamp': ts_iso}
            else:
                h['legacy'] = {'data': json_data, 'timestamp': ts_iso}
            h['timestamp'] = ts_iso

        elif msg_type == 'config':
            if port not in _parsed_config_data:
                _parsed_config_data[port] = {
                    'data': {},
                    'timestamp': ts_iso,
                    'wifi': None,
                    'audio': None,
                    'other': None,
                    'recorder': None,
                    'general': None,
                }
            entry = _parsed_config_data[port]
            for k in ('recorder', 'general'):
                if k not in entry:
                    entry[k] = None

            config_subtype = json_data.get('cm')
            if config_subtype in ('wifi', 'audio', 'other'):
                entry[config_subtype] = {'data': json_data, 'timestamp': ts_iso}
                _rebuild_merged_config(port, ts_iso)
            else:
                has_rec = any(
                    json_data.get(k) is not None
                    for k in ('ath', 'mrm', 'xrm', 'stm', 'prm', 'cg', 'is', 'rsc')
                )
                has_gen = any(
                    json_data.get(k) is not None
                    for k in ('fw', 'ss', 'ho', 'po', 'sie', 'rte', 'usc', 'oh', 'wtp')
                )
                if has_rec:
                    entry['recorder'] = {'data': json_data, 'timestamp': ts_iso}
                if has_gen:
                    entry['general'] = {'data': json_data, 'timestamp': ts_iso}
                if not has_rec and not has_gen:
                    entry['other'] = {'data': json_data, 'timestamp': ts_iso}
                _rebuild_merged_config(port, ts_iso)

        elif msg_type == 'log':
            lv = (json_data.get('lv') or '').strip().lower()
            if lv in ('error', 'warning', 'fatal'):
                _error_logs[port].append(
                    {
                        'type': lv,
                        'message': json_data.get('ms', ''),
                        'timestamp': ts_iso,
                        'raw': json_data,
                    }
                )

        elif msg_type in ('error', 'warning', 'fatal'):
            et = 'error' if msg_type == 'error' else msg_type
            _error_logs[port].append(
                {
                    'type': et,
                    'message': json_data.get('ms', ''),
                    'timestamp': ts_iso,
                    'raw': json_data,
                }
            )


def _add_message(port, message):
    """Add a message to the in-memory buffer and log it."""
    timestamp = datetime.now(timezone.utc)
    local_time_str = timestamp.strftime('%H:%M:%S')  # Only time, no date
    
    message_entry = {
        'port': port,
        'timestamp': timestamp.isoformat(),
        'local_time': local_time_str,
        'message': message
    }
    
    with _monitoring_lock:
        if port not in _monitoring_messages:
            _monitoring_messages[port] = deque(maxlen=_max_messages_per_port)
        _monitoring_messages[port].append(message_entry)

    # Try to parse JSON from message
    json_data = _parse_json_message(message)

    # Fulfill response waiter for JSON {"status":"ok"} or text CLI acks (SET OK, SAVE OK, etc.)
    if _line_indicates_command_ok(message, json_data):
        ack = json_data if isinstance(json_data, dict) else {'status': 'ok', 'raw': message}
        with _monitoring_lock:
            waiter = _response_waiters.pop(port, None)
            if waiter is not None:
                waiter['result'] = ack
                waiter['event'].set()
    if json_data:
        _process_parsed_message(port, json_data, timestamp)

        export_mac = _pending_config_exports.get(port)
        if export_mac and not json_data.get('ty'):
            try:
                from .channel_recorder_sync import import_recorder_configuration

                channel = _settings_manager.get_channel_by_mac(export_mac)
                if channel:
                    import_recorder_configuration(
                        channel,
                        {'port': port},
                        json_data,
                    )
                    _pending_config_exports.pop(port, None)
                    _synchronized_config_mac[port] = export_mac
                    _logger.info(
                        'Synchronized recorder configuration into channel %s on %s',
                        channel['id'], port,
                    )
            except Exception as exc:
                _pending_config_exports.pop(port, None)
                _logger.error(
                    'Failed to synchronize initial recorder configuration on %s: %s',
                    port, exc,
                )

    # Extract and store MAC address if present in message
    mac_address = _extract_mac_from_message(message)
    if mac_address:
        with _monitoring_lock:
            _last_known_mac[port] = mac_address

        # Auto-create channel if needed (check at most once per minute per MAC)
        current_time = time.time()
        last_check = _last_channel_check_time.get(mac_address, 0)
        if current_time - last_check > 60:  # Check at most once per minute
            _last_channel_check_time[mac_address] = current_time
            _ensure_channel_for_mac(mac_address)
        if (_synchronized_config_mac.get(port) != mac_address and
                _pending_config_exports.get(port) != mac_address):
            if send_command_to_port(port, 'EXPORT'):
                _pending_config_exports[port] = mac_address

    # Reboot: log line with rr (DEVICE_SERIAL) or legacy info INIT (+ optional fw)
    if _detect_reboot(message):
        try:
            if not json_data or not isinstance(json_data, dict):
                pass
            elif json_data.get('rr'):
                mac_for_reboot = None
                with _monitoring_lock:
                    mac_for_reboot = _last_known_mac.get(port)
                if not mac_for_reboot:
                    messages_copy = []
                    with _monitoring_lock:
                        if port in _monitoring_messages:
                            messages_copy = list(_monitoring_messages[port])[-50:]
                    for msg_entry in messages_copy:
                        msg_mac = _extract_mac_from_message(msg_entry.get('message', ''))
                        if msg_mac:
                            with _monitoring_lock:
                                _last_known_mac[port] = msg_mac
                            mac_for_reboot = msg_mac
                            break
                _increment_reboot_count(port, mac_for_reboot, timestamp)
            elif json_data.get('ty') == 'info' and json_data.get('ms') == 'INIT' and json_data.get('fw'):
                mac_for_reboot = None
                with _monitoring_lock:
                    mac_for_reboot = _last_known_mac.get(port)
                if not mac_for_reboot:
                    messages_copy = []
                    with _monitoring_lock:
                        if port in _monitoring_messages:
                            messages_copy = list(_monitoring_messages[port])[-50:]
                    for msg_entry in messages_copy:
                        msg_mac = _extract_mac_from_message(msg_entry.get('message', ''))
                        if msg_mac:
                            with _monitoring_lock:
                                _last_known_mac[port] = msg_mac
                            mac_for_reboot = msg_mac
                            break
                _increment_reboot_count(port, mac_for_reboot, timestamp)
        except Exception as exc:
            _logger.debug("Error detecting reboot in message: %s", exc)
    
    # Log to file with MAC tag if available
    log_message = message
    if mac_address:
        # Tag the log message with MAC address for easier searching
        log_message = f"[MAC:{mac_address}] {message}"
    _log_message(port, log_message)


def _disconnect_and_cleanup(port, serial_conn):
    """Helper function to disconnect and cleanup a serial connection."""
    try:
        if serial_conn and serial_conn.is_open:
            serial_conn.close()
            _logger.info("Disconnected serial connection for port %s", port)
    except Exception as exc:
        _logger.warning("Error closing serial connection for %s: %s", port, exc)
    
    with _monitoring_lock:
        if port in _monitoring_connections:
            del _monitoring_connections[port]
    
    return None


def _is_valid_port_for_platform(port):
    """Check if a port is valid for the current platform."""
    import platform
    system = platform.system().lower()
    
    # On Linux/Unix, COM ports (Windows format) are not valid
    if system in ['linux', 'darwin', 'unix']:
        if port.upper().startswith('COM'):
            return False
    
    # On Windows, /dev/tty* ports (Unix format) are not valid
    if system == 'windows':
        if port.startswith('/dev/tty') or port.startswith('/dev/'):
            return False
    
    return True


def _monitor_serial_port(port):
    """Monitor a serial port for incoming messages with retry logic."""
    # Check if port is valid for current platform
    if not _is_valid_port_for_platform(port):
        import platform
        system = platform.system()
        _logger.warning(
            "Port %s is not valid for %s platform. Skipping monitoring. "
            "Please remove this port from the inventory using the delete button.",
            port, system
        )
        # Mark as inactive and return
        with _monitoring_lock:
            _monitoring_active[port] = False
            if port in _monitoring_threads:
                del _monitoring_threads[port]
        return
    
    _logger.info("Starting serial monitoring for port %s", port)
    
    serial_conn = None
    consecutive_errors = 0
    max_consecutive_errors = 10
    retry_delay = 5.0  # Initial retry delay in seconds
    max_retry_delay = 60.0  # Maximum retry delay
    consecutive_permission_errors = 0  # Track consecutive permission errors
    max_consecutive_permission_errors = 5  # After this many, assume port is in use by another component
    
    while True:
        # Check if monitoring is still active
        with _monitoring_lock:
            if not _monitoring_active.get(port, False):
                _logger.info("Monitoring stopped for port %s", port)
                break
        
        try:
            # Open serial connection if not already open
            if serial_conn is None or not serial_conn.is_open:
                # Check if there's already an open connection in the shared connections dict
                # Only reuse if it's actually open and valid
                with _monitoring_lock:
                    existing_conn = _monitoring_connections.get(port)
                    if existing_conn:
                        try:
                            # Verify the connection is actually open and valid
                            if existing_conn.is_open:
                                _logger.info("Reusing existing serial connection for port %s", port)
                                serial_conn = existing_conn
                                consecutive_errors = 0
                                consecutive_permission_errors = 0
                                retry_delay = 5.0
                            else:
                                # Connection exists but is not open, remove it
                                _logger.debug("Existing connection for %s is not open, removing from cache", port)
                                if port in _monitoring_connections:
                                    del _monitoring_connections[port]
                                serial_conn = None
                        except (AttributeError, OSError):
                            # Connection object is invalid, remove it
                            _logger.debug("Existing connection for %s is invalid, removing from cache", port)
                            if port in _monitoring_connections:
                                del _monitoring_connections[port]
                            serial_conn = None
                    else:
                        serial_conn = None
                
                # If we don't have a valid connection, try to open a new one
                if serial_conn is None or not serial_conn.is_open:
                    # Cleanup any existing connection first
                    serial_conn = _disconnect_and_cleanup(port, serial_conn)
                    
                    try:
                        serial_conn = Serial(
                            port=port,
                            baudrate=115200,
                            timeout=30.0,  # 30 second timeout as specified
                            write_timeout=1.0
                        )
                        _logger.info("Serial connection opened for port %s", port)
                        with _monitoring_lock:
                            _monitoring_connections[port] = serial_conn
                            _last_health_poll.pop(port, None)
                        consecutive_errors = 0
                        consecutive_permission_errors = 0  # Reset on successful connection
                        retry_delay = 5.0  # Reset retry delay on successful connection
                        try:
                            serial_conn.write(b'HEALTH ?\r\n')
                            serial_conn.flush()
                            _last_health_poll[port] = time.time()
                        except Exception:
                            pass
                    except (SerialException, PermissionError, OSError) as exc:
                            error_msg = str(exc).lower()
                            is_permission_error = any(keyword in error_msg for keyword in [
                                'access is denied',
                                'permission denied'
                            ]) or isinstance(exc, PermissionError)
                            
                            if is_permission_error:
                                consecutive_permission_errors += 1
                                
                                # If we've had many consecutive permission errors, the port is likely in use by another component
                                if consecutive_permission_errors >= max_consecutive_permission_errors:
                                    # Back off more aggressively - wait 60 seconds instead of 10
                                    wait_time = 60.0
                                    _logger.warning(
                                        "Port %s appears to be in use by another component (permission error %d/%d). "
                                        "Waiting %d seconds before retry...", 
                                        port, consecutive_permission_errors, max_consecutive_permission_errors, int(wait_time)
                                    )
                                    _add_message(
                                        port, 
                                        f"[INFO] Port appears in use by another component. Waiting {int(wait_time)}s before retry..."
                                    )
                                    _log_message(port, f"Port in use, waiting {int(wait_time)}s: {exc}")
                                    time.sleep(wait_time)
                                else:
                                    # Normal permission error retry
                                    _logger.warning(
                                        "Permission error opening port %s (%d/%d): %s. Retrying in 10 seconds...", 
                                        port, consecutive_permission_errors, max_consecutive_permission_errors, exc
                                    )
                                    _add_message(port, f"[RETRY] Permission denied, retrying in 10s: {exc}")
                                    _log_message(port, f"Permission error, retrying in 10s: {exc}")
                                    time.sleep(10.0)  # Wait 10 seconds for permission errors
                                continue
                            
                            # Other errors: retry with exponential backoff
                            _logger.warning("Failed to open port %s, retrying in %.1f seconds: %s", port, retry_delay, exc)
                            _add_message(port, f"[RETRY] Connection failed, retrying in {int(retry_delay)}s: {exc}")
                            _log_message(port, f"Connection failed, retrying: {exc}")
                            time.sleep(retry_delay)
                            retry_delay = min(retry_delay * 1.5, max_retry_delay)  # Exponential backoff
                            continue
            
            # Verify connection is still valid before reading
            if not serial_conn.is_open:
                _logger.warning("Connection to %s is not open, reconnecting...", port)
                _add_message(port, "[RETRY] Connection closed, reconnecting...")
                serial_conn = _disconnect_and_cleanup(port, serial_conn)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)
                continue
            
            # Read a line (will timeout after 30 seconds)
            line = serial_conn.readline()
            
            if line:
                # Decode and process the line
                try:
                    message = line.decode('utf-8', errors='replace').strip()
                    if message:
                        _add_message(port, message)
                        _logger.debug("Received message from %s: %s", port, message)
                except UnicodeDecodeError:
                    _logger.warning("Failed to decode message from %s", port)
            _maybe_request_health(port, serial_conn)

            # Reset all error counters on successful read - connection is working
            consecutive_errors = 0
            consecutive_permission_errors = 0  # Reset permission errors on successful read
            retry_delay = 5.0  # Reset retry delay on successful read
            
        except SerialTimeoutException:
            # Timeout is expected - just continue
            continue
        except (SerialException, PermissionError, OSError) as exc:
            consecutive_errors += 1
            error_msg = str(exc).lower()
            
            # Check if this is a permission error
            is_permission_error = any(keyword in error_msg for keyword in [
                'access is denied',
                'permission denied'
            ]) or isinstance(exc, PermissionError)
            
            if is_permission_error:
                consecutive_permission_errors += 1
                
                # Disconnect and cleanup
                serial_conn = _disconnect_and_cleanup(port, serial_conn)
                
                # If we've had many consecutive permission errors, the port is likely in use by another component
                if consecutive_permission_errors >= max_consecutive_permission_errors:
                    # Back off more aggressively - wait 60 seconds instead of 10
                    wait_time = 60.0
                    _logger.warning(
                        "Port %s appears to be in use by another component (permission error %d/%d). "
                        "Disconnecting and waiting %d seconds before retry...", 
                        port, consecutive_permission_errors, max_consecutive_permission_errors, int(wait_time)
                    )
                    _add_message(
                        port, 
                        f"[INFO] Port appears in use by another component. Waiting {int(wait_time)}s before retry..."
                    )
                    _log_message(port, f"Port in use, waiting {int(wait_time)}s: {exc}")
                    time.sleep(wait_time)
                else:
                    # Normal permission error retry
                    _logger.warning(
                        "Permission error on %s (%d/%d): %s. Disconnecting and retrying in 10 seconds...", 
                        port, consecutive_permission_errors, max_consecutive_permission_errors, exc
                    )
                    _add_message(port, f"[RETRY] Permission denied, disconnecting and retrying in 10s: {exc}")
                    _log_message(port, f"Permission error, disconnecting and retrying in 10s: {exc}")
                    time.sleep(10.0)
                
                consecutive_errors = 0  # Reset error count after disconnect
                continue
            
            # Check if connection was lost or read failed
            connection_lost = any(keyword in error_msg for keyword in [
                'device not found',
                'could not open port',
                'no such file',
                'read failed',
                'write failed',
                'connection lost',
                'port is already open',
                'cannot configure port'
            ])
            
            if connection_lost or consecutive_errors > 3:
                # Disconnect and reconnect on connection loss or multiple errors
                _logger.warning("Connection issue on %s (error %d): %s. Disconnecting and reconnecting...", 
                              port, consecutive_errors, exc)
                _add_message(port, f"[RETRY] Connection issue, disconnecting and retrying in {int(retry_delay)}s: {exc}")
                _log_message(port, f"Connection issue, disconnecting and retrying: {exc}")
                
                # Disconnect and cleanup
                serial_conn = _disconnect_and_cleanup(port, serial_conn)
                
                # Wait before retry
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)  # Exponential backoff
                consecutive_errors = 0  # Reset error count after disconnect
                continue
            
            if consecutive_errors <= max_consecutive_errors:
                _logger.warning("Serial error on %s (attempt %d/%d): %s", 
                              port, consecutive_errors, max_consecutive_errors, exc)
                # For minor errors, wait a bit but don't disconnect
                time.sleep(1.0 * consecutive_errors)  # Exponential backoff
            else:
                # Too many errors - disconnect and reconnect
                _logger.error("Too many consecutive errors on %s, disconnecting and reconnecting", port)
                _add_message(port, f"[RETRY] Too many errors, disconnecting and retrying in {int(retry_delay)}s")
                _log_message(port, f"Too many errors, disconnecting and retrying: {exc}")
                
                # Disconnect and cleanup
                serial_conn = _disconnect_and_cleanup(port, serial_conn)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)
                consecutive_errors = 0  # Reset for retry
        except Exception as exc:
            consecutive_errors += 1
            _logger.exception("Unexpected error monitoring port %s", port)
            
            # Check if it's a permission error wrapped in Exception
            error_msg = str(exc).lower()
            is_permission_error = any(keyword in error_msg for keyword in [
                'access is denied',
                'permission denied'
            ]) or isinstance(exc, PermissionError)
            
            if is_permission_error:
                consecutive_permission_errors += 1
                
                # Disconnect and cleanup
                serial_conn = _disconnect_and_cleanup(port, serial_conn)
                
                # If we've had many consecutive permission errors, the port is likely in use by another component
                if consecutive_permission_errors >= max_consecutive_permission_errors:
                    # Back off more aggressively - wait 60 seconds instead of 10
                    wait_time = 60.0
                    _logger.warning(
                        "Port %s appears to be in use by another component (unexpected permission error %d/%d). "
                        "Disconnecting and waiting %d seconds before retry...", 
                        port, consecutive_permission_errors, max_consecutive_permission_errors, int(wait_time)
                    )
                    _add_message(
                        port, 
                        f"[INFO] Port appears in use by another component. Waiting {int(wait_time)}s before retry..."
                    )
                    _log_message(port, f"Port in use, waiting {int(wait_time)}s: {exc}")
                    time.sleep(wait_time)
                else:
                    # Normal permission error retry
                    _logger.warning(
                        "Permission error (unexpected) on %s (%d/%d): %s. Disconnecting and retrying in 10 seconds...", 
                        port, consecutive_permission_errors, max_consecutive_permission_errors, exc
                    )
                    _add_message(port, f"[RETRY] Permission denied, disconnecting and retrying in 10s: {exc}")
                    _log_message(port, f"Permission error, disconnecting and retrying in 10s: {exc}")
                    time.sleep(10.0)
                
                consecutive_errors = 0  # Reset for retry
                continue
            
            if consecutive_errors > max_consecutive_errors:
                _logger.error("Too many consecutive errors on %s, disconnecting and reconnecting", port)
                _add_message(port, f"[RETRY] Unexpected error, disconnecting and retrying in {int(retry_delay)}s: {exc}")
                _log_message(port, f"Unexpected error, disconnecting and retrying: {exc}")
                
                # Disconnect and cleanup
                serial_conn = _disconnect_and_cleanup(port, serial_conn)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)
                consecutive_errors = 0  # Reset for retry
            else:
                time.sleep(1.0 * consecutive_errors)
    
    # Final cleanup
    serial_conn = _disconnect_and_cleanup(port, serial_conn)
    
    # Mark monitoring as inactive
    with _monitoring_lock:
        _monitoring_active[port] = False
        if port in _monitoring_threads:
            del _monitoring_threads[port]


def _load_inventory():
    """Load the recorder inventory from database."""
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
                    # If 'data' is a string, it might be JSON that needs parsing
                    # But get_all_recorders should already parse it
                    devices.append(record['data'])
            else:
                # If no 'data' field, use the record itself (for backward compatibility)
                devices.append(record)
        return devices
    except Exception as exc:
        _logger.error("Failed to load recorder inventory: %s", exc)
        return []


def start_monitoring_for_device(port):
    """Start monitoring a specific device."""
    with _monitoring_lock:
        if port in _monitoring_threads and _monitoring_threads[port].is_alive():
            _logger.warning("Monitoring already active for port %s", port)
            return False
        
        _monitoring_active[port] = True
        thread = threading.Thread(
            target=_monitor_serial_port,
            args=(port,),
            name=f"RecorderMonitor-{port}",
            daemon=True
        )
        thread.start()
        _monitoring_threads[port] = thread
        _logger.info("Started monitoring thread for port %s", port)
        return True


def stop_monitoring_for_device(port):
    """Stop monitoring a specific device."""
    from .channel_recorder_sync import forget_recorder_configuration

    forget_recorder_configuration(port)
    with _monitoring_lock:
        _pending_config_exports.pop(port, None)
        _synchronized_config_mac.pop(port, None)
        _last_known_mac.pop(port, None)
        if port not in _monitoring_threads:
            # Even if no thread, ensure connection is closed
            if port in _monitoring_connections:
                conn = _monitoring_connections[port]
                try:
                    if conn and conn.is_open:
                        conn.close()
                        _logger.info("Closed serial connection for port %s", port)
                except Exception as exc:
                    _logger.warning("Error closing serial connection for %s: %s", port, exc)
                del _monitoring_connections[port]
            return False
        
        _monitoring_active[port] = False
        
        # Explicitly close the serial connection immediately
        if port in _monitoring_connections:
            conn = _monitoring_connections[port]
            try:
                if conn and conn.is_open:
                    conn.close()
                    _logger.info("Closed serial connection for port %s", port)
            except Exception as exc:
                _logger.warning("Error closing serial connection for %s: %s", port, exc)
            del _monitoring_connections[port]
        
        # Wait for the thread to notice and finish
        thread = _monitoring_threads.get(port)
        if thread and thread.is_alive():
            thread.join(timeout=5.0)  # Increased timeout to 5 seconds
        
        if port in _monitoring_threads:
            del _monitoring_threads[port]
        
        _logger.info("Stopped monitoring for port %s", port)
        return True


def reconnect_device(port):
    """Disconnect and reconnect monitoring for a device."""
    _logger.info("Reconnecting device on port %s", port)
    
    # Stop monitoring
    stop_monitoring_for_device(port)
    
    # Wait a moment for cleanup
    time.sleep(1.0)
    
    # Start monitoring again
    return start_monitoring_for_device(port)


def reconnect_all_monitored_devices():
    """Reconnect all devices that are flagged for monitoring."""
    devices = _load_inventory()
    results = {}
    
    for device in devices:
        if device.get('monitor', False):
            port = device.get('port')
            if port:
                results[port] = reconnect_device(port)
    
    return results


def is_monitoring_active(port):
    """Check if monitoring is active for a port."""
    with _monitoring_lock:
        return _monitoring_active.get(port, False)


def _maybe_request_health(port, serial_conn):
    """Ask firmware for HEALTH so UI gets rc/uc/pq (DEVICE_SERIAL.md)."""
    if is_port_flashing(port) or not serial_conn or not getattr(serial_conn, 'is_open', False):
        return
    now = time.time()
    last = _last_health_poll.get(port)
    if last is not None and (now - last) < _HEALTH_POLL_INTERVAL_SEC:
        return
    try:
        serial_conn.write(b'HEALTH ?\r\n')
        serial_conn.flush()
        _last_health_poll[port] = now
        _logger.debug('Sent HEALTH to %s for recording stats', port)
    except Exception as exc:
        _logger.debug('HEALTH poll failed for %s: %s', port, exc)


def get_port_mac_map():
    """port -> MAC (12 hex) for routing API logs to DEVICES tab."""
    with _monitoring_lock:
        return dict(_last_known_mac)


def is_port_flashing(port):
    """Check if a port is currently being flashed."""
    with _flashing_lock:
        return port in _flashing_ports


def set_port_flashing(port, flashing=True):
    """Mark a port as being flashed or not."""
    with _flashing_lock:
        if flashing:
            _flashing_ports.add(port)
            _logger.debug("Marked port %s as flashing", port)
        else:
            _flashing_ports.discard(port)
            _logger.debug("Unmarked port %s as flashing", port)


def get_messages(port=None, limit=100):
    """Get messages from monitoring. If port is None, get messages from all ports."""
    with _monitoring_lock:
        if port:
            messages = list(_monitoring_messages.get(port, deque()))
            return messages[-limit:] if len(messages) > limit else messages
        else:
            # Get messages from all ports
            all_messages = []
            for port_name, messages in _monitoring_messages.items():
                all_messages.extend(list(messages))
            # Sort by timestamp
            all_messages.sort(key=lambda x: x.get('timestamp', ''))
            return all_messages[-limit:] if len(all_messages) > limit else all_messages


def send_command_to_port(port, command):
    """Send a command to a specific serial port. Uses existing monitoring connection if available."""
    # CRLF matches HEALTH probe and typical serial CLI. Never call _add_message while holding
    # _monitoring_lock — _add_message also acquires it (re-entrant deadlock).
    command_bytes = (command + '\r\n').encode('utf-8')
    monitoring_result = None  # None=no monitoring path; True=ok; ('serial', e); ('other', e)
    with _monitoring_lock:
        conn = _monitoring_connections.get(port)
        if conn is not None and conn.is_open:
            try:
                conn.write(command_bytes)
                conn.flush()
                monitoring_result = True
            except SerialException as exc:
                monitoring_result = ('serial', exc)
            except Exception as exc:
                monitoring_result = ('other', exc)

    if monitoring_result is True:
        _logger.info("Sent command to %s via monitoring connection: %s", port, command)
        _add_message(port, f"[SENT] {command}")
        return True
    if isinstance(monitoring_result, tuple):
        kind, exc = monitoring_result
        if kind == 'serial':
            _logger.error("Failed to send command to %s via monitoring connection: %s", port, exc)
            _add_message(port, f"[ERROR] Failed to send command: {exc}")
        else:
            _logger.error(
                "Unexpected error sending command to %s via monitoring connection: %s", port, exc
            )
            _add_message(port, f"[ERROR] Unexpected error sending command: {exc}")
        return False

    # No existing connection — open a temporary one
    try:
        with Serial(port=port, baudrate=115200, timeout=1.0, write_timeout=2.0) as conn:
            conn.write(command_bytes)
            conn.flush()
            _logger.info("Sent command to %s via temporary connection: %s", port, command)
            _add_message(port, f"[SENT] {command}")
            return True
    except SerialException as exc:
        error_msg = str(exc).lower()
        # Check if port is busy (likely being monitored)
        if 'access is denied' in error_msg or 'being used' in error_msg or 'busy' in error_msg:
            _logger.warning("Port %s is busy (likely being monitored). Try using the monitoring connection.", port)
            _add_message(port, f"[ERROR] Port is busy. Command not sent: {command}")
        else:
            _logger.error("Failed to send command to %s: %s", port, exc)
            _add_message(port, f"[ERROR] Failed to send command: {exc}")
        return False
    except Exception as exc:
        _logger.exception("Unexpected error sending command to %s", port)
        _add_message(port, f"[ERROR] Unexpected error sending command: {exc}")
        return False


def send_command_to_ports(ports, command):
    """Send a command to multiple serial ports."""
    results = {}
    for port in ports:
        results[port] = send_command_to_port(port, command)
    return results


def _line_indicates_command_ok(message, json_data=None):
    """True when a serial line acknowledges a CLI set/save/reboot command."""
    if json_data and isinstance(json_data, dict) and json_data.get('status') == 'ok':
        return True
    if not message or not isinstance(message, str):
        return False
    upper = message.upper()
    return any(
        marker in upper
        for marker in (
            'SET OK', 'SAVE OK', 'CONFIG OK', 'IMPORT OK', 'REBOOT',
            'AUTOCONFIG OK',
        )
    )


def _clear_response_waiter(port):
    """Drop any stale waiter so the next command gets a fresh ack."""
    with _monitoring_lock:
        stale = _response_waiters.pop(port, None)
    if stale is not None:
        stale['event'].set()


def send_command_and_wait_response(port, command, timeout=5.0, log_command=None):
    """
    Send a command to a serial port and wait for a JSON response with status=="ok", or timeout.
    If the port has an active monitoring connection, uses it and the monitor thread fulfills the waiter.
    Otherwise opens a temporary connection and reads until JSON status ok or timeout.
    Returns (success: bool, response: dict | None).
    """
    command_bytes = (command + '\r\n').encode('utf-8')

    with _monitoring_lock:
        conn = _monitoring_connections.get(port)

    if conn is not None and conn.is_open:
        _clear_response_waiter(port)
        event = threading.Event()
        waiter = {'event': event, 'result': None}
        with _monitoring_lock:
            _response_waiters[port] = waiter
        try:
            with _monitoring_lock:
                conn = _monitoring_connections.get(port)
                if conn is None or not conn.is_open:
                    _response_waiters.pop(port, None)
                    return False, None
                try:
                    conn.write(command_bytes)
                    conn.flush()
                except (SerialException, Exception) as exc:
                    _logger.error("Failed to send command to %s: %s", port, exc)
                    _response_waiters.pop(port, None)
                    return False, None
            _add_message(port, f"[SENT] {log_command or command}")
            ok = event.wait(timeout=timeout)
            with _monitoring_lock:
                data = _response_waiters.pop(port, None)
                result = data.get('result') if data else None
            return (ok and result is not None, result)
        except Exception as exc:
            _logger.exception("Unexpected error in send_command_and_wait_response for %s: %s", port, exc)
            with _monitoring_lock:
                _response_waiters.pop(port, None)
            return False, None

    # No monitoring connection: open temporary connection, send, read until JSON status ok or timeout
    try:
        with Serial(port=port, baudrate=115200, timeout=0.5, write_timeout=2.0) as ser:
            ser.write(command_bytes)
            ser.flush()
            _add_message(port, f"[SENT] {log_command or command}")
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = ser.readline()
                if not line:
                    continue
                try:
                    message = line.decode('utf-8', errors='replace').strip()
                    if not message:
                        continue
                    data = _parse_json_message(message)
                    if _line_indicates_command_ok(message, data):
                        return True, data if isinstance(data, dict) else {'status': 'ok', 'raw': message}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
        return False, None
    except SerialException as exc:
        error_msg = str(exc).lower()
        if 'access is denied' in error_msg or 'being used' in error_msg or 'busy' in error_msg:
            _logger.warning("Port %s is busy (likely being monitored). Use monitoring connection.", port)
        else:
            _logger.error("Failed to send/read on %s: %s", port, exc)
        return False, None


_AUTOCONFIG_INTER_COMMAND_DELAY_SEC = 2.5


def run_autoconfig_sequence(
    port,
    host_ssid,
    host_password,
    host_ip,
    host_port,
    command_interval=None,
):
    """
    Run the 7-step autoconfig sequence: set SSID, password, custom host/port, save, reboot.

    Sends each command over serial one at a time with command_interval seconds between them.
    Does not wait for device acknowledgements.
    """
    interval = command_interval if command_interval is not None else _AUTOCONFIG_INTER_COMMAND_DELAY_SEC
    if interval < 0:
        interval = _AUTOCONFIG_INTER_COMMAND_DELAY_SEC

    steps_spec = [
        (1, 'set wifi[0].ssid', host_ssid),
        (2, 'set wifi[0].password', host_password),
        (3, 'set upload.useCustomHost', 'true'),
        (4, 'set upload.customHost', host_ip),
        (5, 'set upload.customPort', str(host_port)),
        (6, 'save', None),
        (7, 'reboot', None),
    ]
    results = []
    for index, (step_num, cmd_prefix, value) in enumerate(steps_spec):
        if index > 0:
            time.sleep(interval)

        command = f'{cmd_prefix} {value}' if value is not None else cmd_prefix
        _logger.info("Autoconfig %s: step %s sending: %s", port, step_num, command)
        sent = send_command_to_port(port, command)
        results.append({
            'step': step_num,
            'command': command,
            'ok': sent,
            'sent': sent,
        })

    all_sent = all(step.get('sent') for step in results)
    return {
        'success': all_sent,
        'steps': results,
        'message': (
            'Autoconfig commands sent successfully'
            if all_sent
            else 'One or more autoconfig commands failed to send on the serial port'
        ),
    }


def _matches_esp32_bridge(port_info):
    """Check if a port matches ESP32 bridge characteristics."""
    # Vendor/Product IDs and descriptor keywords commonly exposed by CP210x bridges used on ESP32 boards
    ESP32_VENDOR_IDS = {0x10C4}
    ESP32_PRODUCT_IDS = {0xEA60, 0xEA63, 0xEA70}
    ESP32_KEYWORDS = (
        'cp210',
        'silicon labs',
        'usb to uart',
        'esp32'
    )
    
    descriptor_parts = filter(None, [
        port_info.description,
        port_info.manufacturer,
        port_info.product,
        port_info.device
    ])
    descriptor = ' '.join(descriptor_parts).lower()

    if port_info.vid in ESP32_VENDOR_IDS and port_info.pid in ESP32_PRODUCT_IDS:
        return True

    return any(keyword in descriptor for keyword in ESP32_KEYWORDS)


def _continuous_port_scanning():
    """Continuously scan for COM ports and start/stop monitoring as devices connect/disconnect."""
    _logger.info("Starting continuous port scanning thread...")
    
    last_known_ports = set()
    
    while _port_scanning_active:
        try:
            # Get current COM ports that match ESP32 devices
            current_ports = set()
            for port_info in serial.tools.list_ports.comports():
                if _matches_esp32_bridge(port_info):
                    current_ports.add(port_info.device)
            
            # Find newly connected ports
            new_ports = current_ports - last_known_ports
            for port in new_ports:
                _logger.info("New COM port detected: %s. Starting monitoring...", port)
                # Update inventory and start monitoring
                _update_inventory_for_port(port)
                # Don't start monitoring if port is being flashed
                if not is_port_flashing(port) and not is_monitoring_active(port):
                    start_monitoring_for_device(port)
            
            # Find disconnected ports
            disconnected_ports = last_known_ports - current_ports
            for port in disconnected_ports:
                _logger.info("COM port disconnected: %s. Stopping monitoring...", port)
                if is_monitoring_active(port):
                    stop_monitoring_for_device(port)
            
            # Update last known ports
            last_known_ports = current_ports
            
            # Also check existing inventory and ensure all are monitored
            devices = _load_inventory()
            for device in devices:
                port = device.get('port')
                if port and port in current_ports:
                    # Don't restart monitoring if port is being flashed
                    if is_port_flashing(port):
                        _logger.debug("Skipping monitoring restart for port %s (flash in progress)", port)
                        continue
                    # Ensure device is set to monitor=True
                    if not device.get('monitor', False):
                        _update_device_monitor_flag(port, True)
                    # Start monitoring if not already active
                    if not is_monitoring_active(port):
                        start_monitoring_for_device(port)
            
        except Exception as exc:
            _logger.exception("Error in continuous port scanning: %s", exc)
        
        # Wait before next scan
        time.sleep(_port_scan_interval)
    
    _logger.info("Continuous port scanning thread stopped.")


def _update_inventory_for_port(port):
    """Update inventory to include a port with monitor=True."""
    try:
        devices = _load_inventory()
        port_exists = False
        
        for device in devices:
            if device.get('port') == port:
                device['monitor'] = True
                device['last_seen'] = datetime.utcnow().isoformat() + 'Z'
                port_exists = True
                break
        
        if not port_exists:
            # Add new device to inventory
            devices.append({
                'port': port,
                'description': '',
                'status': 'available',
                'last_seen': datetime.utcnow().isoformat() + 'Z',
                'monitor': True
            })
        
        _save_inventory(devices)
    except Exception as exc:
        _logger.error("Failed to update inventory for port %s: %s", port, exc)


def _update_device_monitor_flag(port, monitor_flag):
    """Update the monitor flag for a device in inventory."""
    try:
        devices = _load_inventory()
        for device in devices:
            if device.get('port') == port:
                device['monitor'] = monitor_flag
                _save_inventory(devices)
                break
    except Exception as exc:
        _logger.error("Failed to update monitor flag for port %s: %s", port, exc)


def _save_inventory(devices):
    """Save inventory to database."""
    try:
        # Save each device to the database
        for device in devices:
            _settings_manager.save_recorder(device)
    except Exception as exc:
        _logger.error("Failed to save inventory: %s", exc)


def count_reboots_from_logs(port):
    """Count INIT messages (reboots) from log files for a specific port."""
    reboot_count = 0
    try:
        # Get all log files for this port from the shared logs directory
        if not _LOGS_DIR.exists():
            return 0
        
        # Support both legacy and new filename formats:
        # - Legacy: COM3_2025-11-21.log  (old format, may exist in logs/)
        # - New:    2025-11-21_COM3.log  (under logs/YYYY/MM/)
        safe_port = ''.join(c if c.isalnum() else '_' for c in port)
        log_files = []
        for file_path in _LOGS_DIR.rglob("*.log"):
            filename = file_path.name

            if (
                filename.startswith(f"{safe_port}_") or  # legacy format
                filename.endswith(f"_{safe_port}.log")   # new format
            ):
                log_files.append(file_path)     
                   
        # Sort by date (newest first)
        log_files.sort(reverse=True)
        
        # Parse each log file
        for log_file in log_files:
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        # Format: [2025-11-21 16:10:32] {"ty":"info","ms":"INIT", "fw":"EDGE-2025-11-18"}
                        # Look for INIT messages with firmware info (first INIT message)
                        if 'INIT' in line and '"fw"' in line:
                            try:
                                # Extract JSON from line
                                start = line.find('{')
                                end = line.rfind('}')
                                if start != -1 and end != -1 and end > start:
                                    json_str = line[start:end + 1]
                                    json_data = json.loads(json_str)
                                    if isinstance(json_data, dict):
                                        if json_data.get('ty') == 'info' and json_data.get('ms') == 'INIT' and json_data.get('fw'):
                                            reboot_count += 1
                            except (json.JSONDecodeError, ValueError):
                                continue
            except Exception as exc:
                _logger.warning("Error reading log file %s: %s", log_file, exc)
                continue
        
    except Exception as exc:
        _logger.error("Error counting reboots from logs for port %s: %s", port, exc)
    
    return reboot_count


def sync_reboot_counts_from_logs():
    """Sync reboot counts from log files to inventory for all devices."""
    try:
        devices = _load_inventory()
        updated = False
        
        for device in devices:
            port = device.get('port')
            if not port:
                continue
            
            # Count reboots from logs
            log_count = count_reboots_from_logs(port)
            current_count = device.get('reboot_count', 0)
            
            # Update if log count is higher (log files are source of truth)
            if log_count > current_count:
                device['reboot_count'] = log_count
                updated = True
                _logger.info("Updated reboot count for port %s: %d (from logs)", port, log_count)
        
        if updated:
            _save_inventory(devices)
        
        return updated
    except Exception as exc:
        _logger.error("Error syncing reboot counts from logs: %s", exc)
        return False


def start_port_scanning():
    """Start the continuous port scanning thread."""
    global _port_scanning_active, _port_scanning_thread
    
    with _monitoring_lock:
        if _port_scanning_active:
            _logger.warning("Port scanning already active")
            return False
        
        _port_scanning_active = True
        _port_scanning_thread = threading.Thread(
            target=_continuous_port_scanning,
            name="PortScanner",
            daemon=True
        )
        _port_scanning_thread.start()
        _logger.info("Started continuous port scanning thread")
        return True


def stop_port_scanning():
    """Stop the continuous port scanning thread."""
    global _port_scanning_active, _port_scanning_thread
    
    with _monitoring_lock:
        if not _port_scanning_active:
            return False
        
        _port_scanning_active = False
        
        if _port_scanning_thread and _port_scanning_thread.is_alive():
            _port_scanning_thread.join(timeout=5.0)
        
        _logger.info("Stopped continuous port scanning thread")
        return True


def initialize_monitoring():
    """Initialize monitoring for all devices in inventory (all are monitored by default)."""
    global _reboot_history_thread
    
    _logger.info("Initializing recorder monitoring...")
    
    # Ensure database is ready before proceeding
    try:
        # Test database connection by trying to load settings
        test_settings = _settings_manager.get_all_settings()
        _logger.debug("Database connection verified")
    except Exception as db_exc:
        _logger.warning("Database not ready yet, waiting 2 seconds: %s", db_exc)
        time.sleep(2.0)
        # Try once more
        try:
            test_settings = _settings_manager.get_all_settings()
            _logger.debug("Database connection verified after wait")
        except Exception as db_exc2:
            _logger.error("Database still not ready after wait: %s", db_exc2)
            # Continue anyway - might work later
    
    # Start background thread for processing reboot history queue
    if _reboot_history_thread is None or not _reboot_history_thread.is_alive():
        _reboot_history_thread = threading.Thread(
            target=_process_reboot_history_queue,
            name="RebootHistoryProcessor",
            daemon=True
        )
        _reboot_history_thread.start()
        _logger.info("Started reboot history background processor thread")
    
    # Small delay to allow Windows to initialize serial ports after reboot
    # This helps avoid immediate permission errors on startup
    time.sleep(2.0)
    
    devices = _load_inventory()
    monitored_count = 0
    
    # Monitor all devices in inventory (monitor flag defaults to True)
    for device in devices:
        port = device.get('port')
        if port:
            # Ensure monitor flag is True
            if not device.get('monitor', False):
                device['monitor'] = True
                _save_inventory(devices)
            
            # Start monitoring
            if (not port.startswith("/dev/") or  os.path.exists(port)) and start_monitoring_for_device(port):
                monitored_count += 1
                _logger.info("Started monitoring for device on port %s", port)
            else:
                _logger.warning("Failed to start monitoring for device on port %s (may already be active)", port)
    
    # Start continuous port scanning
    start_port_scanning()
    
    _logger.info("Recorder monitoring initialized. Monitoring %d device(s).", monitored_count)
    return monitored_count


def get_parsed_short_status(port):
    """Get the latest parsed short status message for a port."""
    with _monitoring_lock:
        return _parsed_short_status.get(port)


def _normalize_health_state(h):
    """Build API health object: system, recording, legacy, data (flat KPI-friendly), timestamp."""
    if not h:
        return None
    if isinstance(h, dict) and 'data' in h and 'system' not in h and 'recording' not in h:
        # Legacy single-object storage
        d = h.get('data') or {}
        return {
            'system': None,
            'recording': {'data': d, 'timestamp': h.get('timestamp')},
            'legacy': {'data': d, 'timestamp': h.get('timestamp')},
            'data': d,
            'timestamp': h.get('timestamp'),
        }
    rec = h.get('recording')
    sys_ = h.get('system')
    leg = h.get('legacy')
    flat = None
    if rec and rec.get('data'):
        flat = dict(rec['data'])
    elif leg and leg.get('data'):
        flat = dict(leg['data'])
    elif sys_ and sys_.get('data'):
        flat = dict(sys_['data'])
    return {
        'system': sys_,
        'recording': rec,
        'legacy': leg,
        'data': flat,
        'timestamp': h.get('timestamp'),
    }


def get_parsed_health_data(port):
    """Latest health (system + recording + flat data) for a port."""
    with _monitoring_lock:
        return _normalize_health_state(_parsed_health_data.get(port))


def get_parsed_config_data(port):
    """Get the latest parsed config data for a port."""
    with _monitoring_lock:
        return _parsed_config_data.get(port)


def get_error_logs(port):
    """Get the last 10 error/warning/fatal messages for a port."""
    with _monitoring_lock:
        if port in _error_logs:
            return list(_error_logs[port])
        return []


def get_all_parsed_data(port):
    """Get all parsed data for a port (short, health, config, error logs)."""
    with _monitoring_lock:
        return {
            'short': _parsed_short_status.get(port),
            'health': _normalize_health_state(_parsed_health_data.get(port)),
            'config': _parsed_config_data.get(port),
            'error_logs': list(_error_logs.get(port, deque())),
        }

