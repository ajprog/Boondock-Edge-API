"""Cloud device-event batching and bootstrap tests."""

import time
from flask import Flask
from app.routes import device_routes
from app.services import cloud_device_events


def test_cloud_events_use_one_batched_writer(monkeypatch):
    batches = []
    monkeypatch.setattr(cloud_device_events, "_write_cloud_event_batch", batches.append)

    cloud_device_events.persist_cloud_device_event_async(
        "AABBCCDDEEFF", 11, "ping", {"sequence": 1}
    )
    cloud_device_events.persist_cloud_device_event_async(
        "AABBCCDDEEFF", 11, "ping", {"sequence": 2}
    )

    deadline = time.monotonic() + 2
    while not batches and time.monotonic() < deadline:
        time.sleep(0.01)
    cloud_device_events.shutdown_cloud_event_writer()

    assert len(batches) == 1
    assert len(batches[0]) == 2
    assert batches[0][0][:4] == (
        "AABBCCDDEEFF",
        11,
        "ping",
        '{"sequence": 1}',
    )


def test_cloud_event_batch_is_persisted_and_listed(monkeypatch, tmp_path):
    database = tmp_path / "recordings.db"
    monkeypatch.setattr(
        cloud_device_events.Config,
        "get_recordings_db_path",
        staticmethod(lambda: database),
    )
    cloud_device_events._write_cloud_event_batch(
        [
            (
                "AABBCCDDEEFF",
                11,
                "ping",
                '{"sequence": 1}',
                "2026-09-07T10:00:00+00:00",
            ),
            (
                "AABBCCDDEEFF",
                9,
                "status",
                None,
                "2026-09-07T10:00:01+00:00",
            ),
        ]
    )

    events = cloud_device_events.list_cloud_events_for_mac("AABBCCDDEEFF")

    assert [event["event_type"] for event in events] == ["status", "ping"]
    assert events[1]["payload"] == {"sequence": 1}


def test_cloud_event_logs_missing_lookup_before_issuing_token(monkeypatch):
    app = Flask(__name__)
    token_lookups = []

    monkeypatch.setattr(
        device_routes, "get_channel_id_from_mac", lambda *args, **kwargs: 1
    )
    monkeypatch.setattr(device_routes, "is_mac_registered", lambda mac: True)
    monkeypatch.setattr(
        device_routes,
        "get_mac_for_token",
        lambda token, expected_mac=None: token_lookups.append((token, expected_mac)),
    )
    monkeypatch.setattr(
        device_routes, "generate_token", lambda mac: ("new-device-token", "later")
    )
    monkeypatch.setattr(
        device_routes, "persist_cloud_device_event_async", lambda *args: None
    )
    monkeypatch.setattr(device_routes, "touch_device_activity", lambda *args: None)
    monkeypatch.setattr(device_routes, "track_connection", lambda *args: None)
    monkeypatch.setattr(device_routes, "track_event", lambda *args: None)
    monkeypatch.setattr(device_routes, "get_stored_visual_state", lambda *args: None)
    monkeypatch.setattr(device_routes, "set_channel_visual_state", lambda *args: None)

    with app.test_request_context("/api/v1/events", method="POST"):
        response, status = device_routes._handle_cloud_style_device_event(
            {
                "mac_address": "E0:8C:FE:64:0C:14",
                "event_type": "ping",
            }
        )

    assert status == 200
    assert response.get_json()["token"] == "new-device-token"
    assert token_lookups == [(None, "E0:8C:FE:64:0C:14")]
