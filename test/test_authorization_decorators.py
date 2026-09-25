"""Isolated behavior tests for the authorization decorators."""

from flask import Flask, g, jsonify

from app.middleware.auth_middleware import (
    require_admin,
    require_auth,
    require_permission,
)


def test_decorators_authenticate_authorize_and_inject_resources(monkeypatch):
    app = Flask(__name__)
    principals = {
        "member": {
            "type": "user",
            "email": "m@example.com",
            "role": "member",
            "permissions": ["channel.read"],
        },
        "admin": {
            "type": "user",
            "email": "a@example.com",
            "role": "admin",
            "permissions": [],
        },
        "key": {"type": "api_key", "id": "key-1", "permissions": ["recording.create"]},
    }
    monkeypatch.setattr("app.utils.auth.authenticate_token", principals.get)

    @app.get("/auth")
    @require_auth
    def authenticated():
        return jsonify(g.principal)

    @app.get("/admin")
    @require_admin
    def administrator():
        return jsonify(ok=True)

    @app.get("/resource/<resource_id>")
    @require_permission(
        ["device", "recording.create"],
        loader=lambda principal, resource_id: {
            "id": resource_id,
            "type": principal["type"],
        },
        id_argument="resource_id",
        inject_as="resource",
    )
    def resource(resource_id, resource):
        return jsonify(resource)

    client = app.test_client()
    assert client.get("/auth").status_code == 401
    assert client.get("/auth", headers={"X-API-Key": "key"}).json["type"] == "api_key"
    assert (
        client.get("/admin", headers={"Authorization": "Bearer member"}).status_code
        == 403
    )
    assert (
        client.get("/admin", headers={"Authorization": "Bearer admin"}).status_code
        == 200
    )
    assert client.get("/resource/7", headers={"Authorization": "Bearer key"}).json == {
        "id": "7",
        "type": "api_key",
    }
    assert (
        client.get(
            "/resource/7", headers={"Authorization": "Bearer member"}
        ).status_code
        == 403
    )
    assert (
        client.get("/resource/7", headers={"Authorization": "Bearer admin"}).status_code
        == 200
    )
