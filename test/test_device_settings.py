"""Device reports and reads use channel storage and firmware field names."""

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def device_settings_api(admin_auth, initialized_settings_manager):
    manager = initialized_settings_manager
    manager.save_group({'name': 'Default', 'is_default': True, 'permissions': []})
    channel_id = manager.save_channel({
        'name': 'Recorder', 'mac': 'AABBCCDDEEFF', 'threshold': '10',
        'silence': '1500', 'deleted': False,
    })
    other_id = manager.save_channel({'name': 'Other', 'mac': '112233445566'})
    token, _ = manager.issue_credential(
        'device', str(channel_id),
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    from app import create_app

    return (create_app().test_client(), manager, channel_id, other_id,
            {'Authorization': f'Bearer {token}'}, admin_auth)


@pytest.mark.parametrize('base', ['/api/v1/settings', '/api/device/settings',
                                 '/api/v1/settings/', '/api/device/settings/'])
def test_report_updates_channel_and_get_reads_latest_channel(device_settings_api, monkeypatch, base):
    client, manager, channel_id, _, headers, admin_headers = device_settings_api
    from app.services import channel_recorder_sync as sync

    def unexpected_sync(*_args, **_kwargs):
        raise AssertionError('A device report must not send settings back to the device')

    monkeypatch.setattr(sync, 'send_command_and_wait_response', unexpected_sync)
    response = client.post(base, headers=headers, data={
        'mac_address': 'aa:bb:cc:dd:ee:ff',
        'settings': json.dumps({
            'a': {'ath': 35, 'mrm': 1000, 'xrm': 25000, 'cg': 3},
            'ho': 'recorder.local', 'cip': {'ip': '192.168.1.10'},
            'fw': 'untrusted', 'w': [{'pw': 'ignored'}],
        }),
    })
    assert response.status_code == 200
    assert response.json['message'] == 'Settings saved successfully'
    channel = manager.get_channel(channel_id)
    assert channel['threshold'] == '35'
    assert channel['silence'] == '1500'
    assert channel['name'] == 'Recorder'
    assert channel['device_hostname'] == 'recorder.local'

    # A later dashboard edit is immediately visible without a device upload.
    channel['threshold'] = '42'
    manager.save_channel(channel)
    response = client.get(f'{base.rstrip("/")}/AABBCCDDEEFF' + ('/' if base.endswith('/') else ''), headers=admin_headers)
    assert response.status_code == 200
    assert response.mimetype == 'application/json'
    assert response.json == {
        'a': {'ath': 42, 'stm': 1500, 'mrm': 1000, 'xrm': 25000, 'cg': 3},
        'ho': 'recorder.local', 'cip': {'ip': '192.168.1.10'},
    }


@pytest.mark.parametrize('settings', [[], None, 'text', {'a': []},
                                        {'cip': 'bad'}, {'a': {'ath': 'bad'}},
                                        {'a': {'ath': float('nan')}}, {'ho': []}])
def test_invalid_report_does_not_change_channel(device_settings_api, settings):
    client, manager, channel_id, _, headers, _ = device_settings_api
    before = manager.get_channel(channel_id)
    response = client.post('/api/v1/settings', headers=headers, data={
        'mac_address': 'AABBCCDDEEFF', 'settings': json.dumps(settings),
    })
    assert response.status_code == 400
    assert manager.get_channel(channel_id) == before


@pytest.mark.parametrize('base', ['/api/v1/settings', '/api/device/settings'])
def test_device_cannot_report_or_read_another_mac(device_settings_api, base):
    client, manager, _, other_id, headers, _ = device_settings_api
    before = manager.get_channel(other_id)
    response = client.post(base, headers=headers, data={
        'mac_address': '112233445566', 'settings': '{"a":{"ath":99}}',
    })
    assert response.status_code == 403
    assert client.get(f'{base}/112233445566', headers=headers).status_code == 403
    assert manager.get_channel(other_id) == before


def test_report_database_failure_is_not_acknowledged(device_settings_api, monkeypatch):
    client, manager, _, _, headers, _ = device_settings_api
    monkeypatch.setattr(manager, 'save_channel', lambda _channel: -1)
    response = client.post('/api/v1/settings', headers=headers, data={
        'mac_address': 'AABBCCDDEEFF', 'settings': '{"a":{"ath":35}}',
    })
    assert response.status_code == 500


@pytest.fixture
def event_settings_api(device_settings_api, monkeypatch):
    from app.routes import device_routes

    monkeypatch.setattr(device_routes, 'persist_cloud_device_event_async', lambda *_args: None)
    return device_settings_api


@pytest.mark.parametrize('event_type', ['ping', 'heartbeat', 'settings_updated', 'record_begin'])
def test_event_delivers_pending_settings_once(event_settings_api, event_type):
    client, manager, channel_id, _, headers, _ = event_settings_api
    event = {'mac_address': 'AA:BB:CC:DD:EE:FF', 'event_type': event_type}
    assert manager.get_channel(channel_id)['synced'] is False

    response = client.post('/api/v1/events', headers=headers, json=event)
    assert response.status_code == 200
    assert response.mimetype == 'application/json'
    assert response.json['message'] == 'Event received'
    assert response.json['timestamp']
    assert 'token' not in response.json  # Existing valid-token behavior.
    assert response.json['settings'] == {'a': {'ath': 10, 'stm': 1500}}
    assert manager.get_channel(channel_id)['synced'] is True
    response = client.post('/api/v1/events', headers=headers, json=event)
    assert response.status_code == 200
    assert 'settings' not in response.json


def test_event_without_matching_identity_leaves_settings_pending(event_settings_api):
    client, manager, channel_id, other_id, headers, _ = event_settings_api
    event = {'mac_address': 'AABBCCDDEEFF', 'event_type': 'ping'}
    response = client.post('/api/v1/events', json=event)
    assert response.status_code == 200
    assert response.json['token']
    assert 'settings' not in response.json
    assert manager.get_channel(channel_id)['synced'] is False

    response = client.post('/api/v1/events', headers=headers, json={**event, 'mac_address': '112233445566'})
    assert response.status_code == 200
    assert 'settings' not in response.json
    assert manager.get_channel(other_id)['synced'] is False


@pytest.mark.parametrize('hostname', ['x' * 4097, '\u00e9' * 700])
def test_oversized_settings_remain_pending(event_settings_api, hostname):
    client, manager, channel_id, _, headers, _ = event_settings_api
    channel = manager.get_channel(channel_id)
    channel['device_hostname'] = hostname
    manager.save_channel(channel)
    response = client.post('/api/v1/events', headers=headers, json={
        'mac_address': 'AABBCCDDEEFF', 'event_type': 'ping',
    })
    assert response.status_code == 200
    assert response.json['message'] == 'Event received'
    assert 'settings' not in response.json
    assert manager.get_channel(channel_id)['synced'] is False


@pytest.mark.parametrize('synced', [False, True])
def test_device_reports_preserve_sync_flag(event_settings_api, synced):
    client, manager, channel_id, _, headers, _ = event_settings_api
    channel = manager.get_channel(channel_id)
    channel['synced'] = synced
    manager.save_channel(channel)
    response = client.post('/api/v1/settings', headers=headers, data={
        'mac_address': 'AABBCCDDEEFF', 'settings': '{"a":{"ath":35}}',
    })
    assert response.status_code == 200
    assert manager.get_channel(channel_id)['synced'] is synced


def test_parallel_events_claim_only_one_delivery(event_settings_api):
    from concurrent.futures import ThreadPoolExecutor

    _, manager, channel_id, _, _, _ = event_settings_api
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(manager.take_unsynced_device_settings, ['AABBCCDDEEFF'] * 2))
    assert sum(result is not None for result in results) == 1
    assert manager.get_channel(channel_id)['synced'] is True
