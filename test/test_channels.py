"""Channel API validation tests using the production app, config, and repository."""

from datetime import datetime, timedelta, timezone

import pytest

@pytest.fixture
def channel_api(initialized_settings_manager):
    manager = initialized_settings_manager
    manager.save_group(
        {
            "name": "Default",
            "description": "Default user group",
            "is_default": True,
            "permissions": [],
        }
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
    token, _ = manager.issue_credential(
        "user",
        "admin@example.com",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        token="channel-test-token",
    )
    channel_id = manager.save_channel(
        {"name": "Channel 1", "mac": "AABBCCDDEEFF", "deleted": False}
    )

    from app import create_app

    client = create_app().test_client()
    headers = {"Authorization": f"Bearer {token}"}
    return client, manager, channel_id, headers


def test_channel_update_accepts_documented_threshold_range(channel_api):
    client, manager, channel_id, headers = channel_api

    response = client.put(
        f"/api/channel/{channel_id}", json={"threshold": "50"}, headers=headers
    )

    assert response.status_code == 200
    assert manager.get_channel(channel_id)["threshold"] == "50"


def test_channel_update_rejects_threshold_outside_documented_range(channel_api):
    client, manager, channel_id, headers = channel_api

    response = client.put(
        f"/api/channel/{channel_id}", json={"threshold": "101"}, headers=headers
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Threshold must be between 0 and 100."}
    assert manager.get_channel(channel_id)["threshold"] is None
