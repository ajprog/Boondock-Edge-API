"""
In-memory channel visual state management.
Tracks recording/idle/error/warning states for visual feedback without persisting to channels.json
"""
import threading
import time
from config import Config
from typing import Dict, Optional
from datetime import datetime, timezone
from flask import request

from .settings_manager import get_settings_manager, normalize_mac_address
from ..utils.sqlite_utils import connect_sqlite

_settings_manager = get_settings_manager()

# Canonical MAC = 12 hex uppercase (no colons)
# In-memory state store: {mac_key: {state, timestamp}}
_channel_states: Dict[str, Dict] = {}
# Last API/cloud activity per device (for offline detection)
_last_seen: Dict[str, float] = {}
_state_lock = threading.Lock()

# > 2× typical device ping interval (60s)
CLOUD_ACTIVITY_STALE_SECONDS = 150

def _mac_key(mac_address: str) -> str:
    if not mac_address:
        return ""
    k = normalize_mac_address(mac_address)
    return k if len(k) == 12 else mac_address.replace(":", "").replace("-", "").upper()

def touch_device_activity(mac_address: str) -> None:
    """Record that the device was recently seen (legacy ping or cloud event)."""
    k = _mac_key(mac_address)
    if not k or len(k) != 12:
        return
    with _state_lock:
        _last_seen[k] = time.time()

class ChannelVisualState:
    """Enum-like class for visual state constants."""
    IDLE = "idle"
    RECORDING = "recording"
    ERROR = "error"
    WARNING = "warning"

def set_channel_visual_state(mac_address: str, state: str) -> None:
    """
    Set the visual state for a channel in memory.

    Args:
        mac_address: Channel MAC (any format)
        state: recording, idle, error, warning, online, offline, etc.
    """
    mac = _mac_key(mac_address)
    if not mac:
        return
    with _state_lock:
        _channel_states[mac] = {
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

def get_stored_visual_state(mac_address: str) -> Optional[str]:
    """State without offline stale check (e.g. ping must not clear recording)."""
    mac = _mac_key(mac_address)
    if not mac:
        return None
    with _state_lock:
        if mac in _channel_states:
            return _channel_states[mac].get("state")
    return None

def get_channel_visual_state(mac_address: str) -> Optional[str]:
    """
    Effective visual state: offline if last activity is stale; else stored state.
    """
    mac = _mac_key(mac_address)
    if not mac:
        return None
    with _state_lock:
        last = _last_seen.get(mac)
        if last is not None and (time.time() - last) > CLOUD_ACTIVITY_STALE_SECONDS:
            return "offline"
        if mac in _channel_states:
            return _channel_states[mac].get("state")
    return None

def get_all_channel_visual_states(channels) -> Dict[str, Dict]:
    """Return effective visual state for the supplied channel records."""
    with _state_lock:
        out: Dict[str, Dict] = {}
        now = time.time()
        for channel in channels:
            k = _mac_key(channel.get("mac"))
            if not k:
                continue
            last = _last_seen.get(k)
            if last is not None and (now - last) > CLOUD_ACTIVITY_STALE_SECONDS:
                st = "offline"
            elif k in _channel_states:
                st = _channel_states[k].get("state")
            else:
                st = None
            ts = _channel_states.get(k, {}).get("timestamp")
            if st is not None or k in _channel_states or (
                last is not None and (now - last) <= CLOUD_ACTIVITY_STALE_SECONDS
            ):
                out[k] = {"state": st, "timestamp": ts}
        return out

def load_owned_channels(principal):
    """Load the channels accessible to the principal."""
    return _settings_manager.get_all_channels(owner_ids=principal.get('owner_ids'))

def load_request_channel(principal, value, id_argument):
    """Load the device channel or an owned channel."""
    if principal.get('type') == 'device':
        return _settings_manager.get_channel(principal.get('id'))
    elif id_argument == 'channel_id':
        owner_ids = principal.get('owner_ids')
        if owner_ids is None:
            return _settings_manager.get_channel(value)
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return None
        return next(
            (channel for channel in _settings_manager.get_all_channels(owner_ids=owner_ids)
             if channel.get('id') == channel_id),
            None,
        )
    elif id_argument == 'mac':
        mac_address = normalize_mac_address(value) if value else None
        if not mac_address or len(mac_address) != 12:
            return None
        return _settings_manager.get_channel_by_mac(
            mac_address, owner_ids=principal.get('owner_ids'))
    return None


"""Ownership-scoped recording loaders used by route authorization decorators."""

def _recording_join_scope(principal):
    """Build JOINs and ownership predicates for recording queries."""
    joins = ["LEFT JOIN channels c ON c.id = recordings.channel_id"]
    clauses = []
    parameters = []

    owner_ids = principal.get('owner_ids')

    # Non-device principals with no owner scope are unrestricted (for example admin).
    if principal.get("type") != "device" and owner_ids is None:
        return joins, clauses, parameters

    predicate, owner_parameters = _settings_manager._channel_owner_filter(owner_ids)

    joins.extend([
        "JOIN channel_owners co ON co.channel_id = c.id",
    ])
    clauses.append("c.deleted = 0")
    clauses.append(f"({predicate})")
    parameters.extend(owner_parameters)

    return joins, clauses, parameters

def _recording_request_filters(clauses, parameters):
    """Add common recording filters from the current request query string."""
    since_timestamp = request.args.get('since_timestamp', type=str)
    before_timestamp = request.args.get('before_timestamp', type=str)
    before_id = request.args.get('before_id', type=int)

    if since_timestamp:
        clauses.append("recordings.timestamp >= ?")
        parameters.append(since_timestamp)

    if before_timestamp:
        if before_id is not None:
            clauses.append(
                "(recordings.timestamp < ? OR "
                "(recordings.timestamp = ? AND recordings.id < ?))"
            )
            parameters.extend([before_timestamp, before_timestamp, before_id])
        else:
            clauses.append("recordings.timestamp < ?")
            parameters.append(before_timestamp)

    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    day = request.args.get('day', type=int)
    hour = request.args.get('hour', type=int)

    timestamp_prefix = None
    if year is not None and month is not None:
        timestamp_prefix = f"{year}{month:02d}"
        if day is not None:
            timestamp_prefix += f"{day:02d}_"
            if hour is not None:
                timestamp_prefix += f"{hour:02d}"

    if timestamp_prefix:
        clauses.append("recordings.timestamp LIKE ?")
        parameters.append(f"{timestamp_prefix}%")

    return hour is not None

def load_request_recording(principal, value, id_argument):
    """Load one recording within the principal's channel scope."""
    joins, clauses, parameters = _recording_join_scope(principal)

    if id_argument == 'recording_id':
        clauses.append("recordings.id = ?")
    elif id_argument == 'filename':
        clauses.append("recordings.filename = ?")
    else:
        raise ValueError(f"Unsupported recording identifier: {id_argument}")
    parameters.append(value)

    query = f"""
        SELECT DISTINCT recordings.*, c.name AS channel_name
        FROM recordings
        {' '.join(joins)}
        WHERE {' AND '.join(clauses) if clauses else '1'}
        LIMIT 1
    """

    with connect_sqlite(
        Config.get_recordings_db_path(), row_factory=True
    ) as connection:
        row = connection.execute(query, parameters).fetchone()

    return dict(row) if row else None


def load_owned_recordings(principal, value = None, id_argument = None):
    """Load all recordings within the principal's channel scope."""
    joins, clauses, parameters = _recording_join_scope(principal)

    if id_argument == 'channel_id':
        clauses.append("recordings.channel_id = ?")
        parameters.append(value)
    elif id_argument is not None:
        raise ValueError(f"Unsupported recording scope: {id_argument}")

    order_ascending = _recording_request_filters(clauses, parameters)

    limit = request.args.get('limit', type=int)
    if limit is not None:
        limit = max(1, min(limit, 5000))

    query = f"""
        SELECT DISTINCT recordings.*, c.name AS channel_name
        FROM recordings
        {' '.join(joins)}
        WHERE {' AND '.join(clauses) if clauses else '1'}
        ORDER BY recordings.timestamp {'ASC' if order_ascending else 'DESC'}, recordings.id {'ASC' if order_ascending else 'DESC'}
    """

    # Fetch one extra row when paginating so the route can report has_more.
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit + 1)

    with connect_sqlite(
        Config.get_recordings_db_path(), row_factory=True
    ) as connection:
        rows = connection.execute(query, parameters).fetchall()

    return [dict(row) for row in rows]
