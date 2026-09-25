"""Cloud transcription service configuration tests."""

import pytest

from app.services import transcription_service


def test_cloud_transcription_uses_supplied_settings_key(monkeypatch, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    captured = {}

    class Response:
        status_code = 200
        text = '{"text": "done"}'

        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "done"}

    def post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    settings = type(
        "Settings", (), {"get_setting": lambda self, key, default: "dashboard-key"}
    )()
    monkeypatch.setattr(transcription_service, "get_settings_manager", lambda: settings)
    monkeypatch.setattr(transcription_service.requests, "post", post)
    service = transcription_service.TranscriptionService.__new__(
        transcription_service.TranscriptionService
    )
    result = service._transcribe_boondock_api(audio)

    assert result == "done"
    assert captured["headers"]["X-Boondock-Key"] == "dashboard-key"
    assert captured["timeout"] == 60
    assert captured["data"] == {"model_id": "whisper-large-v3-turbo"}


def test_cloud_transcription_rejects_missing_settings_key(monkeypatch, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    settings = type("Settings", (), {"get_setting": lambda self, key, default: ""})()
    monkeypatch.setattr(transcription_service, "get_settings_manager", lambda: settings)

    with audio.open("rb") as audio_file, pytest.raises(
        ValueError, match="Missing Boondock Transcription API Key"
    ):
        transcription_service.request_openai_transcription(audio_file, audio.name)
