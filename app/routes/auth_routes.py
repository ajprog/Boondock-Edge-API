"""
Authentication and MFA routes.
Handles user login, logout, token verification, and MFA operations.
"""
import json
import os
import logging
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from flask import Blueprint, g, jsonify, request
from flasgger import swag_from

from ..utils.logging_setup import error_logger
from ..middleware.auth_middleware import require_auth
from ..utils.auth import (
    get_request_token,
)
from ..utils.password_utils import hash_password, verify_password, is_md5_hash
from ..utils.mfa_utils import generate_mfa_secret, generate_mfa_qr_code, verify_totp_code, get_totp_uri
from ..routes.route_utils import init_users
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

auth_bp = Blueprint('auth', __name__)
mfa_bp = Blueprint('mfa', __name__)


def _authenticated_user():
    """Return the current user record; MFA is unavailable to other principals."""
    if g.principal.get('type') != 'user':
        return None
    return _settings_manager.get_user(g.principal['email'])


@auth_bp.route('/login', methods=['POST'])
@swag_from({
    'tags': ['Authentication'],
    'summary': 'User login',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['email', 'password'],
                'properties': {
                    'email': {'type': 'string'},
                    'password': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Login successful'},
        '401': {'description': 'Invalid credentials'},
        '500': {'description': 'Server error'}
    }
})
def login():
    """Authenticate user and return session token."""
    try:
        data = request.get_json()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        
        if not email or not password:
            return jsonify({'error': 'Email and password required'}), 400
        
        init_users()
        user = _settings_manager.get_user(email)
        if user is None:
            # Don't reveal if user exists (prevent user enumeration)
            return jsonify({'error': 'Invalid credentials'}), 401
        stored_password = user.get('password', '')
        
        # Handle migration from MD5 to bcrypt
        if is_md5_hash(stored_password):
            # Legacy MD5 password - verify and upgrade
            md5_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
            if md5_hash == stored_password:
                # Upgrade to bcrypt
                user['password'] = hash_password(password)
                _settings_manager.save_user(email, user)
                logging.info(f"Upgraded password for user {email} from MD5 to bcrypt")
            else:
                return jsonify({'error': 'Invalid credentials'}), 401
        else:
            # Verify bcrypt password
            if not verify_password(password, stored_password):
                return jsonify({'error': 'Invalid credentials'}), 401
        
        # Check if MFA is enabled
        mfa_enabled = user.get('mfa_enabled', False)
        mfa_secret = user.get('mfa_secret', '')
        
        # If MFA is enabled, require TOTP code
        if mfa_enabled and mfa_secret:
            totp_code = data.get('totp_code', '')
            if not totp_code:
                return jsonify({
                    'error': 'MFA code required',
                    'mfa_required': True
                }), 401
            
            if not verify_totp_code(mfa_secret, totp_code):
                return jsonify({
                    'error': 'Invalid MFA code',
                    'mfa_required': True
                }), 401

        # Store session expiry as timezone-aware UTC.
        now_utc = datetime.now(timezone.utc)
        session_token = secrets.token_urlsafe(32)
        expires_at = now_utc + timedelta(hours=24)  # 24 hour session

        _settings_manager.issue_credential(
            'user', email, expires_at.isoformat(), token=session_token
        )

        # Check if MFA is enforced but not enabled
        mfa_enforced = user.get('mfa_enforced', False)
        show_mfa_reminder = mfa_enforced and not mfa_enabled
        
        principal = _settings_manager.get_principal('user', user['id'])
        if principal is None:
            logging.error('Unable to materialize principal for user %s', user['id'])
            return jsonify({'error': 'Internal server error'}), 500

        # Return user info (without password and MFA secret)
        return jsonify({
            'token': session_token,
            'user': {
                'id': principal['id'],
                'email': principal['email'],
                'name': principal['name'],
                'role': principal['role'],
                'groups': principal['groups'],
                'permissions': principal['permissions'],
                'keywords': principal['keywords'],
                'preferences': principal['preferences'],
            },
            'show_mfa_reminder': show_mfa_reminder
        }), 200
        
    except Exception as e:
        logging.error(f"Login error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@auth_bp.route('/logout', methods=['POST'])
@require_auth
@swag_from({
    'tags': ['Authentication'],
    'summary': 'User logout',
    'responses': {
        '200': {'description': 'Logged out successfully'},
        '500': {'description': 'Server error'}
    }
})
def logout():
    """Invalidate session token."""
    try:
        token = get_request_token()
        if token:
            _settings_manager.delete_credential(token)
        return jsonify({'message': 'Logged out successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@auth_bp.route('/verify', methods=['GET'])
@require_auth
@swag_from({
    'tags': ['Authentication'],
    'summary': 'Verify authentication token',
    'responses': {
        '200': {'description': 'Token is valid'},
        '401': {'description': 'Token is invalid or expired'},
        '500': {'description': 'Server error'}
    }
})
def verify_token():
    """Verify if a token is valid."""
    return jsonify({'valid': True, 'principal': g.principal}), 200

@mfa_bp.route('/setup', methods=['POST'])
@require_auth
@swag_from({
    'tags': ['MFA'],
    'summary': 'Generate MFA secret and QR code for user',
    'responses': {
        '200': {'description': 'MFA setup data returned'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_setup():
    """Generate MFA secret and QR code for the authenticated user."""
    try:
        user = _authenticated_user()
        if user is None:
            return jsonify({'error': 'User authentication required'}), 403
        email = user['email']
        
        # Generate new secret
        secret = generate_mfa_secret()
        
        # Generate QR code
        qr_code = generate_mfa_qr_code(secret, email)
        totp_uri = get_totp_uri(secret, email)
        
        # Store the secret without enabling MFA until the code is verified.
        user['mfa_secret'] = secret
        
        # Save user
        _settings_manager.save_user(email, user)
        
        return jsonify({
            'secret': secret,
            'qr_code': qr_code,
            'totp_uri': totp_uri
        }), 200
        
    except Exception as e:
        logging.error(f"MFA setup error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@mfa_bp.route('/verify-setup', methods=['POST'])
@require_auth
@swag_from({
    'tags': ['MFA'],
    'summary': 'Verify MFA setup with TOTP code',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['totp_code'],
                'properties': {
                    'totp_code': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'MFA verified and enabled'},
        '400': {'description': 'Invalid code'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_verify_setup():
    """Verify MFA setup code and enable MFA for user."""
    try:
        data = request.get_json()
        totp_code = data.get('totp_code', '')

        if not totp_code:
            return jsonify({'error': 'TOTP code required'}), 400

        user = _authenticated_user()
        if user is None:
            return jsonify({'error': 'User authentication required'}), 403
        email = user['email']
        pending_secret = user.get('mfa_secret', '')
        
        if not pending_secret:
            return jsonify({'error': 'No MFA setup in progress'}), 400
        
        # Verify TOTP code
        if not verify_totp_code(pending_secret, totp_code):
            return jsonify({'error': 'Invalid TOTP code'}), 400
        
        # Enable MFA
        user['mfa_enabled'] = True
        
        # Save user
        _settings_manager.save_user(email, user)
        
        return jsonify({'message': 'MFA enabled successfully'}), 200
        
    except Exception as e:
        logging.error(f"MFA verify setup error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@mfa_bp.route('/disable', methods=['POST'])
@require_auth
@swag_from({
    'tags': ['MFA'],
    'summary': 'Disable MFA for user',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['password', 'totp_code'],
                'properties': {
                    'password': {'type': 'string'},
                    'totp_code': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'MFA disabled successfully'},
        '400': {'description': 'Invalid credentials'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_disable():
    """Disable MFA for user (requires password and TOTP code)."""
    try:
        data = request.get_json()
        password = data.get('password', '')
        totp_code = data.get('totp_code', '')

        user = _authenticated_user()
        if user is None:
            return jsonify({'error': 'User authentication required'}), 403
        email = user['email']
        
        # Verify password
        stored_password = user.get('password', '')
        if not verify_password(password, stored_password):
            return jsonify({'error': 'Invalid password'}), 400
        
        # Verify TOTP code if MFA is enabled
        if user.get('mfa_enabled', False):
            mfa_secret = user.get('mfa_secret', '')
            if not verify_totp_code(mfa_secret, totp_code):
                return jsonify({'error': 'Invalid TOTP code'}), 400
        
        # Disable MFA
        user['mfa_enabled'] = False
        user.pop('mfa_secret', None)
        
        # Save user
        _settings_manager.save_user(email, user)
        
        return jsonify({'message': 'MFA disabled successfully'}), 200
        
    except Exception as e:
        logging.error(f"MFA disable error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@mfa_bp.route('/status', methods=['GET'])
@require_auth
@swag_from({
    'tags': ['MFA'],
    'summary': 'Get MFA status for user',
    'responses': {
        '200': {'description': 'MFA status returned'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_status():
    """Get MFA status for the authenticated user."""
    try:
        user = _authenticated_user()
        if user is None:
            return jsonify({'error': 'User authentication required'}), 403
        email = user['email']
        
        return jsonify({
            'mfa_enabled': user.get('mfa_enabled', False),
            'has_secret': bool(user.get('mfa_secret', '')),
            'mfa_enforced': user.get('mfa_enforced', False),
            'authenticated': True
        }), 200
        
    except Exception as e:
        logging.error(f"MFA status error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
