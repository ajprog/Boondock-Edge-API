"""Static authorization checks against Flask's registered API routes.

These tests inspect the application URL map rather than stand-in handlers.  The
public allowlist is intentionally small and method-specific: adding a new HTTP
method to a public path does not make the new method public automatically.
"""

import ast
import inspect
import textwrap

import pytest

PUBLIC_API_METHODS = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/v1/events"),
    ("GET", "/api/time"),
    ("GET", "/api/ping"),
    ("GET", "/api/docs/<path:filename>"),
    ("GET", "/api/health/devices"),
    ("GET", "/api/health/devices/<mac>"),
    ("GET", "/api/health/system"),
}


@pytest.fixture(scope="module")
def app():
    from app import create_app

    return create_app()


def _api_methods(app):
    """Map each registered API method and rule to its Flask view function."""
    methods = {}
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/"):
            continue
        view = app.view_functions[rule.endpoint]
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            methods[(method, rule.rule)] = (rule, view)
    return methods


def _route_function(view):
    """Return the function declaration registered beneath Flask wrappers."""
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(view)))
    return ast.parse(source).body[0]


def _authorization_declarations(view):
    """Return authorization decorators written on the real route handler."""
    declarations = []
    for decorator in _route_function(view).decorator_list:
        function = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(function, ast.Name) and function.id in {
            "require_auth",
            "require_admin",
            "require_permission",
        }:
            declarations.append((function.id, decorator))
    return declarations


def _reads_authentication_header(view):
    """Return whether the underlying route reads an auth header directly."""
    tree = _route_function(view)
    for node in ast.walk(tree):
        owner = key = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            owner, key = node.func.value, node.args[0]
        elif isinstance(node, ast.Subscript):
            owner, key = node.value, node.slice

        is_request_headers = (
            isinstance(owner, ast.Attribute)
            and owner.attr == "headers"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "request"
        )
        if not is_request_headers or not isinstance(key, ast.Constant):
            continue
        if isinstance(key.value, str) and key.value.lower() in {
            "authorization",
            "x-api-key",
        }:
            return True
    return False


def _permission_values(route, declaration):
    assert (
        isinstance(declaration, ast.Call) and declaration.args
    ), f"{route}: require_permission must receive a permission list"
    permissions = declaration.args[0]
    assert isinstance(
        permissions, ast.List
    ), f"{route}: require_permission must receive a list"
    values = [
        item.value if isinstance(item, ast.Constant) else None
        for item in permissions.elts
    ]
    assert all(
        isinstance(value, str) and value.strip() for value in values
    ), f"{route}: permissions must be non-empty string literals"
    return values


def _keyword(declaration, name):
    return next(
        (keyword.value for keyword in declaration.keywords if keyword.arg == name),
        None,
    )


def _assert_loader_declaration(route, rule, declaration):
    loader = _keyword(declaration, "loader")
    id_argument = _keyword(declaration, "id_argument")
    inject_as = _keyword(declaration, "inject_as")

    assert (
        id_argument is None or loader is not None
    ), f"{route}: id_argument requires a loader"
    if loader is None:
        assert inject_as is None, f"{route}: inject_as requires a loader"
        return

    assert (
        isinstance(inject_as, ast.Constant)
        and isinstance(inject_as.value, str)
        and inject_as.value.isidentifier()
    ), f"{route}: loader requires a valid inject_as parameter name"
    if id_argument is not None:
        assert isinstance(id_argument, ast.Constant) and isinstance(
            id_argument.value, str
        ), f"{route}: id_argument must be a string literal"
        assert (
            id_argument.value in rule.arguments
        ), f"{route}: id_argument={id_argument.value!r} is absent from the Flask rule"


def check_api_routes(app):
    """Check URL shape, public surface, and authorization declarations."""
    api_methods = _api_methods(app)
    registered_methods = set(api_methods)

    stale_public_entries = PUBLIC_API_METHODS - registered_methods
    assert not stale_public_entries, (
        f"public allowlist contains routes Flask does not register: "
        f"{sorted(stale_public_entries)}"
    )

    for method_and_rule, (rule, view) in api_methods.items():
        route = " ".join(method_and_rule)
        assert not rule.rule.startswith(
            "/api/api/"
        ), f"{route}: route contains a duplicate /api prefix"
        declarations = _authorization_declarations(view)
        if method_and_rule in PUBLIC_API_METHODS:
            assert (
                not declarations
            ), f"{route}: public route also declares authorization"
            continue

        assert (
            len(declarations) == 1
        ), f"{route}: protected route must declare exactly one authorization policy"
        name, declaration = declarations[0]
        if name == "require_permission":
            _permission_values(route, declaration)
            _assert_loader_declaration(route, rule, declaration)

    # Generic protection is insufficient for these security-sensitive routes.

    for (method, path), (rule, view) in api_methods.items():
        if rule.endpoint.startswith(("users.", "groups.", "release_package.")):
            assert (
                _authorization_declarations(view)[0][0] == "require_admin"
            ), f"{method} {path}: {rule.endpoint} must remain admin-only"

    expected = {
        ("POST", "/api/uploads/queue"): ("require_admin", None),
        ("GET", "/api/queue/status"): ("require_admin", None),
        ("POST", "/api/queue/requeue/<filename>"): ("require_admin", None),
        ("GET", "/api/v1/transcriptions"): (
            "require_permission",
            ["transcriptions.read"],
        ),
    }
    for method_and_rule, (expected_name, expected_permissions) in expected.items():
        _, view = api_methods[method_and_rule]
        name, declaration = _authorization_declarations(view)[0]
        assert name == expected_name, f"{method_and_rule}: wrong policy type"
        if expected_permissions is not None:
            assert (
                _permission_values(" ".join(method_and_rule), declaration)
                == expected_permissions
            ), f"{method_and_rule}: wrong permissions"

    # Authentication stays in decorators after a route adopts a loader.
    checked_endpoints = set()
    for (method, path), (rule, view) in _api_methods(app).items():
        if rule.endpoint in checked_endpoints:
            continue
        checked_endpoints.add(rule.endpoint)
        declarations = _authorization_declarations(view)
        if not declarations or declarations[0][0] != "require_permission":
            continue
        if _keyword(declarations[0][1], "loader") is None:
            continue
        assert not _reads_authentication_header(view), (
            f"{method} {path}: loader-converted route reads an authentication "
            "header directly"
        )


def test_api_routes(app):
    check_api_routes(app)


def test_unknown_api_route_returns_not_found(app):
    response = app.test_client().get("/api/definitely-not-a-route")
    assert response.status_code == 404
