"""Transcription route persistence tests."""

import sqlite3
from app.routes import transcription_routes


def test_transcription_update_uses_resolved_recording_path(
    monkeypatch, tmp_path, admin_auth
):
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    recording = recordings_dir / "sample.wav"
    recording.write_bytes(b"original audio")

    database = tmp_path / "recordings.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE recordings (id INTEGER PRIMARY KEY, filename TEXT, transcription TEXT)"
        )
        connection.execute(
            "INSERT INTO recordings VALUES (?, ?, ?)",
            (1, "recordings/sample.wav", "old text"),
        )

    monkeypatch.setattr(transcription_routes, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(transcription_routes, "DB_PATH", database)
    monkeypatch.setattr(
        transcription_routes, "create_history_entry", lambda *args: None
    )
    from app import create_app

    response = (
        create_app()
        .test_client()
        .post(
            "/api/transcribe_save/1",
            data={"transcription": "corrected text"},
            headers=admin_auth,
        )
    )

    assert response.status_code == 200
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT transcription FROM recordings WHERE id = 1"
        ).fetchone() == ("corrected text",)
