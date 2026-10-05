"""User administration, preferences, and MFA management."""

from flask import Blueprint, g, jsonify, request

from ..middleware.auth_middleware import require_admin, require_auth
from ..services.settings_manager import get_settings_manager
from ..utils.password_utils import hash_password

users_bp = Blueprint('users', __name__)
_settings_manager = get_settings_manager()


def _valid_groups(group_ids):
    return (
        isinstance(group_ids, list)
        and all(isinstance(group_id, int) and _settings_manager.get_group_by_id(group_id)
                for group_id in group_ids)
    )


def _valid_preferences(preferences, admin):
    if not isinstance(preferences, dict) or set(preferences) != {'display', 'inbox', 'reports'}:
        return False
    if not all(isinstance(preferences[name], dict) for name in preferences):
        return False
    admin_fields = {'show_duplicate_recordings', 'show_hallucinations'}
    return admin or not admin_fields.intersection(preferences['inbox'])


@users_bp.route('/users', methods=['GET'])
@require_admin
def get_users():
    return jsonify(list(_settings_manager.get_all_users(safe=True).values()))


@users_bp.route('/users/<int:user_id>', methods=['GET'])
@require_admin
def get_user(user_id):
    user = _settings_manager.get_user_by_id(user_id, safe=True)
    return (jsonify(user), 200) if user else (jsonify({'error': 'User not found'}), 404)


@users_bp.route('/users', methods=['POST'])
@require_admin
def create_user():
    data = request.get_json(silent=True) or {}
    if not all(data.get(field) for field in ('email', 'password', 'name', 'role')):
        return jsonify({'error': 'email, password, name, and role are required'}), 400
    email = data['email'].strip().lower()
    if data['role'] not in {'admin', 'member'} or not _valid_groups(data.get('groups', [])):
        return jsonify({'error': 'Invalid role or groups'}), 400
    if _settings_manager.get_user(email):
        return jsonify({'error': 'Email already exists'}), 409
    if ('preferences' in data and
            not _valid_preferences(data['preferences'], data['role'] == 'admin')):
        return jsonify({'error': 'Invalid preferences'}), 400
    user = {
        'name': data['name'], 'password': hash_password(data['password']),
        'role': data['role'], 'groups': data.get('groups', []),
    }
    if 'preferences' in data:
        user['preferences'] = data['preferences']
    if not _settings_manager.save_user(email, user):
        return jsonify({'error': 'Unable to create user'}), 500
    return jsonify(_settings_manager.get_user(email, safe=True)), 201


@users_bp.route('/users/<int:user_id>', methods=['PATCH'])
@require_auth
def update_user(user_id):
    user = _settings_manager.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json(silent=True) or {}
    principal = g.principal
    admin = principal.get('type') == 'user' and principal.get('role') == 'admin'
    if principal.get('type') != 'user' or (not admin and principal.get('id') != user_id):
        return jsonify({'error': 'Permission denied'}), 403
    if not admin and set(data) != {'preferences'}:
        return jsonify({'error': 'Only preferences may be updated'}), 403
    updates = {}
    for field in ('name', 'email'):
        if field in data:
            updates[field] = data[field].strip() if isinstance(data[field], str) else data[field]
    if 'role' in data:
        if data['role'] not in {'admin', 'member'}:
            return jsonify({'error': 'Invalid role'}), 400
        updates['role'] = data['role']
    if 'groups' in data:
        if not _valid_groups(data['groups']):
            return jsonify({'error': 'Invalid groups'}), 400
        updates['groups'] = data['groups']
    if data.get('password'):
        updates['password'] = hash_password(data['password'])
    if 'preferences' in data:
        target_is_admin = updates.get('role', user.get('role')) == 'admin'
        if not _valid_preferences(data['preferences'], target_is_admin):
            return jsonify({'error': 'Invalid preferences'}), 400
        updates['preferences'] = data['preferences']
    try:
        _settings_manager.update_user_by_id(user_id, updates)
    except Exception as exc:
        if 'unique' in str(exc).lower():
            return jsonify({'error': 'Email already exists'}), 409
        raise
    return jsonify(_settings_manager.get_user_by_id(user_id, safe=True))


@users_bp.route('/users/<int:user_id>', methods=['DELETE'])
@require_admin
def delete_user(user_id):
    if not _settings_manager.delete_user_by_id(user_id):
        return jsonify({'error': 'User not found'}), 404
    return '', 204


@users_bp.route('/users/<int:user_id>/mfa/reset', methods=['POST'])
@require_admin
def reset_mfa(user_id):
    if not _settings_manager.update_user_by_id(user_id, {'mfa_enabled': False, 'mfa_secret': None}):
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'message': 'MFA reset'})


@users_bp.route('/users/<int:user_id>/mfa/enforcement', methods=['POST'])
@require_admin
def set_mfa_enforcement(user_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('enforce'), bool):
        return jsonify({'error': 'enforce must be a boolean'}), 400
    if not _settings_manager.update_user_by_id(user_id, {'mfa_enforced': data['enforce']}):
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'mfa_enforced': data['enforce']})
