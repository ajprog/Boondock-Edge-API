"""Database-authoritative authentication helpers."""
import logging
from datetime import datetime, timedelta, timezone

from flask import g, has_request_context, jsonify, request

from ..services.settings_manager import get_settings_manager

log = logging.getLogger(__name__)
TOKEN_EXPIRY_HOURS = 24 * 7
_settings_manager = get_settings_manager()

def get_request_token():
    """Read a credential without inferring its principal type from its header."""
    authorization = (request.headers.get('Authorization') or '').strip()
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip() or None
    return (request.headers.get('X-API-Key') or '').strip() or None

def authenticate():
    """Get the token and authenticate it."""
    token = get_request_token()
    if not token:
        return jsonify({'error': 'Authentication required'}), 401
    principal = authenticate_token(token)
    if principal is None:
        log.info(
            'authentication_rejected route=%s token=%s result=invalid_or_expired',
            request.path,
            token,
        )
        return jsonify({'error': 'Invalid or expired token'}), 401
    g.principal = principal
    request.current_user = principal
    return None

def authenticate_token(token):
    """Resolve a token to one current, flat principal dictionary."""
    credential, result = _settings_manager.inspect_credential(token)
    diagnostic_credential = credential
    if diagnostic_credential is None and result == 'expired':
        diagnostic_credential = _settings_manager.get_credential_record(token)
    if result != 'success' or not credential:
        return None
    principal = _settings_manager.get_principal(
        credential['principal_type'], credential['principal_id']
    )
    if principal is None:
        return None
    principal['credential_id'] = credential['id']
    principal['credential_expires_at'] = credential.get('expires_at')
    return principal

def _normalize_mac(value):
    compact = ''.join(character for character in (value or '') if character.isalnum())
    compact = compact.upper()
    return ':'.join(compact[index:index + 2] for index in range(0, 12, 2))

def is_mac_registered(mac_address):
    """Return whether a MAC has an existing unexpired device credential."""
    channel = _settings_manager.get_channel_by_mac(_normalize_mac(mac_address)) if mac_address else None
    return bool(channel and _settings_manager.has_current_credential('device', channel['id']))

def generate_token(mac_address, expiry_hours=None):
    """Issue an additional device credential for an existing channel."""
    channel = _settings_manager.get_channel_by_mac(_normalize_mac(mac_address))
    if not channel:
        return None, None
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=expiry_hours if expiry_hours is not None else TOKEN_EXPIRY_HOURS
    )
    token, _ = _settings_manager.issue_credential(
        'device', str(channel['id']), expires_at.isoformat()
    )
    return token, expires_at.isoformat()

def get_mac_for_token(token, expected_mac=None):
    """Resolve a device token and log the diagnostic result."""
    credential, result = _settings_manager.inspect_credential(token)
    if credential and credential['principal_type'] == 'device':
        principal = _settings_manager.get_principal('device', credential['principal_id'])
        if principal:
            actual_mac = principal.get('mac') or ''
            if expected_mac and _normalize_mac(actual_mac) != _normalize_mac(expected_mac):
                return None
            return actual_mac
    return None
