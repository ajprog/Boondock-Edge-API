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
        transcription_service.TranscriptionAuthenticationError,
        match="Missing Boondock Transcription API Key",
    ):
        transcription_service.request_openai_transcription(audio_file, audio.name)


def test_cloud_transcription_raises_specific_error_for_unauthorized_response(
    monkeypatch, tmp_path
):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")

    class Response:
        status_code = 401
        text = '{"error":"expired"}'

        def raise_for_status(self):
            raise AssertionError('401 must be handled before generic HTTP errors')

    monkeypatch.setattr(
        transcription_service,
        "request_openai_transcription",
        lambda *args, **kwargs: Response(),
    )
    service = transcription_service.TranscriptionService.__new__(
        transcription_service.TranscriptionService
    )

    with pytest.raises(
        transcription_service.TranscriptionAuthenticationError,
        match="authentication failed",
    ):
        service._transcribe_boondock_api(audio)


def test_successful_empty_cloud_transcription_is_returned(monkeypatch):
    service = transcription_service.TranscriptionService.__new__(
        transcription_service.TranscriptionService
    )
    monkeypatch.setattr(service, '_transcribe_boondock_api', lambda path: '')

    assert service.transcribe_audio('audio.wav', use_local=False) == ''
