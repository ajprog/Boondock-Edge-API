import sqlite3
from pathlib import Path

from config import DATA_ROOT
from app.routes import recordings_routes


def _insert_recording(
    db_path,
    recording_id,
    filename,
    *,
    channel_id=None,
    timestamp=None,
    transcription="transcript",
    is_duplicate=0,
):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO recordings (
                id, channel_id, filename, timestamp, transcription,
                status, is_duplicate, duration, filesize
            ) VALUES (?, ?, ?, ?, ?, 'new', ?, 1.5, 100)
            """,
            (
                recording_id,
                channel_id,
                filename,
                timestamp,
                transcription,
                is_duplicate,
            ),
        )


def _recording_exists(db_path, recording_id):
    with sqlite3.connect(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
            is not None
        )


def test_resolve_recording_path_accepts_files_only_within_recordings_directory(
    recording_store,
):
    _, recordings_dir = recording_store

    assert recordings_routes._resolve_recording_path(
        "recordings/device/audio.wav"
    ) == recordings_dir / "device" / "audio.wav"
    assert recordings_routes._resolve_recording_path("../outside.wav") is None


def test_recordings_route_reads_real_recordings_database(
    recording_store, admin_auth
):
    db_path, _ = recording_store
    _insert_recording(
        db_path,
        7,
        "recordings/device/audio.wav",
        timestamp="20260818_120000",
    )

    from app import create_app

    response = create_app().test_client().get("/api/recordings", headers=admin_auth)

    assert response.status_code == 200
    body = response.get_json()
    assert [recording["id"] for recording in body] == [7]
    assert body[0]["filename"] == "recordings/device/audio.wav"


def test_recordings_inbox_applies_real_query_filters(
    recording_store, admin_auth
):
    db_path, _ = recording_store
    _insert_recording(db_path, 1, "recordings/1.wav", timestamp="20260818_120000")
    _insert_recording(db_path, 2, "recordings/2.wav", timestamp="20260818_130000")
    _insert_recording(db_path, 3, "recordings/3.wav", timestamp="20260818_140000")

    from app import create_app

    client = create_app().test_client()
    response = client.get(
        "/api/recordings/inbox?limit=1&since_timestamp=20260818_120000",
        headers=admin_auth,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [recording["id"] for recording in body["recordings"]] == [3]
    assert body["meta"] == {
        "limit": 1,
        "returned": 1,
        "has_more": True,
        "next_before_timestamp": "20260818_140000",
        "next_before_id": 3,
    }


def test_serve_audio_returns_existing_file(recording_store, admin_auth):
    db_path, recordings_dir = recording_store
    audio_file = recordings_dir / "device" / "audio.wav"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"audio contents")
    _insert_recording(
        db_path,
        1,
        "recordings/device/audio.wav",
        timestamp="20260818_120000",
    )

    from app import create_app

    response = create_app().test_client().get(
        "/api/recordings/recordings/device/audio.wav",
        headers=admin_auth,
    )

    assert response.status_code == 200
    assert response.get_data() == b"audio contents"


def test_serve_audio_returns_not_found_for_missing_recording(
    recording_store, admin_auth
):
    from app import create_app

    response = create_app().test_client().get(
        "/api/recordings/recordings/missing.wav",
        headers=admin_auth,
    )

    assert response.status_code == 404


def test_delete_recording_removes_database_row_and_audio_file(
    recording_store, admin_auth
):
    db_path, recordings_dir = recording_store
    audio_file = recordings_dir / "device" / "audio.wav"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"audio")
    _insert_recording(db_path, 1, "recordings/device/audio.wav")

    from app import create_app

    response = create_app().test_client().delete(
        "/api/recordings/1", headers=admin_auth
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "message": "Recording deleted successfully",
        "file_deleted": True,
    }
    assert not _recording_exists(db_path, 1)
    assert not audio_file.exists()


def test_delete_recording_succeeds_when_audio_file_is_already_missing(
    recording_store, admin_auth
):
    db_path, _ = recording_store
    _insert_recording(db_path, 2, "recordings/device/missing.wav")

    from app import create_app

    response = create_app().test_client().delete(
        "/api/recordings/2", headers=admin_auth
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "message": "Recording deleted successfully",
        "file_deleted": False,
    }
    assert not _recording_exists(db_path, 2)


def test_delete_recording_rejects_path_outside_recordings_directory(
    recording_store, admin_auth
):
    db_path, _ = recording_store
    _insert_recording(db_path, 3, "../outside.wav")

    from app import create_app

    response = create_app().test_client().delete(
        "/api/recordings/3", headers=admin_auth
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid recording file path"}
    assert _recording_exists(db_path, 3)


def test_delete_recording_reports_file_deletion_failure(
    recording_store, admin_auth, monkeypatch
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

    from app import create_app

    response = create_app().test_client().delete(
        "/api/recordings/4", headers=admin_auth
    )

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Recording deleted from database, but audio file deletion failed",
        "recording_id": 4,
    }
    assert not _recording_exists(db_path, 4)
    assert audio_file.exists()
