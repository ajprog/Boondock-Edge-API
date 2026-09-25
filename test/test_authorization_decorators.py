"""Authorization behavior tests through real application routes."""

from datetime import datetime, timedelta, timezone


def _issue_user(manager, email, role, groups, token):
    manager.save_user(
        email,
        {
            "name": email,
            "password": "unused",
            "role": role,
            "status": "Active",
            "groups": groups,
        },
    )
    value, _ = manager.issue_credential(
        "user",
        email,
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        token=token,
    )
    return {"Authorization": f"Bearer {value}"}


def test_real_routes_authenticate_and_authorize(initialized_settings_manager, recording_store):
    manager = initialized_settings_manager
    group_id = manager.save_group(
        {
            "name": "Readers",
            "description": "Channel readers",
            "is_default": True,
            "permissions": ["channel.read"],
        }
    )

    member_auth = _issue_user(
        manager,
        "member@example.com",
        "member",
        [group_id],
        "member-token",
    )
    admin_auth = _issue_user(
        manager,
        "admin@example.com",
        "admin",
        [],
        "admin-token",
    )

    manager.save_channel(
        {"name": "Channel 1", "mac": "AABBCCDDEEFF", "deleted": False}
    )

    from app import create_app

    client = create_app().test_client()

    assert client.get("/api/channels").status_code == 401
    assert client.get("/api/channels", headers=member_auth).status_code == 200
    assert client.get("/api/channels", headers=admin_auth).status_code == 200

    # A member with channel.read is authenticated but is not an administrator.
    assert client.post("/api/truncate_recordings", headers=member_auth).status_code == 403
