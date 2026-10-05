"""
In-memory channel visual state management.
Tracks recording/idle/error/warning states for visual feedback without persisting to channels.json
"""
import threading
import time
from config import Config
from typing import Dict, Optional
from datetime import datetime, timedelta, timezone
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

# > 2x typical device ping interval (60s)
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
    """Set the visual state for a channel in memory."""
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
    """Effective visual state: offline if last activity is stale; else stored state."""
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
        return _settings_manager.get_channel(channel_id, owner_ids=owner_ids)
    elif id_argument == 'mac':
        mac_address = normalize_mac_address(value) if value else None
        if not mac_address or len(mac_address) != 12:
            return None
        return _settings_manager.get_channel_by_mac(
            mac_address, owner_ids=principal.get('owner_ids')
        )
    return None

"""Ownership-scoped recording loaders used by route authorization decorators."""

def _recording_join_scope(principal):
    """Build JOINs and ownership predicates for recording queries."""
    joins = [
        "LEFT JOIN settings.channels c ON c.id = recordings.channel_id"
    ]
    clauses = []
    parameters = []

    owner_ids = principal.get('owner_ids')

    # An unrestricted principal (for example an admin) can see every recording.
    if owner_ids is None:
        return joins, clauses, parameters

    predicate, owner_parameters = _settings_manager._channel_owner_filter(owner_ids)

    joins.append(
        "JOIN settings.channel_owners co ON co.channel_id = c.id"
    )
    clauses.append("c.deleted = 0")
    clauses.append(f"({predicate})")
    parameters.extend(owner_parameters)

    return joins, clauses, parameters

def _recording_request_filters(clauses, parameters):
    """Add common recording filters from the current request query string."""
    since_timestamp = request.args.get('since_timestamp', type=int)
    before_timestamp = request.args.get('before_timestamp', type=int)
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

    if year is not None and month is not None:
        try:
            start = datetime(year, month, day or 1, hour or 0, tzinfo=timezone.utc)
            if hour is not None:
                end = start + timedelta(hours=1)
            elif day is not None:
                end = start + timedelta(days=1)
            elif month == 12:
                end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            clauses.extend(("recordings.timestamp >= ?", "recordings.timestamp < ?"))
            parameters.extend((int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
        except ValueError:
            clauses.append("0")

    return hour is not None

def _recording_connection():
    """Open the recordings DB and attach the real settings DB for ownership joins."""
    connection = connect_sqlite(
        Config.get_recordings_db_path(), row_factory=True, typed=True
    )
    try:
        connection.execute(
            "ATTACH DATABASE ? AS settings",
            (str(Config.get_settings_db_path()),),
        )
    except Exception:
        connection.close()
        raise
    return connection

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

    with _recording_connection() as connection:
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
        ORDER BY recordings.timestamp {'ASC' if order_ascending else 'DESC'},
                 recordings.id {'ASC' if order_ascending else 'DESC'}
    """

    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit + 1)

    with _recording_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()

    return [dict(row) for row in rows]


def _inbox_argument(name, default=None):
    value = request.args.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f'{name} must be an integer') from error


def load_inbox(principal, value=None, id_argument=None):
    """Load one authorized inbox window and its matching total."""
    try:
        joins, base_clauses, base_parameters = _recording_join_scope(principal)
        channel_values = []
        for value in request.args.getlist('channel_ids'):
            channel_values.extend(part.strip() for part in value.split(',') if part.strip())
        if channel_values:
            try:
                channel_ids = [int(item) for item in channel_values]
            except ValueError as error:
                raise ValueError('channel_ids must contain integers') from error
            if any(channel_id <= 0 for channel_id in channel_ids):
                raise ValueError('channel_ids must contain positive integers')
            placeholders = ','.join('?' for _ in channel_ids)
            base_clauses.append(f'recordings.channel_id IN ({placeholders})')
            base_parameters.extend(channel_ids)

        start_at = _inbox_argument('start_at')
        end_at = _inbox_argument('end_at')
        updated_since = _inbox_argument('updated_since')
        before_timestamp = _inbox_argument('before_timestamp')
        before_id = _inbox_argument('before_id')
        offset = _inbox_argument('offset', 0)
        limit = _inbox_argument('limit')
        sort_direction = request.args.get('sort_direction', 'newest_first')
        if sort_direction not in {'newest_first', 'oldest_first'}:
            raise ValueError('sort_direction must be newest_first or oldest_first')
        if start_at is not None:
            base_clauses.append('recordings.timestamp >= ?')
            base_parameters.append(start_at)
        if end_at is not None:
            base_clauses.append('recordings.timestamp <= ?')
            base_parameters.append(end_at)
        if start_at is not None and end_at is not None and start_at > end_at:
            raise ValueError('start_at must not be greater than end_at')
        if updated_since is not None and end_at is not None:
            raise ValueError('poll requests cannot include end_at')
        if (before_timestamp is None) != (before_id is None):
            raise ValueError('before_timestamp and before_id must be supplied together')
        if updated_since is not None and before_timestamp is not None:
            raise ValueError('poll requests cannot include older-window boundaries')
        if updated_since is None and offset:
            raise ValueError('offset is only valid for poll requests')
        if offset < 0:
            raise ValueError('offset must not be negative')

        inbox_preferences = principal.get('preferences', {}).get('inbox', {})
        if not inbox_preferences.get('show_duplicate_recordings', False):
            base_clauses.append('recordings.is_duplicate = FALSE')
        if not inbox_preferences.get('show_hallucinations', False):
            base_clauses.append('recordings.is_hallucination = FALSE')
        if limit is None:
            preferred = inbox_preferences.get('records_per_page', 20)
            limit = preferred if isinstance(preferred, int) and preferred > 0 else 100
        if limit < 1 or limit > 500:
            raise ValueError('limit must be between 1 and 500')

        where = ' AND '.join(base_clauses) if base_clauses else '1'
        with _recording_connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(DISTINCT recordings.id) FROM recordings {' '.join(joins)} WHERE {where}",
                base_parameters,
            ).fetchone()[0]

            clauses = list(base_clauses)
            parameters = list(base_parameters)
            if updated_since is not None:
                clauses.append('recordings.updated_at >= ?')
                parameters.append(updated_since)
                order = 'recordings.updated_at ASC, recordings.id ASC'
            else:
                direction = 'DESC' if sort_direction == 'newest_first' else 'ASC'
                if before_timestamp is not None:
                    operator = '<' if direction == 'DESC' else '>'
                    clauses.append(
                        f'(recordings.timestamp {operator} ? OR '
                        f'(recordings.timestamp = ? AND recordings.id {operator} ?))'
                    )
                    parameters.extend((before_timestamp, before_timestamp, before_id))
                order = f'recordings.timestamp {direction}, recordings.id {direction}'
            query = f"""
                SELECT DISTINCT recordings.id, recordings.channel_id,
                    recordings.filename, recordings.timestamp, recordings.status,
                    recordings.transcription, recordings.duration,
                    recordings.is_hallucination, recordings.updated_at
                FROM recordings {' '.join(joins)}
                WHERE {' AND '.join(clauses) if clauses else '1'}
                ORDER BY {order} LIMIT ? OFFSET ?
            """
            rows = connection.execute(query, (*parameters, limit + 1, offset)).fetchall()

        has_more = len(rows) > limit
        messages = [dict(row) for row in rows[:limit]]
        last = messages[-1] if messages else None
        return ({
            'messages': messages,
            'meta': {
                'total': total,
                'has_more': has_more,
                'next_before_timestamp': (
                    last['timestamp'] if has_more and updated_since is None else None
                ),
                'next_before_id': last['id'] if has_more and updated_since is None else None,
                'next_offset': offset + limit if has_more and updated_since is not None else None,
            },
        }, 200)
    except ValueError as error:
        return ({'error': str(error)}, 400)
