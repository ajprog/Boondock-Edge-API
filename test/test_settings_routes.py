"""Contract tests for administrator category settings endpoints."""

import pytest


@pytest.fixture
def client():
    from app import create_app

    application = create_app()
    application.config['TESTING'] = True
    return application.test_client()


def test_settings_are_split_into_admin_only_categories(
    client, initialized_settings_manager, admin_auth
):
    manager = initialized_settings_manager
    manager.set_all_settings({
        'target_language': 'english',
        'model': 'tiny.en',
        'method': 'local',
        'transcription_queue_enabled': True,
        'transcription_api_key': 'transcription-secret',
        'ssid': 'boondockedge',
        'host_password': 'wifi-secret',
        'host_ip': '10.42.0.1',
        'host_port': 4000,
        's3_enabled': False,
        's3_endpoint_url': '',
        's3_access_key': 'access-secret',
        's3_secret_key': 's3-secret',
        's3_region': 'us-east-1',
        's3_bucket_name': '',
        'samba_enabled': False,
        'samba_share_path': '',
        'samba_username': '',
        'samba_password': 'samba-secret',
    })

    assert client.get('/api/settings/transcription').status_code == 401

    transcription = client.get(
        '/api/settings/transcription', headers=admin_auth
    ).get_json()
    assert transcription == {
        'target_language': 'english',
        'model': 'tiny.en',
        'method': 'local',
        'queue_enabled': True,
        'api_key_configured': True,
    }

    wifi = client.get('/api/settings/wifi', headers=admin_auth).get_json()
    assert wifi == {
        'ssid': 'boondockedge',
        'password_configured': True,
        'host_ip': '10.42.0.1',
        'host_port': 4000,
    }

    backup = client.get('/api/settings/backup', headers=admin_auth).get_json()
    assert backup == {
        's3_enabled': False,
        's3_endpoint_url': '',
        's3_access_key_configured': True,
        's3_secret_key_configured': True,
        's3_region': 'us-east-1',
        's3_bucket_name': '',
        'samba_enabled': False,
        'samba_share_path': '',
        'samba_username': '',
        'samba_password_configured': True,
    }
    assert not {'api_key', 'password', 's3_access_key', 's3_secret_key', 'samba_password'} & (
        set(transcription) | set(wifi) | set(backup)
    )

    registered = {
        (method, rule.rule)
        for rule in client.application.url_map.iter_rules()
        for method in rule.methods - {'HEAD', 'OPTIONS'}
    }
    assert ('GET', '/api/settings') not in registered
    assert ('PUT', '/api/settings') not in registered
    assert ('GET', '/api/maintenance/settings') not in registered
    assert ('PUT', '/api/maintenance/settings') not in registered


def test_secret_patch_omits_preserves_and_whitespace_clears(
    client, initialized_settings_manager, admin_auth, monkeypatch
):
    manager = initialized_settings_manager
    manager.set_setting('transcription_api_key', 'keep-me')
    monkeypatch.setattr(
        'app.services.audio_handler.reload_transcription_settings', lambda: None
    )
    queue = type('Queue', (), {
        'running': True,
        'stop_queue': lambda self: setattr(self, 'running', False),
        'start': lambda self: setattr(self, 'running', True),
    })()
    monkeypatch.setattr('app.services.audio_handler.get_audio_handler', lambda: queue)

    response = client.patch(
        '/api/settings/transcription', json={'model': 'base.en'}, headers=admin_auth
    )
    assert response.status_code == 200
    assert manager.get_setting('transcription_api_key') == 'keep-me'
    assert response.get_json()['api_key_configured'] is True

    response = client.patch(
        '/api/settings/transcription', json={'api_key': '   '}, headers=admin_auth
    )
    assert response.status_code == 200
    assert manager.get_setting('transcription_api_key') == ''
    assert response.get_json()['api_key_configured'] is False

    response = client.patch(
        '/api/settings/transcription',
        json={'queue_enabled': False},
        headers=admin_auth,
    )
    assert response.status_code == 200
    assert manager.get_setting('transcription_queue_enabled') is False
    assert response.get_json()['queue_enabled'] is False
    assert queue.running is False

    manager.set_setting('host_password', 'keep-wifi')
    client.patch('/api/settings/wifi', json={'ssid': 'new-name'}, headers=admin_auth)
    assert manager.get_setting('host_password') == 'keep-wifi'
    response = client.patch(
        '/api/settings/wifi', json={'password': '\t'}, headers=admin_auth
    )
    assert response.status_code == 200
    assert manager.get_setting('host_password') == ''
    assert response.get_json()['password_configured'] is False


def test_category_patches_validate_and_return_complete_category(
    client, initialized_settings_manager, admin_auth, monkeypatch
):
    restarted = []
    monkeypatch.setattr(
        'app.services.maintenance_scheduler.restart_scheduler',
        lambda: restarted.append(True),
    )

    response = client.patch(
        '/api/settings/recorders',
        json={'edge_recorders_enabled': False},
        headers=admin_auth,
    )
    assert response.status_code == 200
    assert response.get_json() == {
        'uniden_scanners_enabled': False,
        'edge_recorders_enabled': False,
    }

    response = client.patch(
        '/api/settings/maintenance',
        json={'scheduled_time': '04:30'},
        headers=admin_auth,
    )
    assert response.status_code == 200
    assert response.get_json() == {
        'scheduled_time': '04:30',
        'enabled_tasks': ['data_backup', 'logs_cleanup', 'health_checks'],
    }
    assert restarted == [True]

    assert client.patch(
        '/api/settings/maintenance',
        json={'scheduled_time': '4:30'},
        headers=admin_auth,
    ).status_code == 400
    assert client.patch(
        '/api/settings/recorders',
        json={'edge_recorders_enabled': 'false'},
        headers=admin_auth,
    ).status_code == 400
    assert client.patch(
        '/api/settings/wifi',
        json={'password_configured': False},
        headers=admin_auth,
    ).status_code == 400


def test_audio_processing_replaces_complete_pattern_collection(
    client, initialized_settings_manager, admin_auth
):
    created = client.patch(
        '/api/settings/audio-processing',
        json={
            'hallucination_patterns': [
                {'pattern': 'engine fire'},
                {
                    'pattern': r'car\\s+stopped',
                    'match_type': 'regex',
                    'case_sensitive': True,
                },
            ]
        },
        headers=admin_auth,
    )
    assert created.status_code == 200
    patterns = created.get_json()['hallucination_patterns']
    assert patterns[0] == {
        'id': patterns[0]['id'],
        'pattern': 'engine fire',
        'match_type': 'literal',
        'case_sensitive': False,
    }
    assert patterns[1]['case_sensitive'] is True

    replaced = client.patch(
        '/api/settings/audio-processing',
        json={
            'hallucination_patterns': [{**patterns[1], 'case_sensitive': False}]
        },
        headers=admin_auth,
    )
    assert replaced.status_code == 200
    assert replaced.get_json()['hallucination_patterns'] == [
        {**patterns[1], 'case_sensitive': False}
    ]

    invalid = client.patch(
        '/api/settings/audio-processing',
        json={
            'hallucination_patterns': [
                {'pattern': '[', 'match_type': 'regex'}
            ]
        },
        headers=admin_auth,
    )
    assert invalid.status_code == 400


def test_dedicated_hallucination_crud_uses_explicit_pattern_shape(
    client, initialized_settings_manager, admin_auth
):
    response = client.post(
        '/api/hallucinations',
        json={'pattern': 'radio check'},
        headers=admin_auth,
    )
    assert response.status_code == 201
    pattern = response.get_json()
    assert pattern == {
        'id': pattern['id'],
        'pattern': 'radio check',
        'match_type': 'literal',
        'case_sensitive': False,
    }
    assert client.get('/api/hallucinations', headers=admin_auth).get_json() == [pattern]
    assert client.delete(
        f"/api/hallucinations/{pattern['id']}", headers=admin_auth
    ).status_code == 204
    assert client.delete(
        f"/api/hallucinations/{pattern['id']}", headers=admin_auth
    ).status_code == 404
