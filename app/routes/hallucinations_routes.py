"""Dedicated CRUD operations for hallucination patterns."""
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from ..middleware.auth_middleware import require_admin

from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

hallucinations_bp = Blueprint('hallucinations', __name__)


@hallucinations_bp.route('/hallucinations', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Hallucinations'],
    'summary': 'Create a hallucination pattern',
    'description': (
        'Creates one literal or Python-compatible regular-expression pattern. '
        'Literal, case-insensitive matching is used by default.'
    ),
    'parameters': [{
        'name': 'body',
        'in': 'body',
        'required': True,
        'schema': {
            'type': 'object',
            'required': ['pattern'],
            'properties': {
                'pattern': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                'match_type': {
                    'type': 'string',
                    'enum': ['literal', 'regex'],
                    'default': 'literal',
                },
                'case_sensitive': {'type': 'boolean', 'default': False},
            },
        },
    }],
    'responses': {
        '201': {'description': 'Hallucination pattern created'},
        '400': {'description': 'Invalid pattern payload'},
        '500': {'description': 'Server error'},
    },
})
def create_hallucination():
    """Create and return one explicit hallucination pattern."""
    try:
        data = request.get_json(silent=True)
        pattern_id = _settings_manager.save_hallucination(data)
        if pattern_id < 0:
            raise RuntimeError('Failed to save hallucination pattern')
        pattern = next(
            item for item in _settings_manager.get_all_hallucinations()
            if item['id'] == pattern_id
        )
        return jsonify(pattern), 201
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except Exception as error:
        return jsonify({'error': str(error)}), 500


@hallucinations_bp.route('/hallucinations', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Hallucinations'],
    'summary': 'List hallucination patterns',
    'responses': {
        '200': {
            'description': 'Complete hallucination-pattern collection',
            'schema': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'integer'},
                        'pattern': {'type': 'string'},
                        'match_type': {
                            'type': 'string',
                            'enum': ['literal', 'regex'],
                        },
                        'case_sensitive': {'type': 'boolean'},
                    },
                },
            },
        },
    },
})
def list_hallucinations():
    """Return all explicit hallucination patterns."""
    return jsonify(_settings_manager.get_all_hallucinations()), 200


@hallucinations_bp.route('/hallucinations/<int:hallucinations_id>', methods=['DELETE'])
@require_admin
@swag_from({
    'tags': ['Hallucinations'],
    'summary': 'Delete a hallucination pattern',
    'parameters': [{
        'name': 'hallucinations_id',
        'in': 'path',
        'type': 'integer',
        'required': True,
        'description': 'Stable hallucination-pattern ID',
    }],
    'responses': {
        '204': {'description': 'Hallucination pattern deleted'},
        '404': {'description': 'Hallucination pattern not found'},
    },
})
def delete_hallucination(hallucinations_id):
    """Delete a hallucination entry by ID."""
    if not _settings_manager.delete_hallucination(hallucinations_id):
        return jsonify({'error': 'Hallucination not found'}), 404
    return '', 204
