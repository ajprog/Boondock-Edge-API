"""Cloud device-event tests through the real service and HTTP endpoint."""

import time

from app.services import cloud_device_events


def _wait_for_event(mac, event_type, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = cloud_device_events.list_cloud_events_for_mac(mac)
        for event in events:
            if event["event_type"] == event_type:
                return event
        time.sleep(0.02)
    return None


def test_cloud_event_is_persisted_and_listed(recording_store):
    cloud_device_events.persist_cloud_device_event_async(
        "AABBCCDDEEFF", 11, "ping", {"sequence": 1}
    )

    event = _wait_for_event("AABBCCDDEEFF", "ping")
    cloud_device_events.shutdown_cloud_event_writer()

    assert event is not None
    assert event["event_type_id"] == 11
    assert event["payload"] == {"sequence": 1}


def test_v1_events_bootstraps_device_and_returns_token(
    initialized_settings_manager, recording_store
):
    manager = initialized_settings_manager
    manager.save_group(
        {
            "name": "Default",
            "description": "Default device group",
            "is_default": True,
            "permissions": [],
        }
    )

    from app import create_app

    client = create_app().test_client()
    response = client.post(
        "/api/v1/events",
        json={
            "mac_address": "E0:8C:FE:64:0C:14",
            "event_type": "ping",
            "event_data": {"sequence": 1},
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "Event received"
    assert body.get("token")

    event = _wait_for_event("E08CFE640C14", "ping")
    cloud_device_events.shutdown_cloud_event_writer()
    assert event is not None
    assert event["payload"] == {"sequence": 1}
