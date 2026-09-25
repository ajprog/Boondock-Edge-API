"""Database-backed authentication and principal materialization tests."""

from datetime import datetime, timedelta, timezone

from app.services.api_key_manager import APIKeyManager
from app.utils import auth


def test_principal_retrieval_and_unified_authentication(
    monkeypatch, initialized_settings_manager
):
    manager = initialized_settings_manager
    group_id = manager.save_group(
        {
            "name": "Members",
            "description": "Members",
            "is_default": True,
            "permissions": ["channel.read"],
        }
    )
    manager.save_user(
        "member@example.com",
        {
            "name": "Member",
            "password": "unused",
            "role": "member",
            "status": "Active",
            "groups": [group_id],
        },
    )
    manager.save_user(
        "admin@example.com",
        {
            "name": "Admin",
            "password": "unused",
            "role": "admin",
            "status": "Active",
            "groups": [],
        },
    )
    channel_id = manager.save_channel(
        {"name": "Radio", "mac": "AABBCCDDEEFF", "deleted": False}
    )

    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    manager.issue_credential(
        "user", "member@example.com", expires, token="member-token"
    )
    manager.issue_credential("user", "admin@example.com", expires, token="admin-token")
    api_keys = APIKeyManager(db_path=manager.db_path)
    api_keys.settings = manager
    _, api_token = api_keys.create_key(
        "Reader",
        scopes=["recording.create"],
        created_by="admin@example.com",
        expires_at=expires,
    )
    manager.issue_credential("device", str(channel_id), expires, token="device-token")
    monkeypatch.setattr(auth, "_settings_manager", manager)

    member = auth.authenticate_token("member-token")
    assert member["type"] == "user"
    assert member["permissions"] == ["channel.read"]
    assert member["owner_ids"] == ["user:member@example.com", f"group:{group_id}"]
    assert "password" not in member

    assert auth.authenticate_token("admin-token")["owner_ids"] is None
    api_key = auth.authenticate_token(api_token)
    assert api_key["permissions"] == ["recording.create"]
    assert api_key["owner_ids"] is None
    device = auth.authenticate_token("device-token")
    assert device["permissions"] == ["device"]
    assert device["mac"] == "AABBCCDDEEFF"
    assert auth.authenticate_token("missing") is None
