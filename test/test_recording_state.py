"""Recording hallucination classification and queue state tests."""

import json
import queue
import sqlite3
import threading

from app.services import audio_handler
from app.services.recording_state import is_hallucination, update_transcription
from app.services.transcription_service import TranscriptionAuthenticationError


def test_successful_transcription_classification_rules():
    patterns = [
        {
            'pattern': 'engine fire',
            'match_type': 'literal',
            'case_sensitive': False,
        },
        {
            'pattern': r'Car\s+Stopped',
            'match_type': 'regex',
            'case_sensitive': True,
        },
    ]

    assert is_hallucination('', patterns) is True
    assert is_hallucination('   ', patterns) is True
    assert is_hallucination('Report: ENGINE FIRE now', patterns) is True
    assert is_hallucination('Car   Stopped', patterns) is True
    assert is_hallucination('car stopped', patterns) is False
    assert is_hallucination('ordinary radio traffic', patterns) is False


def test_literal_pattern_does_not_interpret_regex_characters():
    patterns = [{
        'pattern': 'car.*stopped',
        'match_type': 'literal',
        'case_sensitive': False,
    }]

    assert is_hallucination('CAR.*STOPPED', patterns) is True
    assert is_hallucination('car suddenly stopped', patterns) is False


def test_successful_transcription_updates_classification_and_timestamp(
    initialized_settings_manager, recording_store
):
    database, _ = recording_store
    initialized_settings_manager.save_hallucination({'pattern': 'engine fire'})
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO recordings (
                id, filename, transcription, is_hallucination, updated_at
            ) VALUES (1, 'recordings/one.wav', 'old', FALSE, 500)"""
        )
        assert update_transcription(connection, 1, 'ENGINE FIRE reported')
        first = connection.execute(
            """SELECT transcription, is_hallucination, updated_at
               FROM recordings WHERE id = 1"""
        ).fetchone()
        assert first[:2] == ('ENGINE FIRE reported', 1)
        assert first[2] > 500

        assert update_transcription(connection, 1, '')
        row = connection.execute(
            """SELECT transcription, is_hallucination, updated_at
               FROM recordings WHERE id = 1"""
        ).fetchone()
        assert row[:2] == ('', 1)
        assert row[2] > first[2]


def test_unauthorized_cloud_request_stops_queue_and_preserves_task(
    monkeypatch, tmp_path
):
    database = tmp_path / 'recordings.db'
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE recordings (
                id INTEGER PRIMARY KEY,
                filename TEXT,
                status TEXT,
                updated_at INTEGER NOT NULL DEFAULT 0
            )"""
        )
        connection.execute(
            "INSERT INTO recordings VALUES (1, 'recordings/one.wav', 'queued', 0)"
        )

    recordings = tmp_path / 'recordings'
    recordings.mkdir()
    (recordings / 'one.wav').write_bytes(b'audio')
    monkeypatch.setattr(audio_handler, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(audio_handler, '_get_db_path', lambda: database)
    monkeypatch.setattr(audio_handler, 'CURRENT_QUEUE_JSON', tmp_path / 'current.json')
    monkeypatch.setattr(audio_handler, 'QUEUE_HISTORY_JSON', tmp_path / 'history.json')

    saved_settings = {}
    fake_settings = type('Settings', (), {
        'get_channel': lambda self, channel_id: {'auto_transcribe': True},
        'set_setting': lambda self, key, value: saved_settings.update({key: value}) or True,
    })()
    monkeypatch.setattr(audio_handler, '_settings_manager', fake_settings)

    class UnauthorizedService:
        model_name = 'unused'

        def transcribe_audio(self, *args, **kwargs):
            raise TranscriptionAuthenticationError(
                'Transcription API authentication failed'
            )

    handler = audio_handler.MultiChannelAudioHandler.__new__(
        audio_handler.MultiChannelAudioHandler
    )
    handler.running = True
    handler.threads = []
    handler.db_lock = threading.Lock()
    handler.upload_queue = queue.Queue()
    handler.upload_tasks = {}
    handler.upload_processor_thread = None
    handler.upload_processor_lock = threading.Lock()
    handler._pending_filenames_in_queue = set()
    handler.channels = {}
    handler.processing_timeout_seconds = 120
    handler.long_stuck_check_interval_seconds = 120
    handler.transcription_service = UnauthorizedService()
    handler.transcribe_method = 'openai'
    handler.queue_error = None

    task = audio_handler.UploadTask(
        'recordings/one.wav', 1, '20261002_120000'
    )
    handler.upload_tasks[task.file_path] = task
    handler.upload_queue.put(task)
    handler._pending_filenames_in_queue.add(task.file_path)

    handler.process_upload_queue()

    assert handler.running is False
    assert task.status == 'pending'
    assert handler.upload_tasks[task.file_path] is task
    assert handler.queue_error == {
        'type': 'authentication',
        'message': 'Transcription API authentication failed',
    }
    assert saved_settings['transcription_queue_enabled'] is False
    assert not (tmp_path / 'history.json').exists()
    current = json.loads((tmp_path / 'current.json').read_text())
    assert current['tasks'][task.file_path]['status'] == 'pending'
    assert current['queue_error'] == handler.queue_error
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT status FROM recordings WHERE id = 1'
        ).fetchone() == ('pending',)
