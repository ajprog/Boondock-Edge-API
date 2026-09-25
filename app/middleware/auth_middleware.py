"""Visible route-level authentication and authorization decorators."""
from functools import wraps

from flask import g, jsonify, request

from ..utils.auth import authenticate

def require_auth(function):
    """Require a valid credential of any principal type."""
    @wraps(function)
    def decorated_function(*args, **kwargs):
        failure = authenticate()
        return failure or function(*args, **kwargs)

    decorated_function.access_policy = {'type': 'auth'}
    return decorated_function

def require_admin(function):
    """Require a user principal whose current role is administrator."""
    @wraps(function)
    def decorated_function(*args, **kwargs):
        failure = authenticate()
        if failure:
            return failure
        if g.principal.get('type') != 'user' or g.principal.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return function(*args, **kwargs)

    decorated_function.access_policy = {'type': 'admin'}
    return decorated_function

def require_permission(permissions, loader=None, id_argument=None, inject_as=None):
    """Require any listed permission and optionally inject an owned resource."""
    def decorator(function):
        @wraps(function)
        def decorated_function(*args, **kwargs):
            failure = authenticate()
            if failure:
                return failure
            principal = g.principal
            admin = principal.get('type') == 'user' and principal.get('role') == 'admin'
            if permissions and not admin:
                available = set(principal.get('permissions') or [])
                if not available.intersection(permissions):
                    return jsonify({'error': 'Permission required'}), 403
            if loader is not None:
                resource = None
                if id_argument:
                    argument = kwargs.get(id_argument) or request.values.get(id_argument)
                    resource = loader(principal, argument, id_argument)
                else:
                    resource = loader(principal)

                if resource is None:
                    return jsonify({'error': 'Resource not found'}), 404
                kwargs[inject_as] = resource
            return function(*args, **kwargs)

        decorated_function.access_policy = {
            'type': 'permission',
            'permissions': permissions,
            'loader': loader,
            'id_argument': id_argument,
            'inject_as': inject_as,
        }
        return decorated_function
    return decorator
