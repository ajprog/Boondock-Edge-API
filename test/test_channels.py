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

    response = client.patch(
        f"/api/channels/{channel_id}", json={"threshold": "50"}, headers=headers
    )

    assert response.status_code == 200
    assert manager.get_channel(channel_id)["threshold"] == "50"


def test_channel_update_rejects_threshold_outside_documented_range(channel_api):
    client, manager, channel_id, headers = channel_api

    response = client.patch(
        f"/api/channels/{channel_id}", json={"threshold": "101"}, headers=headers
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Threshold must be between 0 and 100."}
    assert manager.get_channel(channel_id)["threshold"] is None


def test_channel_read_synchronizes_connected_recorder(channel_api, monkeypatch):
    client, manager, channel_id, headers = channel_api
    from app.routes import channels_routes

    recorder = {"port": "/dev/ttyUSB0"}
    monkeypatch.setattr(channels_routes, "find_connected_recorder", lambda _mac: recorder)

    def import_config(channel, connected):
        assert connected is recorder
        channel.update(device_hostname="edge-1.local", device_ip="192.168.4.12")
        manager.save_channel(channel)
        return channel

    monkeypatch.setattr(channels_routes, "import_recorder_configuration", import_config)

    response = client.get(f"/api/channels/{channel_id}", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["usb_connected"] is True
    assert body["device_dashboard_url"] == "http://edge-1.local/"
    assert body["device_sync"] == {"attempted": True, "success": True, "error": None}
    assert manager.get_channel(channel_id)["device_ip"] == "192.168.4.12"


def test_channel_patch_keeps_database_update_when_device_sync_fails(
    channel_api, monkeypatch
):
    client, manager, channel_id, headers = channel_api
    from app.routes import channels_routes

    monkeypatch.setattr(
        channels_routes,
        "find_connected_recorder",
        lambda _mac: {"port": "/dev/ttyUSB0"},
    )

    def fail_sync(_channel, _recorder):
        raise TimeoutError("Recorder did not confirm configuration")

    monkeypatch.setattr(channels_routes, "export_channel_configuration", fail_sync)

    response = client.patch(
        f"/api/channels/{channel_id}", json={"threshold": "55"}, headers=headers
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["channel"]["threshold"] == "55"
    assert body["device_sync"] == {
        "attempted": True,
        "success": False,
        "error": "Recorder did not confirm configuration",
    }
    assert manager.get_channel(channel_id)["threshold"] == "55"


def test_legacy_singular_channel_update_route_is_removed(channel_api):
    client, _manager, channel_id, headers = channel_api

    response = client.put(
        f"/api/channel/{channel_id}", json={"threshold": "50"}, headers=headers
    )

    assert response.status_code in {404, 405}
