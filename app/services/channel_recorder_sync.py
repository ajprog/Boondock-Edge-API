"""Associate channels with USB recorders and synchronize their configuration."""

from copy import deepcopy
import json
import threading
from urllib.parse import urlsplit

from .recorder_monitor import get_port_mac_map, send_command_and_wait_response
from .settings_manager import get_settings_manager, normalize_mac_address


import logging

_logger = logging.getLogger(__name__)


_settings_manager = get_settings_manager()

_EXPORT_TO_CHANNEL = {
    'threshold': ('a.ath', 'ath'),
    'silence': ('a.stm', 'stm'),
    'min_rec': ('a.mrm', 'mrm'),
    'max_rec': ('a.xrm', 'xrm'),
    'audio_gain': ('a.cg', 'cg'),
    'device_hostname': ('ho', 'hostname'),
    'device_ip': ('cip.ip', 'ip'),
}

_CHANNEL_TO_RECORDER = {
    'threshold': ('a', 'ath'),
    'silence': ('a', 'stm'),
    'min_rec': ('a', 'mrm'),
    'max_rec': ('a', 'xrm'),
    'audio_gain': ('a', 'cg'),
}

_config_lock = threading.Lock()
_exported_configs = {}

def _inventory_by_port():
    inventory = {}
    for record in _settings_manager.get_all_recorders():
        device = record.get('data', record)
        if isinstance(device, dict) and device.get('port'):
            inventory[device['port']] = device
    return inventory

def find_connected_recorder(mac):
    """Return live recorder details for a channel MAC, or ``None``."""
    target = normalize_mac_address(mac)
    if not target:
        return None
    inventory = _inventory_by_port()
    for port, recorder_mac in get_port_mac_map().items():
        if normalize_mac_address(recorder_mac) == target:
            return {'port': port, **inventory.get(port, {})}
    return None

def _device_address(channel):
    return channel.get('device_hostname') or channel.get('device_ip')

def present_channel(channel, recorder=None):
    """Add computed recorder connection and dashboard-link fields."""
    result = dict(channel)
    result['usb_connected'] = recorder is not None
    address = _device_address(result)
    if address:
        result['device_dashboard_url'] = (
            address if urlsplit(address).scheme else f'http://{address}/'
        )
    else:
        result['device_dashboard_url'] = None
    return result

def _flatten_config(config, prefix=''):
    if not isinstance(config, dict):
        raise ValueError('Recorder returned an invalid configuration')
    flattened = {}
    for key, value in config.items():
        path = f'{prefix}.{key}' if prefix else key
        flattened[path] = value
        if isinstance(value, dict):
            flattened.update(_flatten_config(value, path))
    return flattened

def channel_from_device_configuration(channel, config):
    """Merge supported firmware fields into a channel without device I/O."""
    exported = _flatten_config(config)
    updated = dict(channel)
    for field, candidates in _EXPORT_TO_CHANNEL.items():
        for candidate in candidates:
            if candidate in exported and exported[candidate] is not None:
                value = exported[candidate]
                if field in {'threshold', 'silence', 'min_rec', 'max_rec', 'audio_gain'}:
                    value = str(value)
                updated[field] = value
                break
    return updated


def device_configuration_from_channel(channel):
    """Return the supported firmware settings stored on a channel."""
    config = {}
    for field, (section, setting) in _CHANNEL_TO_RECORDER.items():
        if channel.get(field) is not None:
            value = float(channel[field])
            config.setdefault(section, {})[setting] = int(value) if value.is_integer() else value
    if channel.get('device_hostname') is not None:
        config['ho'] = channel['device_hostname']
    if channel.get('device_ip') is not None:
        config['cip'] = {'ip': channel['device_ip']}
    return config


def import_recorder_configuration(channel, recorder, config):
    """Persist supported values from an Echo/Tango EXPORT document."""
    updated = channel_from_device_configuration(channel, config)
    with _config_lock:
        _exported_configs[recorder['port']] = deepcopy(config)
    for field in ('device_hostname', 'device_ip'):
        if recorder.get(field) or recorder.get(field.removeprefix('device_')):
            updated[field] = recorder.get(field) or recorder.get(field.removeprefix('device_'))
    _settings_manager.save_channel(updated)
    return updated

def forget_recorder_configuration(port):
    """Discard the saved export when a USB recorder disconnects."""
    with _config_lock:
        _exported_configs.pop(port, None)

def export_channel_configuration(channel, recorder):
    """Merge channel settings into the last export and import it atomically."""
    port = recorder['port']
    with _config_lock:
        saved_config = _exported_configs.get(port)
        if saved_config is None:
            raise RuntimeError('Recorder configuration has not been exported')
        config = deepcopy(saved_config)

    # Both transports use the same fields and numeric types. Preserve unrelated
    # settings from EXPORT, including other keys in the audio and IP sections.
    for key, value in device_configuration_from_channel(channel).items():
        if isinstance(value, dict):
            config.setdefault(key, {}).update(value)
        else:
            config[key] = value

    payload = json.dumps(config, separators=(',', ':'), ensure_ascii=True)
    success, _response = send_command_and_wait_response(
        port,
        f'IMPORT {payload}',
        log_command='IMPORT [configuration omitted]',
    )
    if not success:
        raise TimeoutError(
            f"Recorder did not confirm configuration import: "
            f"JSON length={len(payload.encode('utf-8'))}, "
            f"command length={len(f'IMPORT {payload}' + chr(13) + chr(10))}"
        )
    with _config_lock:
        _exported_configs[port] = config
