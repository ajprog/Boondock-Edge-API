import sqlite3
import threading
from pathlib import Path

import pytest
from app.routes import recordings_routes
from app.services import audio_handler as audio_handler_service


class RecordingQuerySpy:
    def __init__(self):
        self.window_arguments = None
        self.count_arguments = None

    def get_all_recordings(self):
        return [{"id": 7, "filename": "recordings/device/audio.wav"}]

    def get_recordings_inbox_window(
        self,
        limit=1000,
        since_timestamp=None,
        before_timestamp=None,
        before_id=None,
    ):
        self.window_arguments = {
            "limit": limit,
            "since_timestamp": since_timestamp,
            "before_timestamp": before_timestamp,
            "before_id": before_id,
        }
        return {"recordings": [{"id": 7}], "meta": {"returned": 1}}

    def get_recordings_inbox_count(
        self, since_timestamp=None, before_timestamp=None, before_id=None
    ):
        self.count_arguments = {
            "since_timestamp": since_timestamp,
            "before_timestamp": before_timestamp,
            "before_id": before_id,
        }
        return {"total": 1}


@pytest.fixture
def recordings_client():
    from app import create_app

    return create_app().test_client()


@pytest.fixture
def recording_store(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    recordings_dir = data_root / "recordings"
    recordings_dir.mkdir(parents=True)
    db_path = data_root / "recordings.db"

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE recordings (
                id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                filename TEXT,
                timestamp TEXT,
                transcription TEXT,
                status TEXT,
                is_duplicate INTEGER DEFAULT 0,
                duration REAL,
                filesize INTEGER
            )
            """)

    monkeypatch.setattr(recordings_routes, "DATA_ROOT", data_root)
    monkeypatch.setattr(recordings_routes, "RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(recordings_routes, "DB_PATH", db_path)

    return db_path, recordings_dir


@pytest.fixture
def real_audio_handler(recording_store, monkeypatch):
    db_path, _ = recording_store

    class AudioSettings:
        def get_all_settings(self):
            return {
                "global_hallucination": False,
                "global_show_duplicate_files": False,
            }

    monkeypatch.setattr(audio_handler_service, "_get_db_path", lambda: db_path)
    monkeypatch.setattr(audio_handler_service, "_settings_manager", AudioSettings())

    # These query methods only require the handler's database lock. Constructing
    # the full service would unnecessarily start transcription dependencies.
    handler = audio_handler_service.MultiChannelAudioHandler.__new__(
        audio_handler_service.MultiChannelAudioHandler
    )
    handler.db_lock = threading.Lock()
    return handler


def _insert_recording(db_path, recording_id, filename):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO recordings (id, filename) VALUES (?, ?)",
            (recording_id, filename),
        )


def _recording_exists(db_path, recording_id):
    with sqlite3.connect(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
            is not None
        )


def _insert_inbox_recording(
    db_path,
    recording_id,
    timestamp,
    *,
    is_duplicate=0,
    transcription="transcript",
):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO recordings (
                id, channel_id, filename, timestamp, transcription, status,
                is_duplicate, duration, filesize
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                1,
                f"recordings/{recording_id}.wav",
                timestamp,
                transcription,
                "new",
                is_duplicate,
                1.5,
                100,
            ),
        )


def test_resolve_recording_path_accepts_files_only_within_recordings_directory(
    recording_store,
):
    _, recordings_dir = recording_store

    assert recordings_routes._resolve_recording_path("recordings/device/audio.wav") == (
        recordings_dir / "device" / "audio.wav"
    )
    assert recordings_routes._resolve_recording_path("../outside.wav") is None


def test_serve_audio_returns_existing_file(
    recording_store, recordings_client, admin_auth
):
    _, recordings_dir = recording_store
    audio_file = recordings_dir / "device" / "audio.wav"
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"audio contents")
    response = recordings_client.get(
        "/api/recordings/device/audio.wav", headers=admin_auth
    )

    assert response.status_code == 200
    assert response.get_data() == b"audio contents"


def test_serve_audio_returns_not_found_for_missing_file(
    recording_store, recordings_client, admin_auth
):
    response = recordings_client.get("/api/recordings/missing.wav", headers=admin_auth)

    assert response.status_code == 404


def test_recording_queries_forward_filters_to_audio_handler(
    monkeypatch, recordings_client, admin_auth
):
    handler = RecordingQuerySpy()
    monkeypatch.setattr(recordings_routes, "get_audio_handler", lambda: handler)
    recordings_response = recordings_client.get("/api/recordings", headers=admin_auth)
    window_response = recordings_client.get(
        "/api/recordings/inbox?limit=25&since_timestamp=20260818_120000"
        "&before_timestamp=20260818_130000&before_id=9",
        headers=admin_auth,
    )
    count_response = recordings_client.get(
        "/api/recordings/inbox/count?since_timestamp=20260818_120000"
        "&before_timestamp=20260818_130000&before_id=9",
        headers=admin_auth,
    )

    assert recordings_response.get_json() == [
        {"id": 7, "filename": "recordings/device/audio.wav"}
    ]
    assert window_response.get_json() == {
        "recordings": [{"id": 7}],
        "meta": {"returned": 1},
    }
    assert handler.window_arguments == {
        "limit": 25,
        "since_timestamp": "20260818_120000",
        "before_timestamp": "20260818_130000",
        "before_id": 9,
    }
    assert count_response.get_json() == {"total": 1}
    assert handler.count_arguments == {
        "since_timestamp": "20260818_120000",
        "before_timestamp": "20260818_130000",
        "before_id": 9,
    }


def test_recording_queries_return_empty_results_without_audio_handler(
    monkeypatch, recordings_client, admin_auth
):
    monkeypatch.setattr(recordings_routes, "get_audio_handler", lambda: None)
    recordings_response = recordings_client.get("/api/recordings", headers=admin_auth)
    window_response = recordings_client.get(
        "/api/recordings/inbox?limit=25", headers=admin_auth
    )
    count_response = recordings_client.get(
        "/api/recordings/inbox/count", headers=admin_auth
    )

    assert recordings_response.get_json() == []
    assert window_response.get_json() == {
        "recordings": [],
        "meta": {
            "limit": 25,
            "returned": 0,
            "has_more": False,
            "next_before_timestamp": None,
            "next_before_id": None,
        },
    }
    assert count_response.get_json() == {"total": 0}


def test_real_audio_handler_applies_keyset_paging_and_count_filters(
    recording_store, real_audio_handler
):
    db_path, _ = recording_store
    _insert_inbox_recording(db_path, 1, "20260818_120000")
    _insert_inbox_recording(db_path, 2, "20260818_130000")
    _insert_inbox_recording(db_path, 3, "20260818_130000")
    _insert_inbox_recording(db_path, 4, "20260818_140000", is_duplicate=1)

    first_page = real_audio_handler.get_recordings_inbox_window(limit=2)
    second_page = real_audio_handler.get_recordings_inbox_window(
        limit=2,
        before_timestamp="20260818_130000",
        before_id=2,
    )
    remaining_count = real_audio_handler.get_recordings_inbox_count(
        before_timestamp="20260818_130000",
        before_id=2,
    )

    assert [recording["id"] for recording in first_page["recordings"]] == [3, 2]
    assert first_page["meta"] == {
        "limit": 2,
        "returned": 2,
        "has_more": True,
        "next_before_timestamp": "20260818_130000",
        "next_before_id": 2,
    }
    assert [recording["id"] for recording in second_page["recordings"]] == [1]
    assert remaining_count == {"total": 1}


def test_recordings_inbox_route_integrates_with_real_audio_handler(
    recording_store, real_audio_handler, monkeypatch, recordings_client, admin_auth
):
    db_path, _ = recording_store
    _insert_inbox_recording(db_path, 1, "20260818_120000")
    _insert_inbox_recording(db_path, 2, "20260818_130000")
    monkeypatch.setattr(
        recordings_routes, "get_audio_handler", lambda: real_audio_handler
    )
    response = recordings_client.get(
        "/api/recordings/inbox?limit=1&since_timestamp=20260818_120000",
        headers=admin_auth,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [recording["id"] for recording in body["recordings"]] == [2]
    assert body["meta"] == {
        "limit": 1,
        "returned": 1,
        "has_more": True,
        "next_before_timestamp": "20260818_130000",
        "next_before_id": 2,
    }


def test_delete_recording_removes_database_row_and_audio_file(
    recording_store, recordings_client, admin_auth
):
    db_path, recordings_dir = recording_store
    audio_file = recordings_dir / "device" / "audio.wav"
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"audio")
    _insert_recording(db_path, 1, "recordings/device/audio.wav")

    response = recordings_client.delete("/api/recordings/1", headers=admin_auth)

    assert response.status_code == 200
    assert response.get_json() == {
        "message": "Recording deleted successfully",
        "file_deleted": True,
    }
    assert not _recording_exists(db_path, 1)
    assert not audio_file.exists()


def test_delete_recording_succeeds_when_audio_file_is_already_missing(
    recording_store, recordings_client, admin_auth
):
    db_path, _ = recording_store
    _insert_recording(db_path, 2, "recordings/device/missing.wav")

    response = recordings_client.delete("/api/recordings/2", headers=admin_auth)

    assert response.status_code == 200
    assert response.get_json() == {
        "message": "Recording deleted successfully",
        "file_deleted": False,
    }
    assert not _recording_exists(db_path, 2)


def test_delete_recording_rejects_path_outside_recordings_directory(
    recording_store, recordings_client, admin_auth
):
    db_path, _ = recording_store
    _insert_recording(db_path, 3, "../outside.wav")

    response = recordings_client.delete("/api/recordings/3", headers=admin_auth)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid recording file path"}
    assert _recording_exists(db_path, 3)


def test_delete_recording_reports_file_deletion_failure(
    recording_store, monkeypatch, recordings_client, admin_auth
):
    db_path, recordings_dir = recording_store
    audio_file = recordings_dir / "undeletable.wav"
    audio_file.write_bytes(b"audio")
    _insert_recording(db_path, 4, "recordings/undeletable.wav")

    original_unlink = Path.unlink

    def fail_for_audio_file(path, *args, **kwargs):
        if path == audio_file:
            raise OSError("permission denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_for_audio_file)

    response = recordings_client.delete("/api/recordings/4", headers=admin_auth)

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Recording deleted from database, but audio file deletion failed",
        "recording_id": 4,
    }
    assert not _recording_exists(db_path, 4)
    assert audio_file.exists()
